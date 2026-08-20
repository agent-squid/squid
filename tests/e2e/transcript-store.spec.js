/**
 * ADR-0041 normalized transcript store — reducer invariant tests.
 * The HTTP-history producer feeds the store via shadowInstallHistoryPage in
 * app.js (see transcript-store.js's header); these tests still exercise
 * window.SquidTranscriptStore directly rather than through the live UI, to
 * cover the reducer's invariants in isolation.
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

async function freshStore(page) {
  return page.evaluateHandle(() => window.SquidTranscriptStore.createTranscriptStore());
}

test.beforeEach(async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
});

test('installHistoryPage merges rows and records a boundary without touching other pages', async ({ page }) => {
  const store = await freshStore(page);
  const result = await store.evaluate((s, rows) => s.installHistoryPage(rows, { before: 100 }), [
    { msg_id: 1, role: 'user', content: 'hi' },
    { msg_id: 2, role: 'assistant', reply_to: 1, status: 'done', completed_at: '2026-08-17T00:00:00Z' },
  ]);
  expect(result.ok).toBe(true);

  const turn = await store.evaluate(s => s.getTurn(2));
  expect(turn.promptMsgId).toBe(1);
  expect(turn.status).toBe('done');

  const view = await store.evaluate(s => s.getView());
  expect(view.pageBoundaries).toEqual([{ before: 100 }]);
  expect(view.loadedMessageIds.sort((a, b) => a - b)).toEqual([1, 2]);
});

test('installSnapshot below the current watermark is a no-op that still returns dirty ids', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.installSnapshot({ messages: [{ msg_id: 5, role: 'assistant', status: 'pending' }] }, 10));
  expect(await store.evaluate(s => s.getLastAppliedEventId())).toBe(10);

  const stale = await store.evaluate(s => s.installSnapshot({ messages: [{ msg_id: 5, role: 'assistant', status: 'error' }] }, 3));
  expect(stale.ok).toBe(true);
  expect(stale.noop).toBe(true);
  // status must be unchanged by the stale snapshot
  expect((await store.evaluate(s => s.getMessage(5))).status).toBe('pending');
  // outstanding dirty id from the first (real) transaction is still surfaced
  expect(stale.dirty).toContain(5);
});

test('a terminal status is monotonic: a duplicate or older pending patch cannot reopen it', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(7, 0, 'status', { status: 'done' }, 1));
  expect((await store.evaluate(s => s.getMessage(7))).status).toBe('done');

  await store.evaluate(s => s.applyMessagePatch(7, { status: 'pending' }, 2));
  expect((await store.evaluate(s => s.getMessage(7))).status).toBe('done');
});

// A run-event-sourced message (text/tool/stats deltas) has no status field at
// all until a 'status' event or lifecycle patch reports one — that "unknown"
// must not be misread as terminal, or isTerminal()'s monotonicity guard
// silently drops the real 'running' status that arrives right after (ADR-0041
// Gap 1's fix normalizes a missing status to terminal only at the HTTP-history
// producer's own edge, not in this shared classifier — see historyItemToStoreRows).
test('a status-less streamed message accepts a later running status, then a real terminal one', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(9, 0, 'text', { delta: 'hello' }, 1));
  expect((await store.evaluate(s => s.getMessage(9))).status).toBeUndefined();
  expect(await store.evaluate(s => s.isTerminal(s.getTurn(9).status))).toBe(false);

  await store.evaluate(s => s.applyMessagePatch(9, { status: 'running' }, 2));
  expect((await store.evaluate(s => s.getMessage(9))).status).toBe('running');

  await store.evaluate(s => s.applyMessagePatch(9, { status: 'done', completed_at: '2026-08-20T00:00:00Z' }, 3));
  expect((await store.evaluate(s => s.getMessage(9))).status).toBe('done');
});

test('sparse run-event patches never erase fields they omit', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(9, 0, 'text', { delta: 'hello ' }, 1));
  await store.evaluate(s => s.applyRunEvent(9, 1, 'stats', { input_tokens: 5 }, 2));
  const msg = await store.evaluate(s => s.getMessage(9));
  expect(msg.content).toBe('hello ');
  expect(msg.stats.input_tokens).toBe(5);

  await store.evaluate(s => s.applyRunEvent(9, 2, 'text', { delta: 'world' }, 3));
  const msg2 = await store.evaluate(s => s.getMessage(9));
  expect(msg2.content).toBe('hello world');
  // stats field from the earlier patch must still be present
  expect(msg2.stats.input_tokens).toBe(5);
});

test('event_id at or below the watermark is a no-op; run_seq at or below its own watermark is a no-op', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(11, 5, 'text', { delta: 'a' }, 100));
  expect((await store.evaluate(s => s.getMessage(11))).content).toBe('a');

  // Duplicate/older event_id: dropped entirely.
  const dupEvent = await store.evaluate(s => s.applyRunEvent(11, 6, 'text', { delta: 'b' }, 100));
  expect(dupEvent.noop).toBe(true);
  expect((await store.evaluate(s => s.getMessage(11))).content).toBe('a');

  // New event_id but an older/equal run_seq for this message: payload is a no-op,
  // but the event_id watermark still advances.
  const dupRunSeq = await store.evaluate(s => s.applyRunEvent(11, 5, 'text', { delta: 'c' }, 101));
  expect(dupRunSeq.noop).toBe(true);
  expect((await store.evaluate(s => s.getMessage(11))).content).toBe('a');
  expect(await store.evaluate(s => s.getLastAppliedEventId())).toBe(101);
});

test('completed turns order by (completed_at, msg_id); pending turns keep creation order and are not mixed in', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.installHistoryPage([
    { msg_id: 21, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:02Z' },
    { msg_id: 22, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:01Z' },
    { msg_id: 23, role: 'assistant', status: 'done', completed_at: '2026-08-17T00:00:01Z' }, // tiebreak by msg_id
  ]));
  await store.evaluate(s => s.applyMessagePatch(30, { role: 'assistant', status: 'pending', queued_at: '2026-08-17T00:00:00Z' }, 1));
  await store.evaluate(s => s.applyMessagePatch(29, { role: 'assistant', status: 'pending', queued_at: '2026-08-17T00:00:00Z' }, 2));

  const order = await store.evaluate(s => s.getOrderedTurnIds());
  expect(order.completed).toEqual([22, 23, 21]);
  expect(order.pending).toEqual([29, 30]);
});

test('an identity conflict (role/reply_to mismatch) fails the action before mutating state', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.installHistoryPage([{ msg_id: 40, role: 'user', content: 'hi' }]));
  const result = await store.evaluate(s => s.applyMessagePatch(40, { role: 'assistant' }, 1));
  expect(result.ok).toBe(false);
  expect(await store.evaluate(s => s.getLastAppliedEventId())).toBe(0);
  expect((await store.evaluate(s => s.getMessage(40))).role).toBe('user');
});

test('pendingReconcile accumulates dirty assistant ids and clearReconciled removes only what is passed', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(50, 0, 'text', { delta: 'x' }, 1));
  await store.evaluate(s => s.applyRunEvent(51, 0, 'text', { delta: 'y' }, 2));
  expect((await store.evaluate(s => s.getPendingReconcile())).sort((a, b) => a - b)).toEqual([50, 51]);

  await store.evaluate(s => s.clearReconciled([50]));
  expect(await store.evaluate(s => s.getPendingReconcile())).toEqual([51]);
});

// ── Stage 2: HTTP history producer, shadow mode ─────────────────────────────
// app.js now feeds real /history pages into window.__transcriptStore
// alongside its existing direct-DOM render. These tests drive the live page
// (not a fresh detached store) so the same fetched page produces both the
// DOM and the store state, and check the two stay in parity without a
// second render.

test.describe('HTTP history producer (shadow mode)', () => {
  test('a history page renders the DOM as before and installs equivalent turns into the store', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [
        {
          id: 6, role: 'assistant', reply_to: 5, topic: 'default', agent: 'claude',
          adhoc: false, status: 'done', prompt: "what's your model?", content: 'Done 6',
          completed_at: '2026-08-15T12:00:00Z',
        },
        {
          id: 4, role: 'assistant', reply_to: 3, topic: 'default', agent: 'claude',
          adhoc: false, status: 'error', prompt: 'earlier prompt', content: 'boom',
          completed_at: '2026-08-15T11:00:00Z',
        },
      ],
      has_more: false,
    }}));
    await page.reload();

    // Direct-DOM path is unaffected: both turns render exactly once each.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="6"]')).toHaveCount(1);
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="4"]')).toHaveCount(1);

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return {
        turn6: s.getTurn(6),
        turn4: s.getTurn(4),
        prompt5: s.getMessage(5),
        prompt3: s.getMessage(3),
        order: s.getOrderedTurnIds(),
        boundaries: s.getView().pageBoundaries,
      };
    });

    expect(snapshot.turn6.status).toBe('done');
    expect(snapshot.turn6.promptMsgId).toBe(5);
    expect(snapshot.turn6.promptContent).toBe("what's your model?");
    expect(snapshot.turn4.status).toBe('error');
    expect(snapshot.turn4.promptMsgId).toBe(3);
    expect(snapshot.prompt5).toMatchObject({ role: 'user', content: "what's your model?" });
    expect(snapshot.prompt3).toMatchObject({ role: 'user', content: 'earlier prompt' });
    // (completed_at, msg_id) ordering matches the DOM's chronological order.
    expect(snapshot.order.completed).toEqual([4, 6]);
    expect(snapshot.boundaries).toEqual([{ kind: 'offset', offset: 0, hasMore: false }]);
  });

  test('a pending history row installs into the store as a non-terminal turn', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [
        // _turn_end_expr coalesces completed_at down to created_at server-side, so a
        // real pending row always arrives with a non-null completed_at — same as timestamp
        // here. The adapter must not mistake that fallback for an actual completion time.
        { id: 8, role: 'assistant', reply_to: 7, topic: 'default', agent: 'claude', adhoc: false, status: 'pending', prompt: 'still going', timestamp: '2026-08-15T13:00:00Z', completed_at: '2026-08-15T13:00:00Z' },
      ],
      has_more: false,
    }}));
    await page.reload();

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return { turn8: s.getTurn(8), order: s.getOrderedTurnIds() };
    });
    expect(snapshot.turn8.status).toBe('pending');
    expect(snapshot.turn8.completedAt).toBeNull();
    expect(snapshot.order.pending).toContain(8);
    expect(snapshot.order.completed).not.toContain(8);
  });
});
