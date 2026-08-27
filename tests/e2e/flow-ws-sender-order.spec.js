/**
 * Regression test: the real server emits chat.stats BEFORE message.changed(done)
 * and chat.done (see topic_queue.py — stats chunk, then update_assistant_message,
 * then insert_run_event("done")). Reproduce that exact frame order for the sender's
 * own flow origin and assert the standalone user prompt survives completion.
 */
const { test, expect } = require('@playwright/test');

const NOW = new Date().toISOString();
const LATER = new Date(Date.now() + 1000).toISOString();

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [
    { name: 'tst', agent: 'echo', last_model: null, last_backend: 'echo', queue_depth: 0, active: false, last_prompt: 'hi' },
  ] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'echo' }, { name: 'echo1' }] }));
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
}

const ORIGIN_DONE = {
  id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
  status: 'done', prompt: '#tst@echo>@echo1 hi', content: 'hi',
  reply_to: 99,
  flow_route: '#tst@echo>@echo1', flow_run_id: 'run-1',
  timestamp: NOW, completed_at: LATER, stats: {},
};

test('real server frame order keeps the sender flow origin prompt', async ({ page }) => {
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
  await page.route('**/chat/100/status', route => route.fulfill({ json: ORIGIN_DONE }));

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', '#tst@echo>@echo1 hi');
  await page.keyboard.press('Enter');

  const originThinking = page.locator('.msg.assistant.msg-thinking[data-msg-id="100"]');
  await expect(originThinking).toBeVisible();

  const emit = frames => page.evaluate(list => {
    for (const frame of list) window.__webSocket.receive(frame);
  }, frames);

  // Real order: text → stats → message.changed(done) → chat.done.
  await emit([
    { v: 1, type: 'chat.text', event_id: 3, msg_id: 100, run_seq: 0, scope: { topic: 'tst', agent: 'echo' }, payload: { text: 'hi' } },
    { v: 1, type: 'chat.stats', event_id: 4, msg_id: 100, run_seq: 1, scope: { topic: 'tst', agent: 'echo' },
      payload: { session_id: 's1', input_tokens: 10, output_tokens: 5, lookback: 0 } },
    { v: 1, type: 'message.changed', event_id: 5, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
      payload: { id: 100, role: 'assistant', status: 'done', content: 'hi', reply_to: 99 } },
    { v: 1, type: 'chat.done', event_id: 6, msg_id: 100, run_seq: 2, scope: { topic: 'tst', agent: 'echo' }, payload: {} },
  ]);

  const originBubble = page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)');
  await expect(originBubble).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: '#tst@echo>@echo1 hi' })).toBeVisible();
  await expect(page.locator('.route-chain-marker[data-flow-route]')).toHaveCount(1);
});
