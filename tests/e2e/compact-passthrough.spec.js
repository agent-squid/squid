/**
 * /compact is no longer a Squid-owned command (see ADR-0013 note in
 * docs/data-model-and-api.md). It must fall through to a plain chat message
 * so interactive-protocol backends can run their own native compaction,
 * instead of Squid intercepting it and hitting POST /cmd.
 */
const { test, expect } = require('@playwright/test');

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  'X-Accel-Buffering': 'no',
};

function sse(...events) {
  return events.map(({ event, data }) => {
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return event ? `event: ${event}\ndata: ${str}\n\n` : `data: ${str}\n\n`;
  }).join('');
}

const META = { event: 'meta', data: { agent: 'claude', backend: 'claude', msg_id: 1, adhoc: false } };
const DONE = { event: 'done', data: '' };

async function mockBackend(page) {
  await page.route('**/health',          r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',       r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',         r => r.fulfill({ json: {} }));
  await page.route('**/topics',          r => r.fulfill({ json: [] }));
  await page.route('**/topics/*/memory', r => r.fulfill({ json: { topic: 'test', exists: false, content: '', squid: { code_roots: [], code_roots_skipped: true, code_roots_missing: false } } }));
  await page.route('**/topics/**',       r => r.fulfill({ json: [] }));
  await page.route('**/config/agents',   r => r.fulfill({ json: [] }));
  await page.route('**/chat/*/status',   r => r.fulfill({ json: { status: 'pending', content: '' } }));
}

test('typing /compact shows no Squid command match in the autocomplete popup', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await page.fill('#input', '/compact');
  await expect(page.locator('#autocomplete')).not.toHaveClass(/open/);
});

test('/compact is sent to the backend as a plain chat message, not POST /cmd', async ({ page }) => {
  await mockBackend(page);
  let cmdCalled = false;
  await page.route('**/cmd', r => { cmdCalled = true; r.fulfill({ json: { ok: true } }); });

  let chatBody = null;
  await page.route('**/chat', route => {
    chatBody = JSON.parse(route.request().postData() || '{}');
    route.fulfill({ status: 200, headers: SSE_HEADERS, body: sse(META, { data: 'Compacted.' }, DONE) });
  });

  await page.goto('/');
  await page.fill('#input', '#squid@claude /compact');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toBeVisible();
  expect(cmdCalled).toBe(false);
  expect(chatBody.message).toBe('/compact');
});
