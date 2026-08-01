const { test, expect } = require('@playwright/test');

async function mockApp(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: {} }));
  await page.route('**/quota', r => r.fulfill({ json: {} }));
  await page.route('**/stats/filters', r => r.fulfill({ json: { agents: ['claude'], topics: ['default', 'squid'] } }));
  await page.route('**/stats/filter-presets', r => r.fulfill({ json: [] }));
  await page.route('**/bookmarks', r => r.fulfill({ json: [] }));
}

function historyWith(item) {
  return r => r.fulfill({ json: { items: [item], has_more: false } });
}

test('reply button sits in the bottom-right corner of the response bubble', async ({ page }) => {
  await mockApp(page);
  // Long content so the bubble renders much wider than the short stats row —
  // if the button were still positioned relative to the stats row (its old,
  // buggy home) rather than the bubble itself, these edges would diverge.
  await page.route('**/history**', historyWith({
    id: 1, topic: 'squid', agent: 'claude', adhoc: false,
    prompt: 'test prompt',
    content: 'This is a much longer response body so the bubble renders wide, '
      + 'to make sure the reply button tracks the bubble edge and not some '
      + 'short sibling row.',
    timestamp: '2026-07-10T10:00:00Z', session_id: 'sid-1',
    stats: { input_tokens: 100, output_tokens: 50, duration_ms: 3000 },
  }));

  await page.goto('/');

  const replyBtn = page.locator('.msg-reply-btn');
  await expect(replyBtn).toBeVisible();
  await expect(replyBtn).toHaveAttribute('title', 'Reply');
  await expect(replyBtn.locator('.material-symbols-outlined')).toHaveText('reply');

  // It must be a child of the bubble itself (not the stats row) and
  // positioned so its right/bottom edges track the bubble's own edges.
  const bubble = page.locator('.msg.assistant').first();
  await expect(bubble.locator('> .msg-reply-btn')).toHaveCount(1);

  const bubbleBox = await bubble.boundingBox();
  const btnBox = await replyBtn.boundingBox();
  expect(bubbleBox.x + bubbleBox.width - (btnBox.x + btnBox.width)).toBeLessThan(12);
  expect(bubbleBox.y + bubbleBox.height - (btnBox.y + btnBox.height)).toBeLessThan(12);

  const overlapsText = await bubble.evaluate(el => {
    const btn = el.querySelector('.msg-reply-btn').getBoundingClientRect();
    const content = Array.from(el.children).find(child =>
      !child.classList.contains('response-header') &&
      !child.classList.contains('history-prompt-full') &&
      !child.classList.contains('msg-reply-btn') &&
      !child.classList.contains('msg-pin-btn') &&
      !child.classList.contains('msg-bookmark-btn')
    );
    const range = document.createRange();
    range.selectNodeContents(content);
    const textRects = Array.from(range.getClientRects()).filter(rect => rect.width && rect.height);
    return textRects.some(rect =>
      rect.left < btn.right &&
      rect.right > btn.left &&
      rect.top < btn.bottom &&
      rect.bottom > btn.top
    );
  });
  expect(overlapsText).toBe(false);
});

test('reply on a message from a different route sets the composer route without filtering history', async ({ page }) => {
  await mockApp(page);
  const historyRequests = [];
  await page.route('**/history**', route => {
    historyRequests.push(new URL(route.request().url()).search);
    return historyWith({
      id: 1, topic: 'squid', agent: 'claude', adhoc: false,
      prompt: 'test prompt', content: 'test response',
      timestamp: '2026-07-10T10:00:00Z', session_id: 'sid-1',
      stats: { input_tokens: 100, output_tokens: 50, duration_ms: 3000 },
    })(route);
  });

  await page.goto('/');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
  const requestsBeforeReply = historyRequests.length;

  await page.locator('.msg-reply-btn').click();

  // Composer route is set...
  await expect(page.locator('#topic-chip')).toHaveClass(/visible/);
  await expect(page.locator('#topic-chip')).toContainText('#squid');
  await expect(page.locator('#topic-chip')).toContainText('@claude');
  // ...and blinks to confirm the click did something.
  await expect(page.locator('#topic-chip')).toHaveClass(/reply-blink/);
  // Same-route case is unpinned — this click should only navigate.
  await expect(page.locator('.msg-pin-btn')).not.toHaveClass(/pinned/);

  // No new filtered /history request — reply must not touch the history filter.
  await page.waitForTimeout(200);
  expect(historyRequests.length).toBe(requestsBeforeReply);
});

test('reply on same route but a stale session pins the message into context', async ({ page }) => {
  await mockApp(page);
  // A live turn on #squid@claude first, establishing sid-current as "the" session.
  await page.route('**/history**', historyWith({
    id: 1, topic: 'squid', agent: 'claude', adhoc: false,
    prompt: 'earlier prompt', content: 'earlier response',
    timestamp: '2026-07-10T09:00:00Z', session_id: 'sid-old',
    stats: { input_tokens: 100, output_tokens: 50, duration_ms: 3000 },
  }));

  await page.goto('/');

  // First click lands us on #squid@claude (starting from no route filter).
  await page.locator('.msg-reply-btn').click();
  await expect(page.locator('#topic-chip')).toContainText('#squid@claude');
  await expect(page.locator('.msg-pin-btn')).not.toHaveClass(/pinned/);

  // Simulate that #squid@claude's current session has moved on to sid-current
  // (e.g. a later turn happened), while the rendered bubble above is sid-old.
  await page.evaluate(() => {
    _sessionIds['squid@claude'] = 'sid-current';
  });

  // Second click: now we're already on the message's route, so this should
  // pin it into context instead of navigating.
  await page.locator('.msg-reply-btn').click();

  await expect(page.locator('.msg-pin-btn')).toHaveClass(/pinned/);
  await expect(page.locator('#pin-count')).toHaveText('1');
  // Still blinks even though the route itself didn't change.
  await expect(page.locator('#topic-chip')).toHaveClass(/reply-blink/);
});

test('reply on same route with no cached session (e.g. just cleared) pins the message', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', historyWith({
    id: 1, topic: 'squid', agent: 'claude', adhoc: false,
    prompt: 'test prompt', content: 'test response',
    timestamp: '2026-07-10T10:00:00Z', session_id: 'sid-1',
    stats: { input_tokens: 100, output_tokens: 50, duration_ms: 3000 },
  }));

  await page.goto('/');

  // First click lands us on #squid@claude. _sessionIds['squid@claude'] is
  // never populated — same state as right after /clear deletes the entry.
  await page.locator('.msg-reply-btn').click();
  await expect(page.locator('#topic-chip')).toContainText('#squid@claude');
  await expect(page.locator('.msg-pin-btn')).not.toHaveClass(/pinned/);

  // Second click: same route, but with no known "current" session the
  // message's own sid is necessarily stale — this must pin, not no-op.
  await page.locator('.msg-reply-btn').click();

  await expect(page.locator('.msg-pin-btn')).toHaveClass(/pinned/);
  await expect(page.locator('#pin-count')).toHaveText('1');
  await expect(page.locator('#topic-chip')).toHaveClass(/reply-blink/);
});

test('reply on same route and same session is a no-op', async ({ page }) => {
  await mockApp(page);
  await page.route('**/history**', historyWith({
    id: 1, topic: 'squid', agent: 'claude', adhoc: false,
    prompt: 'test prompt', content: 'test response',
    timestamp: '2026-07-10T10:00:00Z', session_id: 'sid-1',
    stats: { input_tokens: 100, output_tokens: 50, duration_ms: 3000 },
  }));

  await page.goto('/');

  // First click lands us on #squid@claude.
  await page.locator('.msg-reply-btn').click();
  await expect(page.locator('#topic-chip')).toContainText('#squid@claude');

  await page.evaluate(() => {
    _sessionIds['squid@claude'] = 'sid-1'; // matches the bubble's own session_id
  });

  // Second click: same route, same session — nothing to pin, but the chip
  // should still blink so the click doesn't look like it did nothing.
  await page.locator('.msg-reply-btn').click();

  await expect(page.locator('.msg-pin-btn')).not.toHaveClass(/pinned/);
  await expect(page.locator('#pin-count')).not.toBeVisible();
  await expect(page.locator('#topic-chip')).toHaveClass(/reply-blink/);
});
