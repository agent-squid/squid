/**
 * Regression test: a flow origin turn completed through the direct-DOM path
 * (insertCompletedHistoryItem — used by flow.step attach and realtime
 * discovery) must keep its standalone user prompt, not drop it until a later
 * reconcile rebuilds the turn.
 *
 * The reconciler's render() keeps the origin prompt via
 * `(!item.flow_route || route)`; insertCompletedHistoryItem used to gate on
 * `!item.flow_route` alone, so a flow origin completed directly lost its
 * prompt until the next turn's reconcile re-added it.
 */
const { test, expect } = require('@playwright/test');

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
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'websocket' } }));
  await page.route('**/chat/100/status', r => r.fulfill({ json: ORIGIN_DONE }));
}

test('direct-DOM flow origin keeps its standalone user prompt', async ({ page }) => {
  await page.addInitScript(() => {
    window.__webSocket = null;
    class MockWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        window.__webSocket = this;
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } });
        });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);

  // A flow step attach fetches the origin's terminal row and renders it
  // directly through insertCompletedHistoryItem — the path that used to drop
  // the flow origin's standalone prompt.
  await page.evaluate(() => window.__webSocket.receive({
    v: 1, type: 'flow.step.created', event_id: 1, msg_id: 100,
    scope: { topic: 'tst', agent: 'echo' },
    payload: { flow_run_id: 'run-1', step_id: 'step-100', assistant_msg_id: 100 },
  }));

  await expect(page.locator('.msg.assistant.history-item[data-msg-id="100"]')).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: '#tst@echo>@echo1 hi' })).toBeVisible();
});
