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

// Pending-turn *rendering* is still direct-DOM (reconnectPendingItem builds
// and live-mutates the wip bubble) — but render() now adopts whatever node
// that path already built as the turn's registered group, real bookkeeping
// identity the eventual live-to-terminal cutover needs (ctx.previousBucket/
// nextBucket). Adoption must reuse the *same* node across repeated
// reconcile() passes (a text delta can dirty this id many times a second
// while streaming) rather than re-querying and never rebuild it.
test('render() adopts an already-on-screen wip bubble for a pending turn, and reuses it across passes', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ container }) => {
    const bubble = document.createElement('div');
    bubble.className = 'msg assistant msg-thinking history-item';
    bubble.dataset.msgId = '80';
    bubble.dataset.testMarker = 'original';
    container.appendChild(bubble);
  });
  await rig.evaluate(({ store }) => store.installHistoryPage(window.historyItemToStoreRows({
    id: 80, reply_to: 79, prompt: 'still going', status: 'pending', topic: 'squid', agent: 'codex',
  })));
  const first = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(first.ok).toBe(true);

  const bubbles = page.locator('#history-registry-test-container .msg-thinking[data-msg-id="80"]');
  await expect(bubbles).toHaveCount(1);
  await expect(bubbles).toHaveAttribute('data-test-marker', 'original');

  // A second, unrelated dirty pass for the same id must not re-query or
  // rebuild — same node identity, proven via the marker attribute surviving
  // a live mutation reconnectPendingItem would be doing to this same node.
  const stableAcrossPasses = await rig.evaluate(({ store, reconciler, container }) => {
    const before = container.querySelector('[data-msg-id="80"]');
    before.dataset.testMarker = 'mutated-live';
    store.applyRunEvent(80, 1, 'text', { delta: 'partial' });
    reconciler.reconcile();
    return container.querySelector('[data-msg-id="80"]') === before;
  });
  expect(stableAcrossPasses).toBe(true);
  await expect(bubbles).toHaveAttribute('data-test-marker', 'mutated-live');
});

// Once pending turns are adopted (above), a turn that just finished has a
// non-null but stale, pending-shaped ctx.previousGroup — gating the
// completed-branch adoption search on `!ctx.previousGroup` (as it used to
// be) would wrongly skip it on exactly this transition, reintroducing the
// duplicate-bubble bug the completed-turn adoption fix closed, only for
// turns that pass through a pending state first (i.e. nearly all of them).
test('render() adopts the real completed node on a pending->completed transition, not a stale wip bubble', async ({ page }) => {
  const rig = await freshRig(page);
  await rig.evaluate(({ container }) => {
    const wip = document.createElement('div');
    wip.className = 'msg assistant msg-thinking history-item';
    wip.dataset.msgId = '90';
    container.appendChild(wip);
  });
  await rig.evaluate(({ store }) => store.installHistoryPage(window.historyItemToStoreRows({
    id: 90, reply_to: 89, prompt: 'still going', status: 'pending', topic: 'squid', agent: 'codex',
  })));
  await rig.evaluate(({ reconciler }) => reconciler.reconcile()); // adopts the wip bubble as pending

  // Simulate replacePendingWithStoredItem: remove the wip bubble, insert the
  // real completed node directly (bypassing the reconciler), same as
  // insertCompletedHistoryItem does in production.
  await rig.evaluate(({ container }) => {
    container.querySelector('.msg-thinking[data-msg-id="90"]').remove();
    const done = document.createElement('div');
    done.className = 'msg assistant history-item';
    done.dataset.msgId = '90';
    done.dataset.testMarker = 'real-completed-node';
    done.textContent = 'finished';
    container.appendChild(done);
  });
  await rig.evaluate(({ store }) => store.installHistoryPage(window.historyItemToStoreRows({
    id: 90, reply_to: 89, prompt: 'still going', content: 'finished', status: 'done',
    topic: 'squid', agent: 'codex', completed_at: '2026-08-21T00:00:00Z',
  })));
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);

  const nodes = page.locator('#history-registry-test-container [data-msg-id="90"]');
  await expect(nodes).toHaveCount(1); // not duplicated
  await expect(nodes).toHaveAttribute('data-test-marker', 'real-completed-node'); // adopted the real node, not the stale wip bubble
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

// A completed turn can already be on screen the first time the reconciler
// ever sees its id dirty: realtime discovery, flow-step attach, and the
// pending->completed swap all render directly via insertCompletedHistoryItem,
// bypassing the reconciler entirely — and shadowInstallHistoryPage/shadow
// run-event applies dirty a turn's id unconditionally, with no "is it
// already on screen" check of their own. Without render() checking first,
// this reconcile() would build and insert a second, duplicate bubble for a
// turn the reconciler never rendered itself but the page already shows.
test('render() adopts an already-on-screen node instead of duplicating it, the first time an id goes dirty', async ({ page }) => {
  const rig = await freshRig(page);
  // Simulate a node inserted outside the reconciler (e.g. discoverRealtimeTurn),
  // with the exact class/attribute shape appendHistoryItem produces, marked
  // so a later query can prove it's still the *same* node, not a rebuild.
  await rig.evaluate(({ container }) => {
    const bubble = document.createElement('div');
    bubble.className = 'msg assistant history-item';
    bubble.dataset.msgId = '70';
    bubble.dataset.testMarker = 'original';
    bubble.textContent = 'directly inserted';
    container.appendChild(bubble);
  });
  await rig.evaluate(({ store }) => store.installHistoryPage(window.historyItemToStoreRows({
    id: 70, reply_to: 69, prompt: 'hi', content: 'directly inserted', status: 'done',
    topic: 'squid', agent: 'codex', completed_at: '2026-08-17T00:00:00Z',
  })));
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);

  const bubbles = page.locator('#history-registry-test-container .msg.assistant.history-item[data-msg-id="70"]');
  await expect(bubbles).toHaveCount(1); // not duplicated
  await expect(bubbles).toHaveAttribute('data-test-marker', 'original'); // adopted, not rebuilt
});

// appendHistoryItem/insertCompletedHistoryItem never produce just a bubble —
// a stats/timestamp/footer sibling and any tool-block-history siblings are
// flat #messages children right after it (this file's own header comment on
// createHistoryRegistry). Adopting only the bubble (the bug the fix above
// closes) would let reorder() move that one-node group alone, stranding the
// siblings at their original position and splitting the turn — undetectable
// by the single-bubble adoption test above, since it has no sibling to lose.
// Caught by #squid@codex review before publish.
test("render() adopts a completed turn's full sibling range, so reorder() moves stats/tool-block siblings together with the bubble", async ({ page }) => {
  const rig = await freshRig(page, [
    {
      id: 100, reply_to: 99, content: 'newer', status: 'done', topic: 'squid',
      completed_at: '2026-08-17T00:00:05Z', stats: { input_tokens: 1, output_tokens: 1 },
    },
  ]);
  await rig.evaluate(({ reconciler }) => reconciler.reconcile()); // builds turn 100 normally: [bubble, stats]

  // Simulate a bypass insert (e.g. attachFlowStep) for an *older* turn,
  // landing at the wrong end-of-container position — same flat shape
  // appendHistoryItem/insertCompletedHistoryItem produce: bubble, then a
  // `.stats` sibling, then a `.tool-block-history` sibling carrying a
  // *different* msg_id (mirroring a real worktree-blocker tool block, which
  // targets an earlier turn's id, not this one's — adoption can't rely on
  // sibling identity matching this turn's own msg_id).
  await rig.evaluate(({ container }) => {
    const bubble = document.createElement('div');
    bubble.className = 'msg assistant history-item';
    bubble.dataset.msgId = '90';
    bubble.dataset.testMarker = 'bubble-90';
    const stats = document.createElement('div');
    stats.className = 'stats history-item';
    stats.dataset.testMarker = 'stats-90';
    const toolBlock = document.createElement('div');
    toolBlock.className = 'tool-block history-item tool-block-history';
    toolBlock.dataset.msgId = '45'; // foreign msg_id, same as a real worktree-blocker block
    toolBlock.dataset.testMarker = 'tool-90';
    container.append(bubble, stats, toolBlock);
  });
  await rig.evaluate(({ store }) => store.installHistoryPage(window.historyItemToStoreRows({
    id: 90, reply_to: 89, content: 'older', status: 'done', topic: 'squid',
    completed_at: '2026-08-17T00:00:01Z', stats: { input_tokens: 1, output_tokens: 1 },
  })));
  const result = await rig.evaluate(({ reconciler }) => reconciler.reconcile());
  expect(result.ok).toBe(true);

  // Turn 90 sorts before turn 100 (earlier completed_at) — reorder() must
  // move it there, which physically moves DOM nodes since it landed at the
  // end above. If adoption had only grabbed the bubble, only 'bubble-90'
  // would move, leaving 'stats-90'/'tool-90' stranded after turn 100.
  const summary = await rig.evaluate(({ container }) =>
    [...container.children].map(el => ({ key: el.dataset.testMarker || el.dataset.msgId || null, cls: el.className })));
  expect(summary.map(s => s.key)).toEqual(['bubble-90', 'stats-90', 'tool-90', '100', null]);
  expect(summary[4].cls).toContain('stats'); // turn 100's own real stats sibling, untouched
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
