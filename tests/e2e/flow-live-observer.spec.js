/**
 * Live flow chain in an OBSERVER tab (renderer=store): no composer send here —
 * every turn arrives via WS discovery, the way a second tab (or the same tab
 * after the sender navigated away) watches `#tst@echo>@echo1 hi` render.
 * This is the path where live-flow render bugs actually reproduce (the sender
 * tab's bubbles are composer-owned and clean).
 *
 * Parity contract with a history refresh: once the chain completes, the live
 * view must show the same scaffolding history shows —
 *   1. response order: older history, then origin, then chain step,
 *   2. the flow divider (route-chain-marker) above the origin,
 *   3. date dividers: one for the older rows, one "Today" for the flow turns,
 *   4. each response header still carrying its prompt (the observer never had
 *      a standalone user bubble — the embedded prompt is the only copy, so if
 *      completion drops it the user prompt is gone from the live view).
 */
const { test, expect } = require('@playwright/test');

const NOW = new Date().toISOString();
const LATER = new Date(Date.now() + 1000).toISOString();
const LATEST = new Date(Date.now() + 2000).toISOString();

const OLDER = [
  { id: 10, role: 'assistant', topic: 'squid', agent: 'claude', adhoc: false, status: 'done',
    prompt: 'older one', content: 'older response one',
    timestamp: '2026-07-15T10:00:00Z', completed_at: '2026-07-15T10:01:00Z' },
  { id: 11, role: 'assistant', topic: 'squid', agent: 'claude', adhoc: false, status: 'done',
    prompt: 'older two', content: 'older response two',
    timestamp: '2026-07-15T10:02:00Z', completed_at: '2026-07-15T10:03:00Z' },
];

const ORIGIN_PENDING = {
  id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
  status: 'pending', prompt: '#tst@echo>@echo1 hi', content: '',
  reply_to: 99,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: NOW, completed_at: null, stats: {},
};
const ORIGIN_DONE = {
  ...ORIGIN_PENDING, status: 'done', content: 'hi', completed_at: LATER,
};
const STEP_PENDING = {
  id: 101, role: 'assistant', topic: 'tst', agent: 'echo1', adhoc: false,
  status: 'pending', prompt: '#tst@echo>@echo1 hi', content: '',
  reply_to: 102,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: LATER, completed_at: null, stats: {},
};
const STEP_DONE = {
  ...STEP_PENDING, status: 'done', content: 'hi', completed_at: LATEST,
};

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: OLDER, has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: 'tst', agent: 'echo', last_model: null, last_backend: 'echo', queue_depth: 0, active: false, last_prompt: 'hi' },
  ] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'echo' }, { name: 'echo1' }] }));
}

test('observer tab live flow chain keeps prompt, flow divider, date divider, order', async ({ page }) => {
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
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));

  // Status rows flip from pending to done as the mock run progresses.
  const statusRows = { 100: ORIGIN_PENDING, 101: STEP_PENDING };
  await page.route('**/chat/100/status', r => r.fulfill({ json: statusRows[100] }));
  await page.route('**/chat/101/status', r => r.fulfill({ json: statusRows[101] }));

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);

  const emit = frames => page.evaluate(list => {
    for (const frame of list) window.__webSocket.receive(frame);
  }, frames);

  // Origin appears (another tab sent it): user row, then pending assistant row.
  await emit([
    { v: 1, type: 'message.changed', event_id: 1, msg_id: 99, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 99, role: 'user', status: 'done', content: '#tst@echo>@echo1 hi' } },
    { v: 1, type: 'message.changed', event_id: 2, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 100, role: 'assistant', status: 'pending', reply_to: 99 } },
  ]);
  const originThinking = page.locator('.msg.assistant.msg-thinking[data-msg-id="100"]');
  await expect(originThinking).toBeVisible();

  // Origin streams, then completes.
  statusRows[100] = ORIGIN_DONE;
  await emit([
    { v: 1, type: 'chat.text', event_id: 3, msg_id: 100, run_seq: 0, scope: { topic: 'tst', agent: 'echo' }, payload: { text: 'hi' } },
    { v: 1, type: 'message.changed', event_id: 4, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 100, role: 'assistant', status: 'done', content: 'hi', reply_to: 99 } },
    { v: 1, type: 'chat.done', event_id: 5, msg_id: 100, run_seq: 1, scope: { topic: 'tst', agent: 'echo' }, payload: {} },
  ]);
  const originBubble = page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)');
  await expect(originBubble).toBeVisible();

  // Server dispatches the chain step — including the spurious out-of-order
  // message.changed(pending) for the origin observed on a real server.
  await emit([
    { v: 1, type: 'message.changed', event_id: 6, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 100, role: 'assistant', status: 'pending', content: '', reply_to: 99 } },
    { v: 1, type: 'message.changed', event_id: 7, msg_id: 102, scope: { topic: 'tst', agent: 'echo1' },
      payload: { id: 102, role: 'user', status: 'done', content: 'Squid route chain handoff.' } },
    { v: 1, type: 'message.changed', event_id: 8, msg_id: 101, scope: { topic: 'tst', agent: 'echo1' },
      payload: { id: 101, role: 'assistant', status: 'pending', content: '', reply_to: 102 } },
    { v: 1, type: 'flow.step.created', event_id: 9, msg_id: 101, scope: { topic: 'tst', agent: 'echo1' },
      payload: { flow_run_id: 'run-1', step_id: 'step-101', assistant_msg_id: 101 } },
  ]);
  const stepThinking = page.locator('.msg.assistant.msg-thinking[data-msg-id="101"]');
  await expect(stepThinking).toBeVisible();

  // Chain step streams, then completes.
  statusRows[101] = STEP_DONE;
  await emit([
    { v: 1, type: 'chat.text', event_id: 10, msg_id: 101, run_seq: 0, scope: { topic: 'tst', agent: 'echo1' }, payload: { text: 'hi' } },
    { v: 1, type: 'message.changed', event_id: 11, msg_id: 101, scope: { topic: 'tst', agent: 'echo1' },
      payload: { id: 101, role: 'assistant', status: 'done', content: 'hi', reply_to: 102 } },
    { v: 1, type: 'chat.done', event_id: 12, msg_id: 101, run_seq: 1, scope: { topic: 'tst', agent: 'echo1' }, payload: {} },
  ]);
  const stepBubble = page.locator('.msg.assistant.history-item[data-msg-id="101"]:not(.msg-thinking)');
  await expect(stepBubble).toBeVisible();

  // ── 1. Response order: older history, origin, chain step. ────────────────
  await expect(originBubble).toBeVisible();
  const ids = await page.locator('#messages > .msg.assistant.history-item[data-msg-id]').evaluateAll(
    nodes => nodes.map(n => n.dataset.msgId),
  );
  expect(ids).toEqual(['10', '11', '100', '101']);

  // ── 2. Flow divider: exactly one route-chain-marker for this route, ───────
  // sitting above the origin bubble.
  const markers = page.locator('.route-chain-marker[data-flow-route]');
  await expect(markers).toHaveCount(1);
  const markerBeforeOrigin = await page.evaluate(() => {
    const marker = document.querySelector('.route-chain-marker[data-flow-route]');
    const origin = document.querySelector('.msg.assistant.history-item[data-msg-id="100"]');
    if (!marker || !origin) return false;
    return !!(marker.compareDocumentPosition(origin) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(markerBeforeOrigin).toBe(true);

  // ── 3. Date dividers: one for the older rows, one "Today" for the flow ────
  const dividers = await page.locator('#messages .date-divider').evaluateAll(
    nodes => nodes.map(n => n.textContent.trim()),
  );
  expect(dividers.length).toBe(2);
  expect(dividers[1]).toBe('Today');
  // The Today divider must introduce the flow chain, not sit below it.
  const dividerBoundaryOrder = await page.evaluate(() => {
    const divs = [...document.querySelectorAll('#messages .date-divider')];
    const today = divs.find(d => d.textContent.trim() === 'Today');
    const marker = document.querySelector('.route-chain-marker[data-flow-route]');
    const origin = document.querySelector('.msg.assistant.history-item[data-msg-id="100"]');
    if (!today || !marker || !origin) return false;
    return today.nextElementSibling === marker
      && !!(marker.compareDocumentPosition(origin) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(dividerBoundaryOrder).toBe(true);

  // ── 4. Prompt survives completion in both response headers. ──────────────
  for (const id of ['100', '101']) {
    const header = page.locator(`.msg.assistant.history-item[data-msg-id="${id}"] .response-header`);
    await expect(header).toContainText('#tst@echo>@echo1 hi');
  }
});
