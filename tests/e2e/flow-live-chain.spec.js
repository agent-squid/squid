/**
 * Regression test for #13956: renderer=store live flow chain loses the origin
 * turn. `#tst@echo>@echo1 hi` — the origin's thinking bubble shows and
 * completes, then the server-dispatched chain step runs; once the step
 * completes, the origin's bubble must still sit at the live edge, directly
 * above the step's bubble.
 *
 * Root cause: replacePendingWithStoredItem's direct-DOM fallback inserted the
 * origin's completed bubble without installing the fetched terminal row into
 * the transcript store, leaving the turn's completedAt at its pending-seed
 * null — which sorts before every real timestamp in getOrderedTurnIds(). The
 * chain step's completion runs a full reconcile() (registered path), adopts
 * the origin's bubble, and reorder() moves it to the oldest position, far
 * above the live edge. Two older history rows exist here so that displacement
 * is observable in document order.
 */
const { test, expect } = require('@playwright/test');

// Flow turns must land in the SAME calendar day as the composer's own user
// bubble (whose timestamp is real "now"), or refreshDateDividers inserts an
// extra divider between them and the divider-count assertions below fail.
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

const ORIGIN_ROW = {
  id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
  status: 'done', prompt: '#tst@echo>@echo1 hi', content: 'hi',
  reply_to: 99,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: NOW, completed_at: LATER,
  stats: {},
};
const STEP_ROW_PENDING = {
  id: 101, role: 'assistant', topic: 'tst', agent: 'echo1', adhoc: false,
  status: 'pending', prompt: '#tst@echo>@echo1 hi', content: '',
  reply_to: 102,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: LATER, completed_at: null,
  stats: {},
};
const STEP_ROW_DONE = {
  ...STEP_ROW_PENDING,
  status: 'done', content: 'hi', completed_at: LATEST,
};

test('live flow chain keeps the origin turn at the live edge (renderer=store)', async ({ page }) => {
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
        } else if (frame.type === 'chat.start') {
          setTimeout(() => {
            // The server journals message.changed for the inserted user +
            // assistant rows inside _prepare_chat_turn — before command.result
            // is sent. The journal pump can therefore deliver them first.
            this.receive({ v: 1, type: 'message.changed', event_id: 1, msg_id: 99, scope: { topic: 'tst', agent: 'echo' },
              payload: { id: 99, role: 'user', status: 'done', content: '#tst@echo>@echo1 hi' } });
            this.receive({ v: 1, type: 'message.changed', event_id: 2, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
              payload: { id: 100, role: 'assistant', status: 'pending', reply_to: 99 } });
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 100, flow_run_id: 'run-1' } });
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
  await mockBackend(page);
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
  await page.route('**/chat/100/status', route => route.fulfill({ json: ORIGIN_ROW }));
  let stepStatusCalls = 0;
  await page.route('**/chat/101/status', route => {
    stepStatusCalls += 1;
    route.fulfill({ json: stepStatusCalls === 1 ? STEP_ROW_PENDING : STEP_ROW_DONE });
  });

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);

  // The mocked /topics + /config/agents make boot install a sticky topic chip
  // (#default@echo); while a chip is active parseInput() deliberately swallows
  // typed route prefixes, so the send would degrade to a plain non-flow
  // message and none of the flow scaffolding under test would ever render.
  await page.evaluate(() => clearTopicChip());

  await page.fill('#input', '#tst@echo>@echo1 hi');
  await page.keyboard.press('Enter');

  // Origin's thinking bubble appears once chat.start resolves.
  const originThinking = page.locator('.msg.assistant.msg-thinking[data-msg-id="100"]');
  await expect(originThinking).toBeVisible();

  const emit = frames => page.evaluate(list => {
    for (const frame of list) window.__webSocket.receive(frame);
  }, frames);

  // Origin streams, then completes.
  await emit([
    { v: 1, type: 'chat.text', event_id: 3, msg_id: 100, run_seq: 0, scope: { topic: 'tst', agent: 'echo' }, payload: { text: 'hi' } },
    { v: 1, type: 'message.changed', event_id: 4, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 100, role: 'assistant', status: 'done', content: 'hi', reply_to: 99 } },
    { v: 1, type: 'chat.done', event_id: 5, msg_id: 100, run_seq: 1, scope: { topic: 'tst', agent: 'echo' }, payload: {} },
  ]);

  // Origin completed bubble replaces the thinking bubble.
  const originBubble = page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)');
  await expect(originBubble).toBeVisible();

  // Server dispatches the chain step — touching the origin row emits a
  // spurious out-of-order message.changed(pending) for it (observed on a real
  // server; see mergeSparse's comment in ui/transcript-store.js), which
  // re-dirties the origin's id in the store even though the patch is rejected.
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

  // Chain step streams, then completes. Its registered completion runs a full
  // reconcile() — the pass that used to drag the origin's bubble to the top.
  await emit([
    { v: 1, type: 'chat.text', event_id: 10, msg_id: 101, run_seq: 0, scope: { topic: 'tst', agent: 'echo1' }, payload: { text: 'hi' } },
    { v: 1, type: 'message.changed', event_id: 11, msg_id: 101, scope: { topic: 'tst', agent: 'echo1' },
      payload: { id: 101, role: 'assistant', status: 'done', content: 'hi', reply_to: 102 } },
    { v: 1, type: 'chat.done', event_id: 12, msg_id: 101, run_seq: 1, scope: { topic: 'tst', agent: 'echo1' }, payload: {} },
  ]);

  const stepBubble = page.locator('.msg.assistant.history-item[data-msg-id="101"]:not(.msg-thinking)');
  await expect(stepBubble).toBeVisible();

  // The origin turn must still be at the live edge: both flow bubbles are the
  // last two history items, in order, below the older history.
  await expect(originBubble).toBeVisible();
  const ids = await page.locator('#messages > .msg.assistant.history-item[data-msg-id]').evaluateAll(
    nodes => nodes.map(n => n.dataset.msgId),
  );
  expect(ids).toEqual(['10', '11', '100', '101']);

  // The sender tab's own scaffolding must survive both completions: the
  // standalone user prompt bubble, the flow divider the composer placed, and
  // a date divider introducing the chain.
  await expect(page.locator('.msg.user', { hasText: '#tst@echo>@echo1 hi' })).toBeVisible();
  await expect(page.locator('.route-chain-marker[data-flow-route]')).toHaveCount(1);
  const senderDividers = await page.locator('#messages .date-divider').evaluateAll(
    nodes => nodes.map(n => n.textContent.trim()),
  );
  expect(senderDividers.length).toBe(2);
  const flowGroupIsContiguous = await page.evaluate(() => {
    const marker = document.querySelector('.route-chain-marker[data-flow-route]');
    const user = document.querySelector('.msg.user');
    const origin = document.querySelector('.msg.assistant.history-item[data-msg-id="100"]');
    if (!marker || !user || !origin) return false;
    return marker.previousElementSibling?.classList.contains('date-divider')
      && marker.nextElementSibling === user
      && user.nextElementSibling?.classList.contains('msg-time')
      && user.nextElementSibling.nextElementSibling === origin;
  });
  expect(flowGroupIsContiguous).toBe(true);
});
