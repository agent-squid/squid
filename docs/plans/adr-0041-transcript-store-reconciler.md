# Plan: Normalized transcript store & reconciler (ADR-0041)

Tracks progress against ADR-0041's six-step migration sequence, one producer
at a time. Status below reflects the tree as of 2026-08-18, verified by
reading `ui/app.js`/`ui/transcript-store.js`/`ui/reconciler.js` directly and
by running the test suite — not by re-reading this doc later. Line numbers
drift; anchor on function/file names.

## Producer order (per the ADR)

1. HTTP history page load (`loadHistory`/`appendHistoryItems`, `ui/app.js`)
2. WebSocket snapshot (`dispatchSnapshot`, `ui/app.js` inside the `realtimeV1` IIFE)
3. WebSocket lifecycle events (`dispatchEvent`, same IIFE, plus `discoverRealtimeTurn`)
4. SSE (`EventSource` fallback path — compatibility-only, migrates last)

Flow and CLI-auth stay on direct-DOM through this whole sequence (out of
scope per the ADR) and get migrated afterward, one at a time.

## Status

| Stage | Producer 1 (HTTP history) | Producer 2 (WS snapshot) | Producer 3 (WS events) | Producer 4 (SSE) |
|---|---|---|---|---|
| 1. Store + reducer | ✅ done | — (shared store) | — (shared store) | — (shared store) |
| 2. Shadow mode | ✅ `shadowInstallHistoryPage` | ❌ not started | ❌ not started | ❌ not started |
| 3. Reconciler + cutover | ⚠️ partial — see below | ❌ | ❌ | ❌ |
| 4. Completion order/route markers/dedup in reconciler | ⚠️ partial | ❌ | ❌ | ❌ |
| 5. Retire direct-DOM path | ❌ **blocked, see gaps** | ❌ | ❌ | ❌ |

Files: `ui/transcript-store.js`, `ui/reconciler.js`, wiring in `ui/app.js`
(search `ADR-0041`). Tests: `tests/e2e/transcript-store.spec.js`,
`reconciler.spec.js`, `history-registry.spec.js`, `history-store-renderer.spec.js`
(32 tests, all green with `--workers=1`; the default parallel run is flaky —
different tests intermittently fail from worker contention, not a real bug —
so use `--workers=1` for a trustworthy local read of this suite).

### What "partial" means for producer 1

- Behind `?renderer=store`, `historyReconciler` (built from `createHistoryRegistry`,
  `ui/app.js` ~7388) renders **completed** history turns; `useReconciler` gates
  `appendHistoryItems` (~2248) to skip the direct-DOM branch for those rows.
- **Pending/live rows are excluded from the reconciler's cutover on purpose**
  (`historyRegistry.render()` no-ops on a non-terminal turn) — that's the
  right long-term split, since pending rendering belongs to producers 2/3.
- Default stays `renderer=dom` (`ui/app.js:38`). The flag is the rollback
  mechanism the ADR's step 5 requires before any direct-DOM code is deleted.

## 2026-08-18 verification run — why producer 1 is not ready for step 5

Ran the full pre-existing e2e suite (39 spec files, unrelated to ADR-0041)
twice: once against the untouched default (`renderer=dom`, baseline), once
against a trial flip of the default to `renderer=store` (kept behind
`?renderer=dom` as rollback). This is a better parity check than hand-picking
files, because it doesn't require touching 30+ unrelated spec files to inject
a query param — flipping the default makes the existing suite exercise the
store path for free.

- **Baseline (`renderer=dom`):** 482 passed, 43 failed. All 43 failures are
  pre-existing and unrelated to transcript rendering — config/stats UI,
  `/stop` and `/clear` command feedback, scroll-reveal timing, mobile
  responsive spacing, CLI-auth unlock retry. None touch history rendering.
- **Trial (`renderer=store`):** 469 passed, 56 failed — the same 43 baseline
  failures **plus 13 new ones**, all in history-rendering code paths:
  - `deep-dive-button.spec.js` — all 7 tests fail.
  - `reply-button.spec.js` — all 5 tests fail.
  - `chat.spec.js:989` — "filter round-trip keeps an older live prompt above
    newer completed history" — 1 test fails.

The default-flip edit was reverted (`ui/app.js:38` is back to defaulting to
`'dom'`); PWA version markers were bumped to `20260818-005` anyway since the
file content (comments) changed. **Do not re-attempt the flip until the two
gaps below are fixed and re-verified with this same before/after run.**

### Gap 1 — a history row without an explicit `status` renders nowhere

`deep-dive-button.spec.js` and `reply-button.spec.js`'s mocked `**/history**`
fixtures omit `status` entirely (most other spec files' fixtures do set it,
e.g. `status: 'done'`, which is why this didn't show up in the 5 dedicated
ADR-0041 tests or most of the suite).

Root cause, `ui/app.js`/`ui/transcript-store.js`:
- `historyItemToStoreRows` (`ui/app.js` ~2201) only sets `completed_at` when
  `HISTORY_TERMINAL_STATUSES.has(item.status)` — false for `status: undefined`.
- `transcript-store.js`'s `isTerminal(status)` (`TERMINAL_STATUSES.has(status)`,
  line ~20) is therefore also false for that row, so `historyRegistry.render()`
  (`ui/app.js` ~7391) treats it as non-terminal and returns `{ nodes: [] }` —
  deferring to the direct-DOM pending-bubble path.
- But `appendHistoryItems`'s direct-DOM pending branch (~2270) only fires on
  `item.status === 'pending'` exactly — also false for `undefined`.
- Net effect: **neither path renders the row.** The old direct-DOM-only code
  was more lenient — its completed-item branch ran for anything that wasn't
  explicitly `'pending'`, so a missing `status` still rendered as completed.

Real server rows (`agent/stats_db.py`) likely always set `status`, so this
may not be a live production bug — but it's a real behavioral divergence
from the old path and the fixtures that hit it aren't wrong to omit an
optional-looking field. Fix by making `historyItemToStoreRows`/`isTerminal`
treat "not `'pending'`, not one of the known in-flight statuses" as terminal
by default, matching the direct-DOM path's leniency, rather than requiring
an explicit terminal status.

### Gap 2 — live/completed interleaving on a filter round-trip

`chat.spec.js:989`: a live in-flight prompt (still direct-DOM, `msg_id` 5)
must stay above two store-rendered completed turns (`msg_id` 6, 7) after a
filter is applied and cleared (`reloadHistory` → `loadHistory` re-anchor).
Expected DOM order `[5, 6, 7]`; store-driven path gets it wrong. This is the
interleaving case the reconciler's `getAnchor`/`historyStoreAnchor` comment
(`ui/app.js` ~7378) already flags as tricky — worth a focused look at how
`historyStoreAnchor` computes the anchor when the live group's position
predates a filter-triggered `reset()`/reload rather than a fresh page load.

## Next steps

1. Fix Gap 1 (status-inference leniency) and Gap 2 (live/completed anchor on
   filter round-trip), each with a regression test added to
   `history-store-renderer.spec.js` reproducing the exact failure first.
2. Re-run the same before/after full-suite comparison from this doc's
   verification section. Producer 1 is ready for step 5 (flip the default,
   keep `?renderer=dom` as rollback for one cycle, then delete the disabled
   direct-DOM branch) only when the trial run's failure count matches the
   baseline exactly — no new failures anywhere in the suite, not just in
   history-adjacent files.
3. Only after that: start producer 2 (Stage 2, shadow mode) — wire
   `dispatchSnapshot`'s per-message loop (`ui/app.js`, inside `realtimeV1`,
   the `for (const message of conversation.messages || [])` block) to also
   call `transcriptStore.installSnapshot(...)`, store-only, no render change,
   mirroring `shadowInstallHistoryPage`. Add parity tests analogous to
   `history-store-renderer.spec.js` comparing store-derived turn groups
   against `dispatchSnapshot`'s current direct-DOM output.
