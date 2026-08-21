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

// ADR-0041 Stage 3 prerequisite: the live status/thinking narrative (the CLI
// scrollback ui/app.js's own statusBuf accumulates today) gets its own
// applyRunEvent kind, since 'status' is already taken by the lifecycle
// transition (payload.status). No renderer reads this yet — store-only.
test('narrative deltas accumulate by default, matching the direct-DOM statusBuf += pattern', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(50, 0, 'narrative', { delta: 'queued — position 3\n' }, 1));
  await store.evaluate(s => s.applyRunEvent(50, 1, 'narrative', { delta: 'reading file.js\n' }, 2));
  const turn = await store.evaluate(s => s.getTurn(50));
  expect(turn.narrative).toBe('queued — position 3\nreading file.js\n');
});

// chat.loading/chat.processing (statusBuf's other writers) replace the
// buffer outright rather than appending — mirrors 'text''s own
// payload.mode === 'replace' switch, not a second bespoke code path.
test('a narrative delta with mode "replace" supersedes the accumulated buffer instead of appending to it', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(51, 0, 'narrative', { delta: 'reading file.js\n' }, 1));
  await store.evaluate(s => s.applyRunEvent(51, 1, 'narrative', { mode: 'replace', text: 'switching claude → codex…\n' }, 2));
  const turn = await store.evaluate(s => s.getTurn(51));
  expect(turn.narrative).toBe('switching claude → codex…\n');
});

// Same dedup machinery applyRunEvent already gives 'text'/'tool'/'stats' —
// nothing kind-specific to narrative, but worth a directed check since a
// duplicate narrative replay double-appending would be a visible bug
// (repeated status lines), unlike a duplicate stats patch.
test('a replayed narrative event (same run_seq or event_id) does not double-apply', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(52, 0, 'narrative', { delta: 'a' }, 1));
  const dup = await store.evaluate(s => s.applyRunEvent(52, 0, 'narrative', { delta: 'a' }, 1));
  expect(dup.noop).toBe(true);
  const turn = await store.evaluate(s => s.getTurn(52));
  expect(turn.narrative).toBe('a');
});

// mergeSparse leaves omitted fields untouched, but this is worth asserting
// directly for narrative: a terminal status patch never mentions it, and
// the render cutover this unblocks needs the accumulated buffer to still be
// there (even though nothing reads it once terminal today).
test('narrative survives a later terminal status transition instead of being cleared', async ({ page }) => {
  const store = await freshStore(page);
  await store.evaluate(s => s.applyRunEvent(53, 0, 'narrative', { delta: 'thinking…\n' }, 1));
  await store.evaluate(s => s.applyMessagePatch(53, { status: 'done', completed_at: '2026-08-21T00:00:00Z' }, 2));
  const turn = await store.evaluate(s => s.getTurn(53));
  expect(turn.status).toBe('done');
  expect(turn.narrative).toBe('thinking…\n');
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

// SSE (producer 4) has no global event_id on the wire at all — this proves
// applyRunEvent's eventId param is genuinely optional, not just permissive:
// omitting it must not touch the shared global watermark WS calls rely on,
// while still gating on this message's own run_seq. A fabricated/local
// global id was rejected during ADR-0041 design specifically because it
// could poison that shared watermark for real WS-sourced calls — this test
// exists to keep that property honest.
test('applyRunEvent with no event_id (SSE) skips the global watermark and does not advance it, but still gates on its own run_seq', async ({ page }) => {
  const store = await freshStore(page);
  // A prior WS-style call establishes a real global watermark.
  await store.evaluate(s => s.applyRunEvent(60, 0, 'text', { delta: 'ws ' }, 5));
  expect(await store.evaluate(s => s.getLastAppliedEventId())).toBe(5);

  // An SSE-style call (event_id omitted) for a different message must not
  // be rejected by that watermark, and must not advance it either.
  const sse1 = await store.evaluate(s => s.applyRunEvent(61, 0, 'text', { delta: 'sse ' }));
  expect(sse1.ok).toBe(true);
  expect((await store.evaluate(s => s.getMessage(61))).content).toBe('sse ');
  expect(await store.evaluate(s => s.getLastAppliedEventId())).toBe(5);

  // Its own run_seq still dedups a stale/duplicate delta.
  const sse2 = await store.evaluate(s => s.applyRunEvent(61, 0, 'text', { delta: 'dup' }));
  expect(sse2.noop).toBe(true);
  expect((await store.evaluate(s => s.getMessage(61))).content).toBe('sse ');

  const sse3 = await store.evaluate(s => s.applyRunEvent(61, 1, 'text', { delta: 'more' }));
  expect(sse3.ok).toBe(true);
  expect((await store.evaluate(s => s.getMessage(61))).content).toBe('sse more');
});

// run_seq is documented as mandatory, not best-effort — a producer that
// can't supply a valid one (e.g. a missing/stripped SSE `id:` line) must be
// rejected outright, not silently applied with no dedup guard. Applying it
// anyway would mean a later replay of that same frame gets accepted as a
// fresh delta instead of a duplicate, since the per-message watermark never
// advanced to reject it (caught in #squid@codex review before publish).
test('applyRunEvent rejects a non-finite run_seq instead of silently applying it unprotected', async ({ page }) => {
  const store = await freshStore(page);
  const missing = await store.evaluate(s => s.applyRunEvent(80, undefined, 'text', { delta: 'a' }));
  expect(missing.ok).toBe(false);
  expect(await store.evaluate(s => s.getMessage(80))).toBeUndefined();

  const nan = await store.evaluate(s => s.applyRunEvent(80, NaN, 'text', { delta: 'a' }));
  expect(nan.ok).toBe(false);
  expect(await store.evaluate(s => s.getMessage(80))).toBeUndefined();

  // A valid run_seq afterward still works normally — the rejection isn't sticky.
  const ok = await store.evaluate(s => s.applyRunEvent(80, 0, 'text', { delta: 'a' }));
  expect(ok.ok).toBe(true);
  expect((await store.evaluate(s => s.getMessage(80))).content).toBe('a');
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

// ── Stage 2: WS snapshot producer, shadow mode ──────────────────────────────
// app.js now also feeds every WS 'snapshot' frame into
// window.__transcriptStore alongside its existing direct-DOM discovery path
// (shadowInstallSnapshot, mirroring shadowInstallHistoryPage). These tests
// drive the live page over a mocked WebSocket so the same frame produces
// both the DOM and the store state, and check the two stay in parity without
// a second render.

test.describe('WS snapshot producer (shadow mode)', () => {
  test.beforeEach(async ({ page }) => {
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
    await page.unroute('**/config/realtime');
    await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'websocket' } }));
  });

  test('a WS-discovered turn renders the DOM the same way HTTP-history discovery does, and installs equivalent turns into the store', async ({ page }) => {
    await page.route('**/chat/7/status', r => r.fulfill({ json: {
      id: 7, role: 'assistant', reply_to: 6, topic: 'default', agent: 'claude',
      adhoc: false, status: 'done', prompt: "what's your model?", content: 'Done 7',
      completed_at: '2026-08-15T12:00:00Z',
    }}));
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{ messages: [
        { id: 6, role: 'user', reply_to: null, content: "what's your model?" },
        { id: 7, role: 'assistant', reply_to: 6, topic: 'default', agent: 'claude',
          adhoc: false, status: 'done', content: 'Done 7', completed_at: '2026-08-15T12:00:00Z' },
      ] }] },
    }));

    // Direct-DOM path is unaffected: the turn discovered via the WS snapshot
    // still renders exactly once, via the existing /chat/{id}/status fetch.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="7"]')).toHaveCount(1);

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return { turn7: s.getTurn(7), prompt6: s.getMessage(6), order: s.getOrderedTurnIds() };
    });
    expect(snapshot.turn7.status).toBe('done');
    expect(snapshot.turn7.promptMsgId).toBe(6);
    expect(snapshot.prompt6).toMatchObject({ role: 'user', content: "what's your model?" });
    expect(snapshot.order.completed).toContain(7);
  });

  // Stage 4: a snapshot message is already a raw chat_messages row, so the
  // store should be able to render from turn.raw the same way producer 1's
  // historyItemToStoreRows lets historyRegistry render straight from its own
  // raw history item — not just track identity/ordering.
  test('a WS snapshot message carries its full row on turn.raw, not just identity/ordering fields', async ({ page }) => {
    await page.route('**/chat/7/status', r => r.fulfill({ json: {
      id: 7, role: 'assistant', reply_to: 6, topic: 'default', agent: 'claude',
      adhoc: false, status: 'done', prompt: "what's your model?", content: 'Done 7',
      completed_at: '2026-08-15T12:00:00Z',
    }}));
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{ messages: [
        { id: 6, role: 'user', reply_to: null, content: "what's your model?" },
        { id: 7, role: 'assistant', reply_to: 6, topic: 'default', agent: 'claude',
          adhoc: false, status: 'done', content: 'Done 7', completed_at: '2026-08-15T12:00:00Z' },
      ] }] },
    }));

    const raw = await page.evaluate(() => window.__transcriptStore.getTurn(7).raw);
    expect(raw).toMatchObject({
      id: 7, topic: 'default', agent: 'claude', adhoc: false, content: 'Done 7',
    });
  });

  test('a pending WS snapshot message installs into the store as a non-terminal turn', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 5, payload: { conversations: [{ messages: [
        { id: 8, role: 'assistant', reply_to: null, status: 'pending', content: '' },
      ] }] },
    }));

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return { turn8: s.getTurn(8), order: s.getOrderedTurnIds() };
    });
    expect(snapshot.turn8.status).toBe('pending');
    expect(snapshot.order.pending).toContain(8);
    expect(snapshot.order.completed).not.toContain(8);
  });

  // ADR-0041 Stage 3 prerequisite: a recovered pending row (e.g. after a
  // page refresh) already carries its accumulated status_raw narrative as a
  // plain field on the row, not as a sequence of run-events — no new store
  // code needed for this path, since Stage 4's raw passthrough already puts
  // the whole row on turn.raw. This documents that it actually works,
  // rather than assuming it from Stage 4's own (differently-scoped) test.
  test('a recovered pending WS snapshot row carries its status_raw narrative on turn.raw', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 6, payload: { conversations: [{ messages: [
        { id: 14, role: 'assistant', reply_to: null, status: 'pending', content: '',
          status_raw: 'reading file.js\nrunning tests…' },
      ] }] },
    }));

    const raw = await page.evaluate(() => window.__transcriptStore.getTurn(14).raw);
    expect(raw.status_raw).toBe('reading file.js\nrunning tests…');
  });

  // The store's own event_id watermark (installSnapshot) is independent of
  // the WS transport's replay cursor — a second snapshot frame at the same
  // event_id passes the transport-level gate (dispatchSnapshot only drops
  // event_id < cursor) but must still be a no-op in the store.
  test('a second snapshot frame at the same event_id is a store no-op', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{ messages: [
        { id: 9, role: 'assistant', reply_to: null, status: 'done', completed_at: '2026-08-20T00:00:00Z' },
      ] }] },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'snapshot', event_id: 10, payload: { conversations: [{ messages: [
        { id: 9, role: 'assistant', reply_to: null, status: 'error' },
      ] }] },
    }));

    const status = await page.evaluate(() => window.__transcriptStore.getMessage(9).status);
    expect(status).toBe('done');
  });
});

// ── Stage 2: WS lifecycle events producer, shadow mode ──────────────────────
// app.js now also feeds message.changed/chat.text/chat.tool/chat.stats
// lifecycle frames into window.__transcriptStore alongside their existing
// direct-DOM handling (shadowApplyEvent, mirroring shadowInstallSnapshot).
// These tests drive the live page over a mocked WebSocket so the same frame
// produces both the DOM and the store state, and check the two stay in
// parity without a second render.

test.describe('WS lifecycle events producer (shadow mode)', () => {
  test.beforeEach(async ({ page }) => {
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
    await page.unroute('**/config/realtime');
    await page.route('**/config/realtime', r => r.fulfill({ json: { transport: 'websocket' } }));
  });

  test('a message.changed event for a newly-discovered turn renders the DOM via the existing discovery fetch, and patches the store from the frame itself', async ({ page }) => {
    await page.route('**/chat/12/status', r => r.fulfill({ json: {
      id: 12, role: 'assistant', reply_to: 11, topic: 'default', agent: 'claude',
      adhoc: false, status: 'done', prompt: 'hello', content: 'fetched content',
      completed_at: '2026-08-20T00:00:00Z',
    }}));
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'message.changed', event_id: 5, msg_id: 12,
      payload: { id: 12, role: 'assistant', status: 'done', content: 'frame content', reply_to: 11 },
    }));

    // Direct-DOM path is unaffected: discovery still renders from its own
    // /chat/{id}/status fetch, not from the frame's own content field.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="12"]'))
      .toContainText('fetched content');

    const message = await page.evaluate(() => window.__transcriptStore.getMessage(12));
    // The store, in contrast, is fed straight from the frame's own payload.
    expect(message).toMatchObject({ role: 'assistant', status: 'done', content: 'frame content', replyTo: 11 });
  });

  test('chat.text run-events accumulate into the store message content in run_seq order', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'message.changed', event_id: 1, msg_id: 20,
      payload: { id: 20, role: 'assistant', status: 'pending', reply_to: 19 },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.text', event_id: 2, msg_id: 20, run_seq: 4, payload: { text: 'hello ' },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.text', event_id: 3, msg_id: 20, run_seq: 5, payload: { text: 'world' },
    }));

    const content = await page.evaluate(() => window.__transcriptStore.getMessage(20).content);
    expect(content).toBe('hello world');
  });

  // ADR-0041 Stage 3 prerequisite: chat.status is the CLI's live status/
  // thinking line, distinct from chat.text (final reply content) — feeds
  // the store's own 'narrative' kind, appending the same way the direct-DOM
  // path's statusBuf += text does.
  test('chat.status run-events accumulate into the store turn narrative, separately from content', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'message.changed', event_id: 1, msg_id: 21,
      payload: { id: 21, role: 'assistant', status: 'pending', reply_to: 19 },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.status', event_id: 2, msg_id: 21, run_seq: 4, payload: { text: 'reading file.js\n' },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.status', event_id: 3, msg_id: 21, run_seq: 5, payload: { text: 'running tests…\n' },
    }));

    const turn = await page.evaluate(() => window.__transcriptStore.getTurn(21));
    expect(turn.narrative).toBe('reading file.js\nrunning tests…\n');
    expect(turn.status).toBe('pending');
  });

  test('a chat.tool event at or below the applied event_id watermark is a store no-op, same as the snapshot producer', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'message.changed', event_id: 10, msg_id: 30,
      payload: { id: 30, role: 'assistant', status: 'pending', reply_to: 29 },
    }));
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.tool', event_id: 10, msg_id: 30, run_seq: 4,
      payload: { name: 'Read', tool_use_id: 'toolu_1' },
    }));

    const tools = await page.evaluate(() => window.__transcriptStore.getMessage(30).tools);
    expect(tools).toEqual([]);
  });

  test('two real chat.tool updates for the same tool_use_id merge into one entry, not two', async ({ page }) => {
    await page.reload();
    await page.waitForFunction(() => window.__webSocket?.readyState === 1);

    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'message.changed', event_id: 1, msg_id: 40,
      payload: { id: 40, role: 'assistant', status: 'pending', reply_to: 39 },
    }));
    // Start: the tool call is announced, no result yet.
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.tool', event_id: 2, msg_id: 40, run_seq: 4,
      payload: { name: 'Read', tool_use_id: 'toolu_1', file: 'app.js' },
    }));
    // Result: the same tool_use_id reports completion.
    await page.evaluate(() => window.__webSocket.receive({
      v: 1, type: 'chat.tool', event_id: 3, msg_id: 40, run_seq: 5,
      payload: { name: 'Read', tool_use_id: 'toolu_1', file: 'app.js', result: 'ok' },
    }));

    const tools = await page.evaluate(() => window.__transcriptStore.getMessage(40).tools);
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ tool_use_id: 'toolu_1', result: 'ok' });
  });
});

// ── Stage 2: SSE producer, shadow mode ──────────────────────────────────────
// app.js now also feeds SSE-transport frames into window.__transcriptStore
// alongside their existing direct-DOM handling — both the primary POST
// /chat streaming response (sendMessage's own hand-rolled parser) and the
// reconnect GET /chat/{msg_id}/events (EventSource, in
// reconnectPendingItem) — via shadowApplySseRunEvent/
// shadowInstallSseCompletion, mirroring shadowApplyEvent. Unlike WS, SSE has
// no global event_id: text/tool/stats deltas are gated only by the real
// run_events.seq now carried on the wire as each frame's `id:` field
// (agent/server.py's sse_chunk/sse_event, fed from agent/topic_queue.py's
// out_q); terminal done/error re-fetch the authoritative row instead of
// applying a sequenced patch (see shadowInstallSseCompletion's own comment
// in ui/app.js).

test.describe('SSE producer (shadow mode)', () => {
  test('a fresh POST /chat SSE stream renders the DOM as before and feeds text/tool/stats deltas into the store by their real run_events.seq', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
        'X-Squid-Msg-Id': '50',
      },
      body:
        'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":50,"adhoc":false}\n\n' +
        'id: 4\nevent: tool\ndata: {"tool_use_id":"t1","name":"Bash","command":"ls"}\n\n' +
        'id: 5\ndata: Hello \n\n' +
        'id: 6\ndata: world\n\n' +
        'id: 7\nevent: stats\ndata: {"session_id":"sid-1","input_tokens":3,"output_tokens":2}\n\n' +
        'id: 8\nevent: done\ndata: \n\n',
    }));
    await page.route('**/chat/50/status', r => r.fulfill({ json: {
      // agent/stats_db.py's get_message (backing this real endpoint) always
      // attaches a stats object via _attach_turn_stats, not an empty one.
      id: 50, role: 'assistant', status: 'done', content: 'Hello world',
      stats: { session_id: 'sid-1', input_tokens: 3, output_tokens: 2 },
      completed_at: '2026-08-21T00:00:00Z',
    }}));

    await page.fill('#input', 'hi');
    await page.keyboard.press('Enter');

    // Direct-DOM path is unaffected: it renders from the stream's own
    // accumulated raw text, never from the shadow completion's /status fetch.
    await expect(page.locator('.msg.assistant:not(.msg-thinking)')).toContainText('Hello world');

    // The completion install is its own async fetch (not awaited by the
    // direct-DOM 'done' branch), so wait for it to land before asserting on
    // fields it alone is responsible for (status/completedAt).
    await expect.poll(() => page.evaluate(() => window.__transcriptStore.getTurn(50)?.status)).toBe('done');

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return { message: s.getMessage(50), turn: s.getTurn(50) };
    });
    // content came from the run_seq-ordered deltas (applyRunEvent), and
    // matches what the authoritative completion install also reports —
    // proving both paths agree, not just that one silently overwrote the other.
    expect(snapshot.message.content).toBe('Hello world');
    expect(snapshot.message.tools).toHaveLength(1);
    expect(snapshot.message.tools[0]).toMatchObject({ tool_use_id: 't1', name: 'Bash' });
    expect(snapshot.message.stats).toMatchObject({ session_id: 'sid-1', input_tokens: 3 });
    expect(snapshot.turn.completedAt).toBe('2026-08-21T00:00:00Z');
  });

  test('a multi-line text delta on the primary POST /chat path is not truncated in the store (sse_chunk can split one delta across several data: lines sharing one id:)', async ({ page }) => {
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
        'X-Squid-Msg-Id': '55',
      },
      // One delta ("line one\nline two") encoded exactly as agent/server.py's
      // sse_chunk would: one id:, two data: lines, one blank-line boundary.
      // A second, single-line delta follows to prove normal deltas still
      // append correctly after a multi-line one. No 'done' event on purpose:
      // sending one would let shadowInstallSseCompletion's authoritative
      // /status fetch overwrite content with the "correct" value regardless
      // of whether delta accumulation actually worked, masking exactly the
      // regression this test exists to catch (a prior version of this test
      // did include 'done' plus a /status mock returning this same string —
      // #squid@codex's review caught that it would still pass even with the
      // truncation bug reintroduced, since the completion install always
      // wins in the end). Omitting 'done' isolates the delta path itself.
      body:
        'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":55,"adhoc":false}\n\n' +
        'id: 4\ndata: line one\ndata: line two\n\n' +
        'id: 5\ndata:  and more\n\n',
    }));

    await page.fill('#input', 'hi');
    await page.keyboard.press('Enter');

    await expect.poll(() => page.evaluate(() => window.__transcriptStore.getMessage(55)?.content))
      .toBe('line one\nline two and more');
  });

  test('a text delta with no id: line on the primary POST /chat path is rejected, not silently applied as run_seq 0', async ({ page }) => {
    // dataId starts as JS null and only becomes non-null once an id: line
    // is actually seen — Number(null) is 0, a valid finite number that
    // shadowApplySseRunEvent must not hand to applyRunEvent as a real
    // run_seq (it would apply unprotected instead of being rejected, and
    // could later collide with a genuine low run_seq). Caught in
    // #squid@codex review before publish: the prior fix only special-cased
    // event.lastEventId === '' (EventSource's own "no id" sentinel), not
    // null/undefined (this hand-rolled parser's).
    await page.route('**/chat', r => r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
        'X-Squid-Msg-Id': '56',
      },
      body:
        'event: meta\ndata: {"agent":"claude","backend":"claude","msg_id":56,"adhoc":false}\n\n' +
        'data: no id here\n\n' +
        'id: 4\ndata: this one has an id\n\n',
    }));

    await page.fill('#input', 'hi');
    await page.keyboard.press('Enter');

    // If the id-less frame had been accepted, its text would prefix this.
    await expect.poll(() => page.evaluate(() => window.__transcriptStore.getMessage(56)?.content))
      .toBe('this one has an id');
  });

  test('a reconnected pending item (page load) renders the DOM as before via its own discovery fetch, and feeds run_events.seq-based deltas into the store from the SSE frames themselves', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [
        { id: 70, role: 'assistant', reply_to: 69, topic: 'default', agent: 'claude', adhoc: false,
          status: 'pending', prompt: 'still going', timestamp: '2026-08-21T00:00:00Z', completed_at: '2026-08-21T00:00:00Z' },
      ],
      has_more: false,
    }}));
    await page.route('**/chat/70/events**', r => r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
      body:
        'id: 4\nevent: tool\ndata: {"tool_use_id":"t2","name":"Read","file":"x.py"}\n\n' +
        'id: 5\ndata: Reconnected \n\n' +
        'id: 6\ndata: text\n\n' +
        'id: 7\nevent: done\ndata: \n\n',
    }));
    await page.route('**/chat/70/status', r => r.fulfill({ json: {
      // reply_to must match the history row's (69) — get_message's real row
      // always carries it, and the store's identity-conflict guard would
      // otherwise reject this completion install against the reply_to the
      // initial pending-row install already established for msg 70.
      id: 70, role: 'assistant', reply_to: 69, status: 'done', content: 'Reconnected text',
      completed_at: '2026-08-21T00:05:00Z',
    }}));

    await page.reload();

    // Direct-DOM path is unaffected: it still resolves the pending bubble
    // via its own reconnect flow, not from the shadow feed.
    await expect(page.locator('.msg.assistant.history-item[data-msg-id="70"]')).toHaveCount(1);
    await expect.poll(() => page.evaluate(() => window.__transcriptStore.getTurn(70)?.status)).toBe('done');

    const snapshot = await page.evaluate(() => {
      const s = window.__transcriptStore;
      return { message: s.getMessage(70) };
    });
    expect(snapshot.message.content).toBe('Reconnected text');
    expect(snapshot.message.tools).toHaveLength(1);
    expect(snapshot.message.tools[0]).toMatchObject({ tool_use_id: 't2', name: 'Read' });
  });

  test('a duplicate/replayed SSE frame (same run_events.seq) is a no-op in the store', async ({ page }) => {
    await page.unroute('**/history**');
    await page.route('**/history**', r => r.fulfill({ json: {
      items: [
        { id: 71, role: 'assistant', reply_to: 69, topic: 'default', agent: 'claude', adhoc: false,
          status: 'pending', prompt: 'still going', timestamp: '2026-08-21T00:00:00Z', completed_at: '2026-08-21T00:00:00Z' },
      ],
      has_more: false,
    }}));
    // A real reconnect always replays from the start (after_seq defaults to
    // -1 server-side) — a duplicate id for the same delta must not double it.
    await page.route('**/chat/71/events**', r => r.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
      body:
        'id: 4\ndata: hello\n\n' +
        'id: 4\ndata: hello\n\n' +
        'id: 5\ndata: !\n\n',
    }));

    await page.reload();
    await expect.poll(() => page.evaluate(() => window.__transcriptStore.getMessage(71)?.content)).toBe('hello!');
  });
});
