/**
 * Filter command and history display contract tests.
 * Verifies that /filter honours topic, agent, and adhoc (!) flag,
 * and that history shows only assistant responses (no user bubbles).
 */
const { test, expect } = require('@playwright/test');

async function expectFilterScope(page, scope) {
  if (!await page.locator('#search-bar').isVisible()) await page.locator('#chip-search-btn').click();
  await expect(page.locator('#search-bar-scope')).toHaveValue(scope);
}

async function mockBackend(page, { topic = 'squid', agent = 'claude' } = {}) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: topic, agent, last_model: null, last_backend: 'claude', queue_depth: 0, active: false, last_prompt: 'hi' }
  ]}));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic, exists: true, content: '---\nsquid:\n  code_roots_skipped: true\n---\n', path: `~/.squid/context/topics/${topic}/memory.md`,
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
}

// ── tests ─────────────────────────────────────────────────────────────────────

test('/filter with #topic@agent sends topic+agent+adhoc=false to /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=false/);
});

test('/filter with #topic@agent! sends adhoc=true to /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude! /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=true/);
});

test('/filter with #topic only omits adhoc param from /history', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid /filter');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).not.toMatch(/agent=/);
  expect(capturedUrl).not.toMatch(/adhoc/);
});

test('composer filter action works without a visible chip using the default latest agent', async ({ page }) => {
  await mockBackend(page, { topic: 'default', agent: 'codex' });

  const urls = [];
  await page.route('**/history**', async route => {
    urls.push(route.request().url());
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await expect(page.locator('#topic-chip')).not.toHaveClass(/visible/);
  await page.locator('#chip-filter-btn').click();

  await expect(page.locator('#topic-chip')).toContainText('#default@codex');
  expect(urls.at(-1)).toMatch(/topic=default/);
  expect(urls.at(-1)).toMatch(/agent=codex/);
  expect(urls.at(-1)).toMatch(/adhoc=false/);

  await page.locator('#chip-filter-btn').click();
  await expect.poll(() => urls.at(-1)).not.toMatch(/topic=/);
  expect(urls.at(-1)).not.toMatch(/agent=/);
  expect(urls.at(-1)).not.toMatch(/adhoc=/);
  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
});

test('/f accepts an explicit topic after the command during normal typing', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', async route => {
    urls.push(route.request().url());
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude!');
  await page.keyboard.press('Enter');
  await page.locator('#input').pressSequentially('/f #squid');
  await page.keyboard.press('Enter');

  const last = urls[urls.length - 1];
  expect(last).toMatch(/topic=squid/);
  expect(last).not.toMatch(/agent=/);
  expect(last).not.toMatch(/adhoc=/);
});

test('/f supports an agent-only filter across topics', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f @claude!');
  await page.keyboard.press('Enter');

  expect(capturedUrl).not.toMatch(/topic=/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).toMatch(/adhoc=true/);
});

test('/f @agent* filters both modes for one exact agent', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f @claude*');
  await page.keyboard.press('Enter');

  expect(capturedUrl).not.toMatch(/topic=/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).not.toMatch(/adhoc=/);
  await expectFilterScope(page, '@claude*');

  await page.locator('#search-bar-scope').click();
  await expect(page.locator('#search-scope-options')).toBeVisible();

  await page.fill('#input', '/f #squid@claude*');
  await page.keyboard.press('Enter');
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/agent=claude/);
  expect(capturedUrl).not.toMatch(/adhoc=/);
  await expectFilterScope(page, '#squid@claude*');
});

test('/f #all is treated as a chat prompt; reset is the explicit way to clear filters', async ({ page }) => {
  await mockBackend(page);
  let chatBody = null;
  await page.route('**/chat', route => {
    chatBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: 'event: done\ndata: \n\n',
    });
  });
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await expect(page.locator('#chip-filter-btn')).toHaveAttribute('aria-pressed', 'true');
  await page.fill('#input', '/f #all');
  await page.keyboard.press('Enter');

  await expect.poll(() => chatBody?.message).toBe('/f #all');
  await expectFilterScope(page, '#squid');
});

test('/filter with prose is sent as chat instead of becoming a filter command', async ({ page }) => {
  await mockBackend(page);
  const historyUrls = [];
  let chatBody = null;
  await page.route('**/history**', route => {
    historyUrls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route('**/chat', route => {
    chatBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: 'event: done\ndata: \n\n',
    });
  });

  await page.goto('/');
  await page.fill('#input', '/filter this list by severity');
  await page.keyboard.press('Enter');

  await expect.poll(() => chatBody?.message).toBe('/filter this list by severity');
  expect(historyUrls.at(-1)).not.toMatch(/topic=/);
});

test('/f supports a route-chain flow filter', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'codex' });

  const historyUrls = [];
  await page.route('**/history**', route => {
    historyUrls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid@codex>@revuqwen');
  await page.keyboard.press('Enter');

  await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
  await expectFilterScope(page, '#squid@codex>@revuqwen');
  await expect(page.locator('#topic-chip')).toContainText('#squid@codex>@revuqwen');
  await expect.poll(() => historyUrls.some(url =>
    url.includes('flow_route=%23squid%40codex%3E%40revuqwen')
  )).toBe(true);
  expect(historyUrls.at(-1)).toContain('flow_route=%23squid%40codex%3E%40revuqwen');
});

test('filter route can be edited without changing the message draft', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await expectFilterScope(page, '#squid@claude!');
  await page.fill('#input', 'unfinished prompt');
  await page.fill('#search-bar-scope', '@claude!');
  await page.locator('#search-bar-scope').press('Enter');
  await expectFilterScope(page, '@claude!');
  await expect(page.locator('#input')).toHaveValue('unfinished prompt');
  await page.fill('#search-bar-scope', '#squid');
  await page.locator('#search-bar-scope').press('Enter');
  await expectFilterScope(page, '#squid');
});

test('/filter reset clears filter and reloads history without params', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', async route => {
    urls.push(route.request().url());
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  // Set a filter first
  await page.fill('#input', '#squid@claude! /filter');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  // Now reset
  await page.fill('#input', '/filter reset');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);

  const last = urls[urls.length - 1];
  expect(last).not.toMatch(/topic=/);
  expect(last).not.toMatch(/agent=/);
  expect(last).not.toMatch(/adhoc=/);
});

test('composer filter icon highlights only while a filter is active', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));

  await page.goto('/');
  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);

  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
  await expect(page.locator('#chip-filter-btn')).toHaveAttribute('aria-pressed', 'true');

  await page.fill('#input', '/f reset');
  await page.keyboard.press('Enter');
  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
  await expect(page.locator('#chip-filter-btn')).toHaveAttribute('aria-pressed', 'false');
});

test('composer filter action clears an active filter even when current route differs', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await expectFilterScope(page, /\#squid/);

  await page.fill('#input', '#squid@claude draft');
  await page.locator('#chip-filter-btn').click();

  await expect.poll(() => urls.at(-1)).not.toMatch(/topic=/);
  expect(urls.at(-1)).not.toMatch(/agent=/);
  expect(urls.at(-1)).not.toMatch(/adhoc=/);
  await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
});

test('user prompts only keeps the active history filter', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await page.fill('#input', '/prompts');
  await page.keyboard.press('Enter');

  const last = urls[urls.length - 1];
  expect(last).toMatch(/topic=squid/);
  expect(last).toMatch(/agent=claude/);
  expect(last).toMatch(/adhoc=true/);
});

test('/prompts toggles user prompts only', async ({ page }) => {
  await mockBackend(page);

  const urls = [];
  await page.route('**/history**', route => {
    urls.push(route.request().url());
    return route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid@claude!');
  await page.keyboard.press('Enter');
  await page.fill('#input', '/prompts');
  await page.keyboard.press('Enter');

  const last = urls[urls.length - 1];
  expect(last).toMatch(/topic=squid/);
  expect(last).toMatch(/agent=claude/);
  expect(last).toMatch(/adhoc=true/);
});

test('bookmarked only sends the active filter to history', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  const urls = [];
  await page.route('**/history**', route => {
    urls.push(route.request().url());
    const url = new URL(route.request().url());
    const bookmarked = url.searchParams.get('bookmarked') === 'true';
    return route.fulfill({ json: { items: bookmarked ? [
      { id: 10, role: 'assistant', topic: 'squid', agent: 'claude', content: 'matching bookmark', status: 'done', adhoc: false, prompt: 'match', timestamp: new Date().toISOString() },
    ] : [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await page.locator('#chip-bookmark-btn').click();

  const last = urls.at(-1);
  expect(last).toMatch(/bookmarked=true/);
  expect(last).toMatch(/topic=squid/);
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.assistant.history-item')).toContainText('matching bookmark');
  await expect(page.locator('.msg.assistant.history-item')).not.toContainText('other bookmark');
});

test('/bookmarks and /bm toggle bookmarked only like the composer bookmark icon', async ({ page }) => {
  await mockBackend(page);
  await page.unroute('**/history**');
  await page.route('**/history**', route => {
    const url = new URL(route.request().url());
    const bookmarked = url.searchParams.get('bookmarked') === 'true';
    return route.fulfill({ json: { items: bookmarked ? [
      { id: 10, role: 'assistant', topic: 'squid', agent: 'claude', content: 'matching bookmark', status: 'done', adhoc: false, prompt: 'match', timestamp: new Date().toISOString() },
    ] : [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await page.fill('#input', '/bookmarks');
  await page.keyboard.press('Enter');

  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.assistant.history-item')).toContainText('matching bookmark');

  await page.fill('#input', '/bm');
  await page.keyboard.press('Enter');
  await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'false');
});

test('/bad highlights the shared thumbs-down toggle', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/history**', route => route.fulfill({ json: { items: [], has_more: false } }));
  await page.goto('/');
  await page.fill('#input', '/bad');
  await page.keyboard.press('Enter');
  await page.locator('#chip-search-btn').click();
  const toggle = page.getByRole('button', { name: 'Bad responses only', exact: true });
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await expect(toggle).toHaveClass(/active/);
  const icon = toggle.locator('.material-symbols-outlined');
  await expect(icon).toHaveText('thumb_down');
  expect(await icon.evaluate(el => getComputedStyle(el).fontVariationSettings)).toMatch(/["']FILL["'] 1/);
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
});

test('message bookmark and bad-response buttons post marker-only payloads', async ({ page }) => {
  await mockBackend(page);
  let bookmarkBody = null;
  let annotationBody = null;
  await page.route('**/bookmarks', route => {
    if (route.request().method() === 'POST') {
      bookmarkBody = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.route('**/annotations**', route => {
    if (route.request().method() === 'POST') {
      annotationBody = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.route('**/history**', route => route.fulfill({ json: {
    items: [
      { id: 10, role: 'assistant', topic: 'squid', agent: 'claude', content: 'response text that should not be copied', status: 'done', adhoc: false, prompt: 'match', timestamp: new Date().toISOString() },
    ],
    has_more: false,
  }}));

  await page.goto('/');
  await page.locator('.msg-bookmark-btn[data-msg-id="10"]').click();
  await page.locator('.msg-bad-response-btn[data-msg-id="10"]').click();

  expect(bookmarkBody).toEqual({ msg_id: 10 });
  expect(annotationBody).toEqual({ msg_id: 10, kind: 'bad_response', payload: {} });
});

test('history renders only assistant bubbles — no user bubbles', async ({ page }) => {
  await mockBackend(page);

  // Server returns only assistant rows — prompt snippet comes from the reply_to join
  await page.route('**/history**', r => r.fulfill({ json: {
    items: [
      { id: 2, role: 'assistant', topic: 'squid', agent: 'claude', content: 'Here is the answer.',
        status: 'done', adhoc: false, session_id: null, prompt: 'What is 2+2?',
        context: null, timestamp: new Date().toISOString(), reply_to: 1, stats: null },
    ],
    has_more: false,
  }}));

  await page.goto('/');
  await page.waitForTimeout(400);

  // Only the assistant bubble should be in the DOM
  await expect(page.locator('.msg.assistant.history-item')).toHaveCount(1);
  await expect(page.locator('.msg.user.history-item')).toHaveCount(0);

  // Prompt snippet is visible in the response header
  await expect(page.locator('.response-header-text')).toContainText('What is 2+2?');
});

test('a live in-flight message not matching the filter stays hidden, then reappears on clear', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));

  let fulfillChat;
  const chatIntercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      fulfillChat = body => route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
        body,
      });
      resolve();
    });
  });

  await page.goto('/');
  // No "#topic" prefix and no sticky chip → sends under #default, which the #squid filter below won't match.
  await page.fill('#input', 'hello');
  await page.keyboard.press('Enter');
  await chatIntercepted;
  await fulfillChat(`event: meta\ndata: ${JSON.stringify({ agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false })}\n\n`);

  const thinking = page.locator('.msg.assistant.msg-thinking');
  const userBubble = page.locator('.msg.user').last();
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');

  // Still in the DOM (so streaming keeps updating it) but not shown while filtered to a
  // different topic than the one this live message belongs to.
  await expect(thinking).toBeAttached();
  await expect(thinking).not.toBeVisible();
  await expect(userBubble).not.toBeVisible();

  await page.locator('#chip-filter-btn').click();
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();
});

test('a live in-flight message matching the filter stays visible', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));

  let fulfillChat;
  const chatIntercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      fulfillChat = body => route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
        body,
      });
      resolve();
    });
  });

  await page.goto('/');
  await page.fill('#input', '#squid hello');
  await page.keyboard.press('Enter');
  await chatIntercepted;
  await fulfillChat(`event: meta\ndata: ${JSON.stringify({ agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false })}\n\n`);

  const thinking = page.locator('.msg.assistant.msg-thinking');
  const userBubble = page.locator('.msg.user').last();
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');

  // Filtering to the same topic this live message belongs to should keep it visible.
  await expect(thinking).toBeVisible();
  await expect(userBubble).toBeVisible();

  await page.fill('#input', '/f #other');
  await page.keyboard.press('Enter');
  await expect(thinking).not.toBeVisible();
});

test('a completed live response not matching the filter is not added to the visible list', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));

  let fulfillChat;
  const chatIntercepted = new Promise(resolve => {
    page.route('**/chat', route => {
      fulfillChat = body => route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
        body,
      });
      resolve();
    });
  });

  await page.goto('/');
  await page.fill('#input', 'hello');
  await page.keyboard.press('Enter');
  await chatIntercepted;

  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await fulfillChat(
    `event: meta\ndata: ${JSON.stringify({ agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false })}\n\n` +
    `data:Filtered out response\n\n` +
    `event: done\ndata: \n\n`
  );

  await expect(page.locator('.msg.assistant:not(.msg-thinking)').filter({ hasText: 'Filtered out response' })).toHaveCount(0);
});

test('a recovered pending item is shown only while it matches the active filter', async ({ page }) => {
  await mockBackend(page, { topic: 'squid', agent: 'claude' });
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/history**', r => r.fulfill({ json: {
    items: [
      { id: 5, role: 'assistant', topic: 'squid', agent: 'claude', content: '', status: 'pending',
        adhoc: false, prompt: 'still running', context: null, timestamp: new Date().toISOString() },
    ],
    has_more: false,
  }}));

  await page.goto('/');
  await page.waitForTimeout(300);
  await expect(page.locator('.msg-thinking.history-item')).toHaveCount(1);

  // Filtering to the same topic as the pending item keeps it visible.
  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  await expect(page.locator('.msg-thinking.history-item')).toHaveCount(1);

  // Filtering to a different topic hides it.
  await page.fill('#input', '/f #other');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(300);
  // Keep the recovered stream connected while its group is filtered out.
  await expect(page.locator('.msg-thinking.history-item')).toHaveCount(1);
  await expect(page.locator('.msg-thinking.history-item')).toBeHidden();
});

test('/f #topic* filters the whole subtree and sends topic_subtree=true', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid*');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).toMatch(/topic_subtree=true/);
  await expectFilterScope(page, /\#squid\*/);
});

test('/f #topic.cat* filters a nested subtree at any depth', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid.cat1*');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid\.cat1/);
  expect(capturedUrl).toMatch(/topic_subtree=true/);
  await expectFilterScope(page, /\#squid\.cat1\*/);
});

test('/f #topic* without a wildcard stays exact (no topic_subtree param)', async ({ page }) => {
  await mockBackend(page);

  let capturedUrl = null;
  await page.route('**/history**', async route => {
    capturedUrl = route.request().url();
    await route.fulfill({ json: { items: [], has_more: false } });
  });

  await page.goto('/');
  await page.fill('#input', '/f #squid');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(300);
  expect(capturedUrl).toMatch(/topic=squid/);
  expect(capturedUrl).not.toMatch(/topic_subtree/);
  await expectFilterScope(page, /\#squid/);
});

for (const width of [1280, 375]) {
  test(`shared history dropdown edits route at width ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 812 });
    await mockBackend(page);
    const urls = [];
    await page.route('**/history**', route => {
      urls.push(new URL(route.request().url()));
      return route.fulfill({ json: { items: [], has_more: false } });
    });
    await page.goto('/');
    await page.fill('#input', '/f #squid@claude!');
    await page.keyboard.press('Enter');
    await page.fill('#input', 'keep my draft');
    await page.locator('#chip-search-btn').click();
    await page.locator('#search-scope-toggle').click();
    await expect(page.locator('#search-scope-options')).toBeVisible();
    await page.locator('#search-scope-options [data-scope="#squid"]').click();
    await page.locator('#search-bar-submit').click();
    await expectFilterScope(page, '#squid');
    await expect.poll(() => urls.at(-1)?.searchParams.get('topic')).toBe('squid');
    expect(urls.at(-1).searchParams.has('agent')).toBe(false);
    await expect(page.locator('#input')).toHaveValue('keep my draft');
    await expect(page.locator('#chip-filter-btn')).toHaveClass(/active/);
    expect(await page.locator('#search-bar').evaluate(el => el.getBoundingClientRect().right <= innerWidth)).toBe(true);
    await page.fill('#search-bar-scope', 'invalid route');
    await page.locator('#search-bar-scope').press('Enter');
    await expect(page.locator('#search-bar-scope')).toHaveAttribute('aria-invalid', 'true');
    await page.locator('#chip-filter-btn').click();
    await expectFilterScope(page, '#all');
    await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
    await page.locator('#chip-bookmark-btn').click();
    await expect(page.locator('#search-bar-bookmarks')).toHaveAttribute('aria-pressed', 'true');
    await page.fill('#input', '/f #squid');
    await page.locator('#input').press('Enter');
    await page.locator('#chip-search-btn').click();
    await page.locator('#search-bar-scope').focus();
    await page.locator('#search-bar-scope').press('Escape');
    await expect(page.locator('#search-scope-options')).not.toBeVisible();
    await page.locator('#search-bar-scope').press('ArrowDown');
    await page.keyboard.press('Enter');
    await expectFilterScope(page, '#all');
    await page.locator('#search-bar-submit').click();
    await expect(page.locator('#chip-filter-btn')).not.toHaveClass(/active/);
    await expect(page.locator('#chip-bookmark-btn')).toHaveAttribute('aria-pressed', 'true');
  });
}
