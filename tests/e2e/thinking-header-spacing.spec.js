const { test, expect } = require('@playwright/test');

// The route (topic tag) and the user prompt share a flex header line inside
// assistant/thinking bubbles. They're separated by a literal text node, and a
// flex container drops collapsible whitespace-only anonymous items — so two
// plain spaces collapse to *zero* gap and the prompt jams against the route.
// The separator must be non-collapsible (nbsp) so there's a real gap.

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

async function mockBackend(page) {
  await page.route('**/health', r => r.fulfill({ json: { status: 'ok' } }));
  await page.route('**/history**', r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**', r => r.fulfill({ json: {} }));
  await page.route('**/queue', r => r.fulfill({ json: [] }));
  await page.route('**/topics', r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: {
    topic: 'default', exists: false, content: '', path: '',
    squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false },
  }}));
  await page.route('**/topics/**', r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status', r => r.fulfill({ json: { status: 'pending', content: '' } }));
  await page.route('**/processes', r => r.fulfill({ json: [] }));
}

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META  = { event: 'meta',  data: { agent: 'claude', harness: 'claudecode', provider: 'anthropic', msg_id: 1, adhoc: false } };

test('thinking header keeps a gap between route and prompt', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');

  // Stream held open mid-flight (status event, no done) so the thinking bubble
  // stays in the DOM with its route + prompt header intact.
  await page.route('**/chat', r => r.fulfill({
    status: 200, headers: SSE_HEADERS,
    body: sse(META, { event: 'status', data: 'Working...' }),
  }));

  await page.fill('#input', 'route spacing check');
  await page.keyboard.press('Enter');

  const thinking = page.locator('.msg.assistant.msg-thinking');
  await expect(thinking).toBeVisible();
  await expect(thinking.locator('.topic-tag')).toBeVisible();
  await expect(thinking.locator('.history-prompt')).toBeVisible();

  const gap = await page.evaluate(() => {
    const header = document.querySelector('.msg.assistant.msg-thinking .response-header-text');
    const tag = header && header.querySelector('.topic-tag');
    const prompt = header && header.querySelector('.history-prompt');
    if (!tag || !prompt) return null;
    return prompt.getBoundingClientRect().left - tag.getBoundingClientRect().right;
  });

  expect(gap).not.toBeNull();
  expect(gap).toBeGreaterThan(0.5);
});
