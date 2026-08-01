const { test, expect } = require('@playwright/test');

// The composer used to let typing a full "#topic@agent message" silently
// swap the active sticky chip — a workaround from before the chip had
// explicit edit affordances (click-to-edit, semantic backspace). Now that
// those exist, that implicit swap is gone: with a chip active, a leading
// "#" is just literal message text unless the chip is explicitly
// cleared/expanded first.
//
// This has two independent code paths that both used to swap the chip:
// parseInput's submit-time parsing, and _maybePromoteSlug's live "input"
// listener, which fires on every keystroke and used to auto-promote a bare
// "#topic " (trailing space) into a new chip regardless of whether one was
// already active. A .fill() call sets the whole value in one shot and never
// triggers that per-keystroke listener, so a test using .fill() alone can
// pass even if the live-typing path is still broken — both are exercised
// below with real keystrokes to guard against that gap.
test('typing a full route while a chip is active sends it as literal text, not a route swap', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  const chatBodies = [];
  await page.route('**/chat', r => {
    const body = JSON.parse(r.request().postData() || '{}');
    chatBodies.push(body);
    return r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-Squid-Msg-Id': '10' },
      body: `event: meta\ndata: {"agent":"${body.agent}","msg_id":10,"adhoc":${body.adhoc ? 'true' : 'false'}}\n\ndata:response\n\nevent: done\ndata: \n\n`,
    });
  });

  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');

  // Real keystrokes, one at a time — this is what actually drives the
  // 'input' event listener and _maybePromoteSlug, unlike .fill().
  await page.click('#input');
  await page.keyboard.type('#other@bob hello there', { delay: 10 });
  await expect(page.locator('#input')).toHaveValue('#other@bob hello there');
  // The chip must survive the trailing-space moment mid-typing ("#other@bob ")
  // that used to trigger _maybePromoteSlug's auto-swap.
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');

  await page.keyboard.press('Enter');

  await expect.poll(() => chatBodies.length).toBe(1);
  expect(chatBodies[0]).toMatchObject({
    topic: 'squid',
    agent: 'codex',
    message: '#other@bob hello there',
  });

  // The chip itself is untouched by the send.
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');
});

test('typing "#topic " from a blank composer still auto-promotes to a chip', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  await page.goto('/');
  await page.waitForSelector('#input');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);

  await page.click('#input');
  await page.keyboard.type('#squid ', { delay: 10 });

  await expect(page.locator('#topic-chip')).toContainText('#squid');
  await expect(page.locator('#input')).toHaveValue('');
});

test('route-shaped autocomplete is suppressed while a chip is active, offered when none is', async ({ page }) => {
  // Since a chip already active blocks "#topic@agent" from functioning as a
  // route (see above), offering route-completion suggestions for it would be
  // misleading — picking one would just insert literal text, not set a route.
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'qwen', backend: 'codex:local' }] }));
  await page.route('**/topics/*/agents/history', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');

  await page.click('#input');
  await page.keyboard.type('#squid@qwen', { delay: 10 });
  await expect.poll(() => page.evaluate(() => acOpen)).toBe(false);

  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '');
  await page.click('#input');
  await page.keyboard.type('#squid@qw', { delay: 10 });
  await expect.poll(() => page.evaluate(() => acItems.some(i => (i.insert || '').includes('qwen')))).toBe(true);
});

test('backspace deletes one character at a time in message text while a chip is active', async ({ page }) => {
  // semanticRouteBackspace's whole-segment deletion (e.g. one press removes
  // an entire "@agent") only makes sense for text that's actually going to
  // function as a route. With a chip active, "#topic@agent" typed as message
  // text no longer does (see above) — backspace inside it should behave like
  // regular text editing, not route editing.
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  await page.addInitScript(() => localStorage.setItem('squid_sticky_chip', JSON.stringify({
    topic: 'squid', agent: 'codex', adhoc: false, lookback: 0,
  })));
  await page.goto('/');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex');

  await page.click('#input');
  await page.keyboard.type('#other@bob hello', { delay: 10 });
  await page.locator('#input').evaluate(el => el.setSelectionRange(10, 10)); // right after "@bob"
  await page.keyboard.press('Backspace');
  // Single-char deletion: "#other@bo hello", not the whole "bob" agent name.
  await expect(page.locator('#input')).toHaveValue('#other@bo hello');

  // Sanity: with no chip active, the same edit still does semantic (whole-name) deletion.
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '');
  await page.click('#input');
  await page.keyboard.type('#other@bob', { delay: 10 });
  await page.locator('#input').evaluate(el => el.setSelectionRange(10, 10));
  await page.keyboard.press('Backspace');
  await expect(page.locator('#input')).toHaveValue('#other@');
});

test('backspace trims dotted composer topics to the previous dot', async ({ page }) => {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok', backends: {} } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/prompts/recent**', r => r.fulfill({ json: { items: [] } }));

  await page.goto('/');
  await page.fill('#input', '#parent.child');
  await page.locator('#input').press('Backspace');

  await expect(page.locator('#input')).toHaveValue('#parent.');
});
