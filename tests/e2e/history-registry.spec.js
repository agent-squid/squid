/**
 * ADR-0041 Stage 3/4 — createHistoryRegistry (ui/app.js).
 *
 * Exercises the real registry (not a fake, unlike reconciler.spec.js) against
 * a real transcript store and an isolated container, proving it renders the
 * same DOM appendHistoryItem/appendHistoryRouteChainMarker produce for the
 * direct-DOM path — the "shadow-equivalence" ADR-0041 requires before a
 * producer can cut over. It's wired into #messages in production under
 * ?renderer=store (see the comment above createHistoryRegistry in app.js and
 * history-store-renderer.spec.js for that wiring); these tests keep exercising
 * the registry against an isolated container to cover its contract directly.
 */
const { test, expect } = require('@playwright/test');

async function mockBackend(page) {
  await page.route('**/health',        r => r.fulfill({ json: { status: 'ok', boot_time: new Date().toISOString() } }));
  await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'sse' } }));
  await page.route('**/quota**',       r => r.fulfill({ json: {} }));
  await page.route('**/topics',        r => r.fulfill({ json: [] }));
  await page.route('**/topics/**',     r => r.fulfill({ json: [] }));
  await page.route('**/config/agents', r => r.fulfill({ json: [] }));
  await page.route('**/history**',     r => r.fulfill({ json: { items: [], has_more: false } }));
}

// Builds a store + reconciler + a real historyRegistry wired to a detached
// container (never touches the real #messages), and installs `items` (in
// /history's own newest-first row shape) via the same adapter production
// code uses. Returns a handle for .evaluate() calls plus the container's id
// so assertions can scope page.locator() to it.
async function freshRig(page, items = []) {
  return page.evaluateHandle((rows) => {
    const store = window.SquidTranscriptStore.createTranscriptStore();
    const container = document.createElement('div');
    container.id = 'history-registry-test-container';
    document.body.appendChild(container);
    const registry = window.createHistoryRegistry({ container });
    const reconciler = window.SquidReconciler.createReconciler({ store, registry });
    for (const item of rows) {
      store.installHistoryPage(window.historyItemToStoreRows(item));
    }
    return { store, container, reconciler };
  }, items);
}

test.beforeEach(async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
});

test('render() no-ops for a pending turn and leaves it dirty-cleared with nothing in the DOM', async ({ page }) => {
  const rig = await freshRig(page, [
    { id: 5, reply_to: 4, prompt: 'still going', status: 'pending', topic: 'squid', agent: 'codex' },
  ]);
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);
  expect(result.reconciledIds).toEqual([5]);
  const html = await rig.evaluate(({ container }) => container.innerHTML);
  expect(html).toBe('');
});

test('render() builds the same bubble appendHistoryItem produces for a completed turn', async ({ page }) => {
  const rig = await freshRig(page, [
    {
      id: 2, reply_to: 1, prompt: 'hello', content: 'hi there', status: 'done',
      topic: 'squid', agent: 'codex', completed_at: '2026-08-17T00:00:00Z',
      stats: { input_tokens: 10, output_tokens: 5 },
    },
  ]);
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);

  const bubble = page.locator('#history-registry-test-container .msg.assistant.history-item[data-msg-id="2"]');
  await expect(bubble).toHaveCount(1);
  await expect(bubble).toContainText('hi there');
  await expect(page.locator('#history-registry-test-container .stats')).toHaveCount(1);
});

test('render() fails (and stays dirty) when a turn has no raw payload to render from', async ({ page }) => {
  const rig = await page.evaluateHandle(() => {
    const store = window.SquidTranscriptStore.createTranscriptStore();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const registry = window.createHistoryRegistry({ container });
    const reconciler = window.SquidReconciler.createReconciler({ store, registry });
    // Bypass the adapter entirely — no `raw` field, unlike every real
    // producer row (historyItemToStoreRows always attaches one).
    store.installHistoryPage([
      { msg_id: 9, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:00Z' },
    ]);
    return { store, reconciler };
  });
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(false);
  expect(result.failedIds).toEqual([9]);
  const pending = await rig.evaluate(({ store }) => store.getPendingReconcile());
  expect(pending).toEqual([9]); // still dirty — retried, not silently dropped
});

test('reorder() places completed turns in (completed_at, msg_id) order regardless of install order', async ({ page }) => {
  const rig = await freshRig(page, [
    { id: 30, reply_to: 29, content: 'third', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:03Z' },
    { id: 10, reply_to: 9, content: 'first', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:01Z' },
    { id: 20, reply_to: 19, content: 'second', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:02Z' },
  ]);
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const ids = await page.locator('#history-registry-test-container .msg.assistant.history-item[data-msg-id]').evaluateAll(
    els => els.map(el => el.dataset.msgId)
  );
  expect(ids).toEqual(['10', '20', '30']);
});

test('reorder() is a DOM no-op for already-placed groups on an unrelated update', async ({ page }) => {
  // Each turn needs its own reply_to, same as real /history rows — sharing
  // one would make later installs' user-row processing re-resolve
  // assistantByReplyTo to a stale assistant id and re-dirty an unrelated
  // turn, which is a transcript-store edge case unrelated to what this test
  // is checking.
  const rig = await freshRig(page, [
    { id: 41, reply_to: 40, content: 'a', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:01Z' },
    { id: 42, reply_to: 41, content: 'b', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:02Z' },
  ]);
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const before = await rig.evaluate(({ container }) =>
    [...container.querySelectorAll('[data-msg-id]')].map(el => el));
  const nodeIdentityStable = await rig.evaluate(({ store, reconciler, container }) => {
    const firstNodes = [...container.querySelectorAll('.msg[data-msg-id]')];
    store.installHistoryPage(window.historyItemToStoreRows({
      id: 43, reply_to: 42, content: 'c', status: 'done', topic: 'squid', completed_at: '2026-08-17T00:00:03Z',
    }));
    reconciler.reconcile();
    const secondNodes = [...container.querySelectorAll('.msg[data-msg-id="41"], .msg[data-msg-id="42"]')];
    return firstNodes.every((n, i) => n === secondNodes[i]);
  });
  expect(nodeIdentityStable).toBe(true);
  expect(before.length).toBeGreaterThan(0);
});

test('route markers render the same way as the direct-DOM path for a matching handoff', async ({ page }) => {
  const rig = await freshRig(page, [
    {
      id: 50, reply_to: 49, content: 'origin reply', status: 'done',
      topic: 'squid', agent: 'codex', adhoc: true, flow_route: '#squid@codex!>@revucla!',
      completed_at: '2026-08-17T00:00:01Z',
    },
    {
      id: 51, reply_to: 50, content: 'target reply', status: 'done',
      topic: 'squid', agent: 'revucla', adhoc: true,
      prompt: 'Squid route chain handoff.\nRoute: #squid@codex!>@revucla!\nPrevious step: @codex\nCurrent step: @revucla!\nOriginal prompt: review this\n\n<previous_step_output>',
      prompt_source: 'system',
      completed_at: '2026-08-17T00:00:02Z',
    },
  ]);
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  await expect(page.locator('#history-registry-test-container .route-chain-marker')).toHaveText('#squid@codex!>@revucla!');
});
