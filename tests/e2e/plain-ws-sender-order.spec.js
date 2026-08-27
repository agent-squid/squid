/**
 * Regression tests for the plain (non-flow) WS sender turn — the exact shape of
 * the user's reported failing messages (msg 14554 / 14560, no flow_route).
 *
 * A plain turn's composer prompt is a live-only breadcrumb: it must stay
 * visible while the turn is live, and once the turn completes it sorts by
 * completion time (ADR-0011). Two failure modes are covered here:
 *   1. The prompt disappearing when the turn transitions live → completed.
 *   2. Clock skew: a response that *finished* before its own prompt was
 *      submitted (client clock ahead of server) sorting *below* the prompt.
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
  await page.route('**/config/agents', r => r.fulfill({ json: [{ name: 'echo' }] }));
  await page.route('**/config/realtime', route => route.fulfill({ json: { transport: 'websocket' } }));
}

// Installs a MockWebSocket that, on chat.start, replays the user prompt and the
// assistant pending bubble before the command.result ack (the ordering the app
// must tolerate — see replacePendingWithStoredItem's fallback path).
async function installMockWebSocket(page) {
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
              payload: { id: 99, role: 'user', status: 'done', content: 'plain prompt keeps bubble' } });
            this.receive({ v: 1, type: 'message.changed', event_id: 2, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
              payload: { id: 100, role: 'assistant', status: 'pending', reply_to: 99 } });
            this.receive({ v: 1, type: 'command.result', request_id: frame.request_id,
              payload: { ok: true, msg_id: 100 } });
          });
        }
      }
      receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    window.WebSocket = MockWebSocket;
  });
}

const DONE_FRAMES = [
  { v: 1, type: 'chat.text', event_id: 3, msg_id: 100, run_seq: 0, scope: { topic: 'tst', agent: 'echo' }, payload: { text: 'hi' } },
  { v: 1, type: 'chat.stats', event_id: 4, msg_id: 100, run_seq: 1, scope: { topic: 'tst', agent: 'echo' },
    payload: { session_id: 's1', input_tokens: 10, output_tokens: 5, lookback: 0 } },
  { v: 1, type: 'message.changed', event_id: 5, msg_id: 100, scope: { topic: 'tst', agent: 'echo' },
    payload: { id: 100, role: 'assistant', status: 'done', content: 'hi', reply_to: 99 } },
  { v: 1, type: 'chat.done', event_id: 6, msg_id: 100, run_seq: 2, scope: { topic: 'tst', agent: 'echo' }, payload: {} },
];

test('plain WS sender keeps its user prompt visible live, then through done + chat.done', async ({ page }) => {
  await installMockWebSocket(page);
  await mockBackend(page);
  await page.route('**/chat/100/status', route => route.fulfill({ json: {
    id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
    status: 'done', prompt: 'plain prompt keeps bubble', content: 'hi',
    reply_to: 99, timestamp: NOW, completed_at: LATER, stats: {},
  } }));

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', 'plain prompt keeps bubble');
  await page.keyboard.press('Enter');

  // Live (pending) stage: the composer's user prompt must be visible alongside
  // the thinking bubble — this is the "user prompt is gone on live" symptom.
  await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="100"]')).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: 'plain prompt keeps bubble' })).toBeVisible();

  await page.evaluate(list => {
    for (const frame of list) window.__webSocket.receive(frame);
  }, DONE_FRAMES);

  // Wait for the completed→completed wave (chat.done → finish → reconcile) to settle.
  const originBubble = page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)');
  await expect(originBubble).toBeVisible();

  const userPrompt = page.locator('.msg.user', { hasText: 'plain prompt keeps bubble' });
  await expect(userPrompt).toBeVisible();
  await expect(userPrompt).toHaveCount(1);
});

test('plain WS sender sorts a clock-skewed response above its own prompt', async ({ page }) => {
  await installMockWebSocket(page);
  await mockBackend(page);

  // The response completed_at is *before* the prompt was submitted (server
  // clock behind the client), so the completed response must sort above it.
  const submitIso = '2026-08-23T05:20:00.000Z';
  const completeIso = '2026-08-23T05:19:00.000Z';
  let statusNow = 'pending';
  await page.route('**/chat/100/status', route => route.fulfill({ json: {
    id: 100, role: 'assistant', topic: 'tst', agent: 'echo', adhoc: false,
    status: statusNow, prompt: 'plain prompt keeps bubble', content: 'hi',
    reply_to: 99, timestamp: submitIso, completed_at: completeIso, stats: {},
  } }));

  await page.goto('/');
  await page.waitForFunction(() => window.__webSocket?.readyState === 1);
  await page.evaluate(() => clearTopicChip());
  await page.fill('#input', 'plain prompt keeps bubble');
  await page.keyboard.press('Enter');

  await expect(page.locator('.msg.assistant.msg-thinking[data-msg-id="100"]')).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: 'plain prompt keeps bubble' })).toBeVisible();

  statusNow = 'done';
  await page.evaluate(list => {
    for (const frame of list) window.__webSocket.receive(frame);
  }, DONE_FRAMES);

  await expect(page.locator('.msg.assistant.history-item[data-msg-id="100"]:not(.msg-thinking)')).toBeVisible();
  await expect(page.locator('.msg.user', { hasText: 'plain prompt keeps bubble' })).toBeVisible();

  const order = await page.evaluate(() => {
    const kids = [...document.querySelectorAll('#messages > *')];
    return {
      response: kids.findIndex(n => n.classList.contains('msg') && n.classList.contains('assistant') && !n.classList.contains('msg-thinking')),
      prompt: kids.findIndex(n => n.classList.contains('msg') && n.classList.contains('user')),
    };
  });
  expect(order.response).toBeLessThan(order.prompt);
});
