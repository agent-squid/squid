/**
 * ADR-0011 — direct-DOM completed insert interleaves above a live composer turn.
 *
 * The direct-DOM completion path (insertCompletedHistoryItem) used to anchor
 * only on `.msg.assistant.history-item[data-msg-id]`. A composer's live
 * thinking bubble is `.msg assistant msg-thinking` WITHOUT `.history-item`
 * (only the recovered wip bubble in makeWipBubble carries it), so that
 * selector missed live turns entirely: a turn that finished *before* a newer
 * prompt was submitted would still land below that prompt (bottomSentinel),
 * because the anchor search could not see the live group it should precede.
 *
 * This drives the real direct-DOM insert (via attachFlowStep, the only
 * non-reconciler completed-insert call site reachable without an in-flight
 * pending registration) while a plain composer turn is live, and asserts the
 * completed turn sorts above that live turn.
 */
const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: 'tst', agent: 'echo', last_model: null, last_backend: 'echo', queue_depth: 0, active: false, last_prompt: 'hi' },
  ] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'echo' }] }));
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
}

test('direct-DOM completed insert interleaves above a live composer turn', async ({ page }) => {
  await page.addInitScript(() => {
    window.__webSocket = null;
    class MockWebSocket {
      static CONNECTING = 0; static OPEN = 1;
      constructor() {
        this.readyState = MockWebSocket.CONNECTING;
        window.__webSocket = this;
        setTimeout(() => { this.readyState = MockWebSocket.OPEN; this.onopen?.(); this.receive({ v: 1, type: 'hello', payload: { cursor: 0 } }); });
      }
      send(data) {
        const frame = JSON.parse(data);
        if (frame.type === 'subscribe') {
          setTimeout(() => this.receive({ v: 1, type: 'subscribed', payload: {} }));
        } else if (frame.type === 'chat.start') {
          const n = window.__chatN = (window.__chatN || 0) + 1;
          const uid = 100 + (n - 1) * 10, aid = 101 + (n - 1) * 10;
          setTimeout(() => {
            this.receive({ v: 1, type: 'message.changed', event_id: n * 3 + 1, msg_id: uid, scope: { topic: 'tst', agent: 'echo' }, payload: { id: uid, role: 'user', status: 'done', content: frame.payload.prompt } });
            this.receive({ v: 1, type: 'message.changed', event_id: n * 3 + 2, msg_id: aid, scope: { topic: 'tst', agent: 'echo' }, payload: { id: aid, role: 'assistant', status: 'pending', reply_to: uid } });
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id, payload: { ok: true, msg_id: aid } });
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  // A completed plain turn (id 999) that finished long before the live turn.
  await page.route('**/chat/999/status', r => r.fulfill({ json: {
    id: 999, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
    status: 'done', prompt: 'old prompt', content: 'old response', reply_to: 998,
    timestamp: '2026-08-23T12:18:00Z', completed_at: '2026-08-23T12:18:30Z', stats: {},
  }}));

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);
  await page.evaluate(() => clearTopicChip());

  // A composer turn ("123") stays live/pending — a thinking bubble WITHOUT `.history-item`.
  await page.fill('#input', '123');
  await page.keyboard.press('Enter');
  await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="101"]')).toBeVisible();

  // Insert the older completed turn through the direct-DOM path.
  await page.evaluate(() => window.__webSocket.receive({
    v: 1, type: 'flow.step.created', event_id: 20, msg_id: 999,
    payload: { flow_run_id: 'fr1', step_id: 's1', assistant_msg_id: 999 },
  }));
  await expect(page.locator('.msg.assistant.history-item[data-msg-id="999"]:not(.msg-thinking)')).toBeVisible();

  const order = await page.evaluate(() => [...document.querySelectorAll('#messages > *')]
    .map(e => `${e.className}[${e.dataset.msgId || e.dataset.turnOwnerId || ''}]`));

  const idx999 = order.findIndex(e => e.includes('999') && e.includes('history-item'));
  const idxUser123 = order.findIndex(e => e.includes('user') && e.includes('101'));
  expect(idx999).toBeGreaterThanOrEqual(0);
  expect(idxUser123).toBeGreaterThanOrEqual(0);
  // The completed turn (12:18:30) must sort ABOVE the live "123" prompt.
  expect(idx999).toBeLessThan(idxUser123);
});
