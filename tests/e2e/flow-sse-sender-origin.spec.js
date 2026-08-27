/**
 * Regression test: the sender's own SSE flow-origin completion must keep the
 * standalone user prompt visible, exactly like the reconciler's render() does.
 *
 * Mirrors the real "auto/SSE" composer path: POST /chat → SSE stream with
 * `meta` (carrying msg_id), streamed `status` text, then `done`. The `done`
 * handler runs replacePendingWithStoredItem (registered pending→terminal
 * handoff) which fetches /chat/{id}/status and reconciles.
 */
const { test, expect } = require('@playwright/test');

const SSE_HEADERS = { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' };
function sse(...events) {
  return events.map(({ event, data, id }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    const idLine = id != null ? `id: ${id}\n` : '';
    return idLine + (event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`);
  }).join('');
}

const NOW = new Date().toISOString();
const LATER = new Date(Date.now() + 1000).toISOString();

const ORIGIN_DONE = {
  id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
  status: 'done', prompt: '#tst@echo>@echo1 hi', content: 'hi',
  reply_to: 99,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: NOW, completed_at: LATER, stats: {},
};

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: 'tst', agent: 'echo', last_model: null, last_backend: 'echo', queue_depth: 0, active: false, last_prompt: 'hi' },
  ] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'echo' }, { name: 'echo1' }] }));
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
  await page.route('**/chat/100/status', r => r.fulfill({ json: ORIGIN_DONE }));
}

test('SSE sender flow origin keeps its standalone user prompt on completion', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/chat', route => {
    route.fulfill({
      status: 200,
      headers: {
        ...SSE_HEADERS,
        'X-Squid-Msg-Id': '100',
        'X-Squid-Flow-Run-Id': 'run-1',
      },
      body: sse(
        { event: 'meta', data: { agent: 'echo', backend: 'echo', msg_id: 100, adhoc: false } },
        { event: 'status', data: 'hi' },
        { event: 'done', data: '' },
      ),
    });
  });

  await page.goto('/');
  // app.js defines clearTopicChip as a top-level function; wait until the
  // composer is interactive before using it (the WS tests gate on their mock
  // socket becoming OPEN first — this SSE path has no such signal).
  await page.waitForFunction(() => typeof clearTopicChip === 'function', null, { timeout: 10_000 });
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '#tst@echo>@echo1 hi');
  await page.keyboard.press('Enter');

  const originBubble = page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)');
  await expect(originBubble).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: '#tst@echo>@echo1 hi' })).toBeVisible();

  const ids = await page.locator('#messages > .msg.assistant.history-item[data-msg-id]').evaluateAll(
    nodes => nodes.map(n => n.dataset.msgId),
  );
  expect(ids).toEqual(['100']);
});
