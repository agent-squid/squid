// Share connection-health fixtures between HTTP metadata callers and WS probes.
// The production WS probe never forwards through HTTP.
const handlers = new WeakMap();
async function routeHealth(page, handler) {
  handlers.set(page, handler);
  await page.route('**/health', handler);
}
async function respondHealth(page, ws, frame) {
  let payload = { ok: true, status: 'ok' };
  const handler = handlers.get(page);
  if (handler) {
    await handler({
      fulfill: async ({ json, status = 200 }) => {
        payload = status < 400 ? { ok: true, ...json } : { ok: false, error: 'health unavailable' };
      },
      abort: async () => { payload = { ok: false, error: 'health unavailable' }; },
    });
  }
  try { ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id, payload })); }
  catch { /* The test can disconnect while a deferred health reply is pending. */ }
}
async function installConnectionSocket(page) {
  await page.routeWebSocket('**/ws/v1', ws => {
    ws.onMessage(async raw => {
      const frame = JSON.parse(raw);
      if (frame.type === 'subscribe') ws.send(JSON.stringify({ v: 1, type: 'subscribed', payload: {} }));
      else if (frame.type === 'health.check') await respondHealth(page, ws, frame);
      else if (frame.type === 'history.check') {
        const items = await page.evaluate(ids => [...messages.querySelectorAll(':scope > .history-item[data-msg-id]')]
          .filter(row => ids.includes(Number(row.dataset.msgId)) && row._historySnapshot)
          .map(row => JSON.parse(row._historySnapshot)), frame.payload.ids);
        ws.send(JSON.stringify({ v: 1, type: 'command.result', request_id: frame.request_id, payload: { ok: true, items } }));
      }
    });
    ws.send(JSON.stringify({ v: 1, type: 'hello', payload: { heartbeat_seconds: 30 } }));
  });
}
module.exports = { routeHealth, respondHealth, installConnectionSocket };
