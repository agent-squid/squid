/**
 * ADR-0041 idempotent reconciler — engine invariant tests.
 * The reconciler (reconciler.js) is fully tested here against a store +
 * injected registry. HTTP history now renders through it via
 * createHistoryRegistry under ?renderer=store (see history-store-renderer.spec.js
 * for that production wiring); these tests still exercise window.SquidReconciler
 * directly, mirroring how transcript-store.spec.js exercises
 * window.SquidTranscriptStore, to cover the engine's invariants in isolation.
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

// Builds a fresh store + a recording fake registry inside the page, and
// hands back a handle whose .evaluate() calls run against that pair. The
// registry records render/reorder calls so assertions can check which turn
// groups were actually touched, not just the final store/DOM state.
async function freshRig(page) {
  return page.evaluateHandle(() => {
    const store = window.SquidTranscriptStore.createTranscriptStore();
    const calls = { render: [], reorder: [] };
    const failing = new Set();
    const registry = {
      render(turn, ctx) {
        calls.render.push({ id: turn.assistantMsgId, bucket: ctx.nextBucket, previousBucket: ctx.previousBucket });
        if (failing.has(turn.assistantMsgId)) throw new Error('simulated render failure');
        return { id: turn.assistantMsgId, bucket: ctx.nextBucket, content: turn.stats?.content ?? null, rev: (ctx.previousGroup?.rev ?? 0) + 1 };
      },
      reorder(order, groups) {
        calls.reorder.push({ completed: [...order.completed], pending: [...order.pending], groupIds: [...groups.keys()] });
      },
    };
    const reconciler = window.SquidReconciler.createReconciler({ store, registry });
    return { store, reconciler, calls, failing };
  });
}

test.beforeEach(async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
});

test('createReconciler rejects a registry missing render or reorder', async ({ page }) => {
  const err = await page.evaluate(() => {
    const store = window.SquidTranscriptStore.createTranscriptStore();
    try {
      window.SquidReconciler.createReconciler({ store, registry: { render: () => {} } });
      return null;
    } catch (e) {
      return e.message;
    }
  });
  expect(err).toMatch(/registry must implement/);
});

test('reconcile renders every dirty turn once and clears them from pendingReconcile', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => {
    store.installHistoryPage([
      { msg_id: 1, role: 'user', content: 'hi' },
      { msg_id: 2, role: 'assistant', reply_to: 1, status: 'done', completed_at: '2026-08-17T00:00:00Z' },
    ]);
  });
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);
  expect(result.reconciledIds).toEqual([2]);

  const pending = await rig.evaluate(({ store }) => store.getPendingReconcile());
  expect(pending).toEqual([]);
});

test('stable registry identity: the same assistant msg_id keeps one group across repeated updates', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.applyRunEvent(9, 0, 'text', { delta: 'a' }, 1));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const first = await rig.evaluate(({ reconciler }) => reconciler.getGroup(9));

  await rig.evaluate(({ store }) => store.applyRunEvent(9, 1, 'text', { delta: 'b' }, 2));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const second = await rig.evaluate(({ reconciler }) => reconciler.getGroup(9));

  expect(first.rev).toBe(1);
  expect(second.rev).toBe(2); // rebuilt from the previous group, not a fresh identity
  const groupCount = await rig.evaluate(({ reconciler }) => reconciler.getGroupCount());
  expect(groupCount).toBe(1);
});

test('atomic live-to-terminal replacement: the registry sees one swap, not two visible states', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.applyMessagePatch(3, { role: 'assistant', status: 'running', queued_at: '2026-08-17T00:00:00Z' }, 1));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  await rig.evaluate(({ store }) => store.applyMessagePatch(3, { status: 'done', completed_at: '2026-08-17T00:01:00Z' }, 2));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());

  const renderCalls = await rig.evaluate(({ calls }) => calls.render.filter(c => c.id === 3));
  expect(renderCalls).toEqual([
    { id: 3, bucket: 'pending', previousBucket: null },
    { id: 3, bucket: 'completed', previousBucket: 'pending' },
  ]);
  const group = await rig.evaluate(({ reconciler }) => reconciler.getGroup(3));
  expect(group.bucket).toBe('completed');
});

test('reorder places groups by store order and is a placement pass, not a re-render, for untouched groups', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 21, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:02Z' },
    { msg_id: 22, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:01Z' },
  ]));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const firstReorder = await rig.evaluate(({ calls }) => calls.reorder.at(-1));
  expect(firstReorder.completed).toEqual([22, 21]);

  // A second page merge that only dirties a new id must not re-render 21/22.
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 23, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:03Z' },
  ], { offset: 1 }));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const renderIds = await rig.evaluate(({ calls }) => calls.render.map(c => c.id));
  expect(renderIds).toEqual([21, 22, 23]); // dirty-set order, not completion order; 21/22 rendered only in the first pass
  const secondReorder = await rig.evaluate(({ calls }) => calls.reorder.at(-1));
  expect(secondReorder.completed).toEqual([22, 21, 23]);
  expect(secondReorder.groupIds.sort((a, b) => a - b)).toEqual([21, 22, 23]);
});

test('a simulated render failure leaves that id dirty for retry and does not ack the cursor', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 31, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:01Z' },
    { msg_id: 32, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:02Z' },
  ]));
  await rig.evaluate(({ failing }) => failing.add(32));

  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcileAndAck(() => {}));
  expect(result.ok).toBe(false);
  expect(result.reconciledIds).toEqual([31]);
  expect(result.failedIds).toEqual([32]);

  const dirty = await rig.evaluate(({ store }) => store.getPendingReconcile());
  expect(dirty).toEqual([32]);

  // Clear the injected failure and retry: the previously-failed id reconciles,
  // and only now does the ack callback fire.
  await rig.evaluate(({ failing }) => failing.delete(32));
  const finalResult = await rig.evaluate(({ reconciler }) => reconciler.reconcileAndAck(() => { window.__acked = true; }));
  expect(finalResult.ok).toBe(true);
  expect(finalResult.reconciledIds).toEqual([32]);
  const ackFired = await rig.evaluate(() => window.__acked === true);
  expect(ackFired).toBe(true);
});

test('a transiently-failed render does not hand its stale group to reorder', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.applyMessagePatch(60, { role: 'assistant', status: 'running', queued_at: '2026-08-17T00:00:00Z' }, 1));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());

  // 60 goes terminal but its render fails this pass; 61 succeeds.
  await rig.evaluate(({ failing }) => failing.add(60));
  await rig.evaluate(({ store }) => {
    store.applyMessagePatch(60, { role: 'assistant', status: 'done', completed_at: '2026-08-17T00:01:00Z' }, 2);
    store.applyMessagePatch(61, { role: 'assistant', status: 'done', completed_at: '2026-08-17T00:02:00Z' }, 3);
  });
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(false);
  expect(result.failedIds).toEqual([60]);

  const lastReorder = await rig.evaluate(({ calls }) => calls.reorder.at(-1));
  // The store's order already lists 60 as completed, but its previous
  // (stale pending) group must not be handed to reorder for placement.
  expect(lastReorder.completed).toContain(60);
  expect(lastReorder.groupIds).not.toContain(60);
});

test('pagination merge does not lose or re-touch groups outside the newly dirty ids (no active-window loss)', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.installSnapshot({ messages: [
    { msg_id: 40, role: 'assistant', status: 'running' },
  ] }, 1));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroupCount())).toBe(1);

  // A later history page brings in an unrelated older id; the live group's
  // own render must not be invoked again by that merge.
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 41, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:00Z' },
  ]));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());

  const renderIdsFor40 = await rig.evaluate(({ calls }) => calls.render.filter(c => c.id === 40).length);
  expect(renderIdsFor40).toBe(1);
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroup(40).bucket)).toBe('pending');
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroup(41).bucket)).toBe('completed');
});

test('reset() forgets registry identity so a caller-owned DOM wipe does not get silently reattached', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 70, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:00Z' },
  ]));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroupCount())).toBe(1);

  // Simulate a caller (reloadHistory/resetHistoryToLatest/jumpToMessage)
  // that resets its own DOM region outside the reconciler: reset() first,
  // matching the documented contract, then a page reload that never
  // re-installs id 70 (it fell outside the new filter/window).
  await rig.evaluate(({ reconciler }) => reconciler.reset());
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroupCount())).toBe(0);
  expect(await rig.evaluate(({ reconciler }) => reconciler.getGroup(70))).toBeUndefined();

  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 80, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:01:00Z' },
  ]));
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  // Only the newly-installed id is dirty — 70 was already clean before reset()
  // ran (its earlier reconcile() cleared it), so this alone doesn't exercise
  // reset()'s own dirty-clearing; see the next test for that.
  expect(result.reconciledIds).toEqual([80]);
  const lastReorder = await rig.evaluate(({ calls }) => calls.reorder.at(-1));
  // The store still knows about 70 (reset() is registry-identity-only, not
  // store eviction) so it's still in the order — but with no up-to-date
  // group for it post-reset, the registry has nothing to place for it.
  expect(lastReorder.completed).toEqual([70, 80]);
  expect(lastReorder.groupIds).toEqual([80]);
});

test('reset() also clears the store dirty set, so an id installed but never reconciled cannot resurface after a reset', async ({ page }) => {
  const rig = await freshRig(page);
  // Installed but deliberately never reconciled — mirrors a producer that
  // feeds the store without always rendering from it (e.g. this app's
  // prompt-only history mode, which still calls shadowInstallHistoryPage but
  // skips reconcile()). 90 sits dirty with no group ever rendered for it.
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 90, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:00Z' },
  ]));
  expect(await rig.evaluate(({ store }) => store.getPendingReconcile())).toEqual([90]);

  // A filter/scope change resets before installing the new scope's data —
  // same call order as reloadHistory/resetHistoryToLatest/jumpToMessage.
  await rig.evaluate(({ reconciler }) => reconciler.reset());
  expect(await rig.evaluate(({ store }) => store.getPendingReconcile())).toEqual([]);

  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 91, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:01:00Z' },
  ]));
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  // Without the fix, 90 would still be dirty here and get rendered alongside
  // 91 — resurrecting a turn from the scope the reset was meant to clear,
  // even though it was never actually shown even once.
  expect(result.reconciledIds).toEqual([91]);
  const renderIds = await rig.evaluate(({ calls }) => calls.render.map(c => c.id));
  expect(renderIds).not.toContain(90);
});

test('reposition() re-places already-rendered groups without requiring a dirty id', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ store }) => store.installHistoryPage([
    { msg_id: 90, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:00Z' },
  ]));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  const reorderCountAfterReconcile = await rig.evaluate(({ calls }) => calls.reorder.length);

  // Nothing in the store changed — reconcile() must not call reorder() again
  // (its dirty set is empty), matching the bug this exists to fix: a
  // still-unmigrated producer mutating #messages outside the store (e.g. a
  // new live group appearing) never dirties anything, so reconcile() alone
  // can't be what re-evaluates a stale placement decision.
  const reconcileResult = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(reconcileResult.reconciledIds).toEqual([]);
  expect(await rig.evaluate(({ calls }) => calls.reorder.length)).toBe(reorderCountAfterReconcile);

  // reposition() forces the placement pass anyway, without touching render.
  const renderCountBefore = await rig.evaluate(({ calls }) => calls.render.length);
  await rig.evaluate(({ reconciler }) => reconciler.reposition());
  expect(await rig.evaluate(({ calls }) => calls.render.length)).toBe(renderCountBefore); // no re-render
  const lastReorder = await rig.evaluate(({ calls }) => calls.reorder.at(-1));
  expect(lastReorder.completed).toEqual([90]);
  expect(lastReorder.groupIds).toEqual([90]);
});
