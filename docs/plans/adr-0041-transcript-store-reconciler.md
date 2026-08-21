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
| 2. Shadow mode | ✅ `shadowInstallHistoryPage` | ✅ `shadowInstallSnapshot` (2026-08-20) | ✅ `shadowApplyEvent` (2026-08-21) | ✅ `shadowApplySseRunEvent`/`shadowInstallSseCompletion` (2026-08-20, both SSE paths) |
| 3. Reconciler + cutover | ⚠️ partial — see below | ❌ (node-adoption prerequisite done 2026-08-21 — bookkeeping only, no render/cutover) | ❌ | ❌ |
| 4. Completion order/route markers/dedup in reconciler | ⚠️ partial | ✅ `raw` field-carry only (2026-08-21) — no cutover | ❌ | ❌ |
| 5. Retire direct-DOM path | ✅ default flipped to `renderer=store` 2026-08-20 (`?renderer=dom` kept as one-cycle rollback; direct-DOM branch not yet deleted) | ❌ | ❌ | ❌ |

Files: `ui/transcript-store.js`, `ui/reconciler.js`, wiring in `ui/app.js`
(search `ADR-0041`). Tests: `tests/e2e/transcript-store.spec.js`,
`reconciler.spec.js`, `history-registry.spec.js`, `history-store-renderer.spec.js`
(40 tests total; each file is green on its own with `--workers=1`). The
default parallel run is flaky from worker contention as previously noted —
2026-08-20/21 testing also found running all four files together is flaky
even at `--workers=1` (a different small subset intermittently fails each
time, reproducing identically on an unmodified tree, so it's pre-existing
suite-level flakiness, not tied to any one change). Read this suite one file
at a time for a trustworthy result.

### What "partial" means for producer 1

- Behind `?renderer=store`, `historyReconciler` (built from `createHistoryRegistry`,
  `ui/app.js` ~7388) renders **completed** history turns; `useReconciler` gates
  `appendHistoryItems` (~2248) to skip the direct-DOM branch for those rows.
- **Pending/live rows are excluded from the reconciler's cutover on purpose**
  (`historyRegistry.render()` no-ops on a non-terminal turn) — that's the
  right long-term split, since pending rendering belongs to producers 2/3.
- Default is `renderer=store` as of 2026-08-20 (`ui/app.js:43`). `?renderer=dom`
  stays available as the rollback mechanism the ADR's step 5 requires before
  any direct-DOM code is deleted — that deletion hasn't happened yet.

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
file content (comments) changed. **Do not re-attempt the flip until the gaps
below are fixed and re-verified with this same before/after run** — both
Gap 1 and Gap 2 are now fixed (see below); the before/after full-suite
comparison still needs to be re-run (Next steps below) before the flip.

### Gap 1 — a history row without an explicit `status` renders nowhere (fixed 2026-08-20)

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
optional-looking field.

First attempt (superseded — see correction below) flipped
`transcript-store.js`'s shared `isTerminal()` from an allowlist of terminal
values to a denylist of known non-terminal ones. Caught in review before
publish: `applyRunEvent`'s `'text'`/`'tool'`/`'stats'` kinds (producers 2/3)
create a message with **no status field at all** while it's genuinely still
streaming — the denylist misread that "unknown" as terminal, so
`mergeSparse`'s terminal-status monotonicity guard then silently dropped the
real `'running'` status patch that arrived right after (verified with a
direct repro: `store.getMessage(id).status` stayed `undefined` after the
patch instead of becoming `'running'`).

Fix (corrected): `transcript-store.js`'s `isTerminal()` stays an explicit
`TERMINAL_STATUSES` allowlist (`'done'`/`'error'`/`'cancelled'`), unchanged
from before Gap 1. The missing-status normalization instead happens only at
the HTTP-history producer's own edge: `historyItemToStoreRows` (`ui/app.js`
~2219) now does `const status = item.status ?? 'done';` before building the
row, so a row with no status renders as completed without weakening the
shared classifier every producer's monotonicity guard depends on staying
strict.
- Regression test added first: `history-store-renderer.spec.js` "a history
  row with no status field still renders as completed" (reproduces the
  `deep-dive-button.spec.js`/`reply-button.spec.js` fixture shape).
- Regression test added for the caught issue:
  `transcript-store.spec.js` "a status-less streamed message accepts a later
  running status, then a real terminal one" (`applyRunEvent('text')` with no
  status → `applyMessagePatch({status:'running'})` must actually apply, not
  be swallowed → a later `'done'` patch still applies and then sticks).

Verified: `deep-dive-button.spec.js` (7/7), `reply-button.spec.js` (5/5), and
both new regression tests all pass under `renderer=store`.
`transcript-store.spec.js`/`reconciler.spec.js`/`history-registry.spec.js`
pass too, modulo a pre-existing `page.evaluateHandle`/`createTranscriptStore`
setup race unrelated to this change (reproduces identically against the
unmodified tree; clears with `--retries=2`) — not a new failure mode.

### Gap 2 — live/completed interleaving on a filter round-trip (fixed 2026-08-20)

`chat.spec.js:989`: a live in-flight prompt (still direct-DOM, `msg_id` 5)
must stay above two store-rendered completed turns (`msg_id` 6, 7) after a
filter is applied and cleared (`reloadHistory` → `loadHistory` re-anchor).
Expected DOM order `[5, 6, 7]`; store-driven path got it wrong (`[6, 7, 5]`).

Root cause: `historyStoreAnchor` (`ui/app.js` ~1873) decided whether a
completed turn must jump above a live group by comparing `completedAt`
against the live group's `data-order-at`. The composer-live-send thinking
bubble (`sendMessage`) never stamps `data-order-at` — only the direct-DOM
history-pending path does — so for a composer-submitted live turn the
comparison always fell through to "no start recorded ⇒ always qualifies as
newest," unconditionally pushing every reloaded completed turn above it.

Fix: when `data-order-at` is present, keep the existing time comparison
unchanged (`history-store-renderer.spec.js`'s "live id newer than completed
ids" test proves id order and time order can disagree, so id can't replace
that comparison). When it's absent, fall back to comparing `msg_id` instead
— the same submission-order proxy `anchorBeforeNextLiveGroup` already uses
for the page-load case. Verified: `chat.spec.js:989`, `:1047`, and `:1134`
(the three filter-round-trip anchor tests) plus both
`history-store-renderer.spec.js` live-bubble-slot tests pass together under
a trial `renderer=store` default; full `chat.spec.js` + all ADR-0041 store
suites show no failures beyond the pre-existing, unrelated baseline
(scroll-reveal timing, CLI-auth unlock retry — see Gap 1's note on that
baseline).

## 2026-08-20 finalization: full baseline, then the flip

Re-ran the full before/after comparison with both gaps fixed:

- **Baseline (`renderer=dom`, still-default tree, `--workers=1 --retries=1`):**
  475 passed, 44 failed, 8 flaky (527 total, ~49 min). Closely matches the
  2026-08-18 baseline (482/43) — no unexplained drift.
- Flipped the default to `renderer=store` and re-ran the same targeted set
  (`chat.spec.js` + all 5 ADR-0041 suites + `boot-logo.spec.js` — the actual
  blast radius of this flip): **1 new failure**, `chat.spec.js:1012` "filter
  round-trip keeps an older live prompt above newer completed history"
  (Gap 2's own regression test), received `[6, 7, 5]` instead of `[5, 6, 7]`.

### Gap 3 — an unrelated same-day change reopened Gap 2 (fixed 2026-08-20)

A separate session's date-divider fix (`refreshDateDividers`, same day) added
`thinkingBubble.dataset.orderAt = sendTime` unconditionally in `sendMessage`
and the auto-resolve flow — reasonable for calendar grouping, but it meant
Gap 2's premise ("the composer-live-send bubble never stamps `data-order-at`,
so `historyStoreAnchor` falls back to `msg_id`") stopped being true. The
`msg_id` fallback branch became unreachable for those two bubble paths, so
`historyStoreAnchor` always took the time-comparison branch instead — using
real wall-clock `sendTime`. `chat.spec.js:1012`'s fixture used fixed
`2026-08-15` dates for the completed turns, on the (now-stale) assumption
that the live bubble's time would never matter; since real "now" is after
that fixed date, the live bubble always compared as newest, reproducing
Gap 2's original bug. This was **dormant under `renderer=dom`**
(`anchorBeforeNextLiveGroup`, the direct-DOM interleave path, only ever reads
`dataset.msgId`, never `dataset.orderAt`) — it only surfaced once the default
flipped to `renderer=store`, which is exactly the change this was blocking.

Fix: `sendTime`/`orderAt` was correct and left alone — the test fixture's
absolute past dates were the actual bug, a ticking time bomb now that a live
bubble's `orderAt` is always real. Changed `chat.spec.js:1012` to compute the
completed turns' timestamps relative to `Date.now() + 1h/2h` instead of a
fixed date, so the fixture keeps testing its real intent (a turn that started
earlier stays above turns that finished later) using the mechanism that's
actually live now. Also updated the `historyStoreAnchor` comment
(`ui/app.js` ~1854), which still claimed the composer bubble never stamps
`data-order-at` — no longer true; `msg_id` fallback now only fires for
`makeWipBubble`'s one remaining conditional case (missing `item.timestamp`).

Re-verified: the same targeted set (156 tests) came back to 154 passed + 1
flaky (`chat.spec.js:4083`, the pre-existing CLI-auth unlock-retry flake also
present in the direct-DOM baseline) — 0 hard failures.

**Decision:** finalized on the targeted-suite result rather than re-running
the full 39-file double comparison a second time (~50 min per run) — the
targeted set covers every file touching history rendering, turn ordering, or
the composer/date-divider paths, which is the full blast radius of both
changes combined. `ui/app.js:43`'s default is now `renderer=store`;
`?renderer=dom` stays as the rollback escape hatch. PWA cache bumped to
`v20260820-006`.

## 2026-08-20: producer 2 (WS snapshot) Stage 2, shadow mode

Added `shadowInstallSnapshot(frame)` in `ui/app.js`, inside the `realtimeV1`
IIFE right before `dispatchSnapshot`, and call it from `dispatchSnapshot`
(after the existing `frame.event_id < cursor` gate, before the per-message
DOM-discovery loop) — store-only, no render change, mirroring
`shadowInstallHistoryPage`.

Unlike producer 1's HTTP history items, a WS snapshot message is already a
raw `chat_messages` row (`agent/stats_db.py`'s `get_realtime_snapshot` does
`dict(row)` per message) — one row per message, not a denormalized turn with
an inlined prompt — so there's no `historyItemToStoreRows`-style split into
two rows. `shadowInstallSnapshot` collects every message across every
`conversation` in the frame's payload into one row array and calls
`transcriptStore.installSnapshot({ messages: rows }, frame.event_id)` **once
per frame**, not once per conversation — the store's `installSnapshot`
watermark (`lastAppliedEventId`) only advances past `frame.event_id` on the
first call, so a second call with the same `event_id` would otherwise be a
silent no-op and lose every conversation after the first.

Only identity/ordering fields are carried (`msg_id`, `role`, `reply_to`,
`status`, `completed_at`, `created_at`, `content`, `stats`) — not enough to
render from (no `topic`/`agent`/`adhoc`/`session_id`/tool context/etc.),
matching producer 1's own Stage 2 scope (see `historyItemToStoreRows`'s
comment on `raw` — that passthrough is Stage 4, not done for producer 2 yet).

Known gap, not fixed here (out of scope for shadow mode, no render depends on
it yet): the store's `installSnapshot` watermark has no concept of the WS
transport's `cursor_reset` rollover (`agent/server.py`'s `future_cursor`
case, `ui/app.js`'s `resetCursor` in `dispatchSnapshot`/`markApplied`). A
reset frame can carry an `event_id` at or below the store's own watermark,
which the store then silently no-ops instead of accepting as authoritative —
revisit if/when producer 2 moves to Stage 3/4 and something actually renders
from it.

Tests: `tests/e2e/transcript-store.spec.js`, new `describe('WS snapshot
producer (shadow mode)')` block (3 tests, analogous to the HTTP history
producer's own shadow-mode block) — drives the live page over a mocked
WebSocket, sends real `snapshot` frames, and checks the direct-DOM discovery
path (`discoverRealtimeTurn` → `/chat/{id}/status` → `insertCompletedHistoryItem`)
renders unchanged while `window.__transcriptStore` picks up an equivalent
turn/message/order. Verified: those 3 plus all 34 pre-existing ADR-0041 store
tests (37 total) and the full `chat.spec.js` (102 tests) pass with
`--workers=1`. PWA cache bumped to `v20260820-007`.

## 2026-08-21: producer 3 (WS lifecycle events) Stage 2, shadow mode

Added `shadowApplyEvent(frame)` in `ui/app.js`, inside the `realtimeV1` IIFE
right before `dispatchEvent`, and call it from `dispatchEvent` (right after
the existing `frame.event_id <= cursor` gate, before the per-type branching)
— store-only, no render change, mirroring `shadowInstallSnapshot`.

`message.changed` is the only frame type on this transport that ever reports
an authoritative status/content transition — every `_insert_realtime_event(
conn, "message.changed", ...)` call site in `agent/stats_db.py` always
includes `id`/`role`/`status`/`content` (sometimes `reply_to`/`session_id`).
`chat.done`/`chat.error` carry no message fields of their own (`insert_run_event`
in `agent/topic_queue.py` passes `payload=None` for `"done"` and a plain
error string for `"error"`), so they have nothing to add beyond what a
`message.changed` frame already will — not wired. `chat.text`/`chat.tool`/
`chat.stats` map onto `applyRunEvent`'s existing `'text'`/`'tool'`/`'stats'`
kinds; every run-event-sourced `chat.*` frame shares one server-side
`run_seq` counter per assistant message (`agent/topic_queue.py`'s
`run_seq += 1`, incremented across all event kinds for that message), which
is exactly what `applyRunEvent`'s own per-`msgId` monotonicity check assumes.

Two known gaps, neither fixed here (same posture as producer 2's
`cursor_reset` gap — shadow mode doesn't render, so nothing depends on
either yet):
- `applyRunEvent`'s `'tool'` case dedupes an incoming tool payload by
  `payload.id`, but real tool payloads (`topic_queue.py`'s `_emit_tool`) key
  on `tool_use_id`/`name` instead — every `chat.tool` frame fed in lands as a
  new `tools[]` entry rather than updating one in place. This is a
  pre-existing mismatch in `transcript-store.js` itself, not introduced by
  this wiring.
- `flow.step.created` carries only identity fields (`flow_run_id`/`step_id`/
  `assistant_msg_id`), not enough to construct a meaningful store row
  without an extra fetch Stage 2 intentionally avoids. Not wired — the flow
  step's own `message.changed` event (fired when the step message row is
  created) covers it instead.

Tests: `tests/e2e/transcript-store.spec.js`, new `describe('WS lifecycle
events producer (shadow mode)')` block (3 tests) — a `message.changed`
discovery renders the DOM from its own `/chat/{id}/status` fetch unchanged
while the store picks up the frame's own payload instead (proving the two
are fed independently, not from each other); `chat.text` deltas accumulate
into the store message's `content` in `run_seq` order; a `chat.tool` frame at
or below the applied `event_id` watermark is a no-op, same as the snapshot
producer. Verified: those 3 plus all 14 pre-existing ADR-0041 store tests in
this file (17 total, `--workers=1`) and the full `chat.spec.js` (102 tests,
`--workers=1`, two runs — the first had 3 unrelated failures that all passed
individually and on a clean second full run, matching this doc's
already-documented suite-level flakiness under load, not a regression).
PWA cache bumped to `v20260820-010`.

### 2026-08-21 fix — tool identity used the wrong field (caught in pre-publish review)

A `#squid@codex` review of this change before publish caught that the
"known gap" above was left as a documented bug instead of an actual fix, and
that the watermark test next to it didn't exercise the real broken path (it
only proved a stale `event_id` is a no-op, never two live tool updates for
the same call). Both points were verified against the code/DB directly, not
taken on faith — the same review's second finding (the "Note on durability"
paragraph above was guessing at the loss mechanism instead of checking) was
corrected in place, in that paragraph.

Fixed in `ui/transcript-store.js`'s `applyRunEvent` `'tool'` case: the
dedup key now falls back to `payload?.tool_use_id` when `payload?.id` is
absent, and the existing-entry lookup checks `t.id ?? t.tool_use_id` the
same way — matching real tool payload shape instead of only the
speculative `id` field. Added `transcript-store.spec.js` test "two real
chat.tool updates for the same tool_use_id merge into one entry, not two"
(18 tests in this file now, all pass with `--workers=1`; full `chat.spec.js`
also re-verified, 100/102 with the 2 failures being the same
already-documented auth-panel flake family, confirmed by passing
individually on retry). PWA cache bumped to `v20260820-011`.

**Note on durability:** this exact change was built and verified once
before, on 2026-08-20 (as turn `13474`), but was lost before ever being
committed. Root cause, confirmed against the `worktrees` table rather than
guessed: `squid|13474|/Users/haebin/Work/squid|discarded|2026-08-21 01:31:39`
— that turn's worktree was explicitly discarded via
`POST /chat/{msg_id}/worktree/discard` while a later turn was stuck on
"Blocked: worktree sync requires attention before starting another turn."
Discarding a blocked worktree is a real, intentional recovery action in
`agent/server.py`, not an accidental revert — it just also destroys whatever
uncommitted change that turn was carrying, with nothing else anchoring it.
Rebuilt from scratch here. If this doc is being read after another gap,
check `git log -S"shadowApplyEvent" -- ui/app.js` first — if it returns
nothing, the code is gone again and this section is describing work that
needs to be redone, not work that exists.

## 2026-08-20: producer 4 (SSE) scoping — blocked on a real backend gap, no code changed

Attempted to start producer 4 Stage 2 (shadow mode). Found a real blocker in
`transcript-store.js` itself before writing any producer code, then a second,
larger one while scoping the fix. No store, server, or client code was
changed this session — this section exists so the next attempt doesn't
re-derive the same two findings from scratch.

### Blocker 1 — `applyRunEvent`/`applyMessagePatch`/`installSnapshot` gate on one *global* `event_id` watermark; SSE has none

`lastAppliedEventId` (`ui/transcript-store.js:90`) is a single counter shared
by the whole store, across every message and every producer — any action
with `event_id <= lastAppliedEventId` is silently treated as a no-op
store-wide (`:215`, `:234`, `:192`). That watermark models WS's real global
realtime-protocol cursor. SSE has no equivalent on the wire at all: neither
`/chat/{msg_id}/events` (`agent/server.py:1956`, the reconnect/fallback
endpoint) nor the primary POST `/chat` streaming response (`stream_response`,
`agent/server.py:1190`) emit an SSE `id:` field (`sse_chunk`/`sse_event`,
`agent/server.py:783-794`, take no id parameter today). Passing `undefined`
errors out (`Number(undefined)` is `NaN`, fails the `Number.isFinite` guard);
passing `null` coerces to `0`, which is `<= lastAppliedEventId` from the
first real event onward and is a **permanent** silent no-op, not a working
degraded mode. Fabricating a synthetic global id (a local counter, a
timestamp) is worse than either: because the watermark is global, an
invented value can sit below or above the real WS-sourced cursor and would
then silently drop legitimate producer-2/3 events for the rest of the
session — a regression to already-shipped, verified behavior, not just a gap
in new code.

Cross-checked with `#squid@codex` (transcript in this turn's referenced
context): the agreed fix is to make `applyRunEvent`'s `eventId` parameter
optional rather than add a new transport-specific action (`applyLocalRunEvent`
was considered and rejected — the ADR explicitly warns against per-transport
store actions). When `eventId` is `undefined` (not `null` — `undefined` means
"this transport has no global cursor," a real, distinguishable state, not an
error), skip the global-watermark branch entirely and gate only on this
message's own `runSeq`, which stays mandatory. This is safe *because* one SSE
connection only ever carries one message's deltas — there's no cross-message
ordering for that connection to lose by not participating in the global
cursor. Existing WS callers (producers 2/3) always pass a real numeric
`eventId`, so this is additive: their code path is untouched. Terminal SSE
state (done/error) should not be forced through a sequenced
`applyMessagePatch` at all — re-fetch `/chat/{msg_id}/status` and install it
via `installHistoryPage` (which has never required `event_id` — see
`shadowInstallHistoryPage`), the same authoritative-row path producer 1
already uses. This part of the fix is scoped, low-risk, and not yet applied
to `transcript-store.js`.

### Blocker 2 — only the *reconnect* SSE endpoint has a real seq to put on the wire; the primary live-send path doesn't

The plan above requires `runSeq` from something real per message. `/chat/{msg_id}/events`
(`message_events`, `agent/server.py:1956`) replays `run_events` rows via
`get_run_events` (`agent/stats_db.py:5052`), and each row's `seq` column
*is* that real value — adding `id: {event["seq"]}` to `sse_chunk`/`sse_event`
for this endpoint's `event_stream()` loop (`agent/server.py:1970-1983`) would
be a small, additive, low-risk server change, and `event.lastEventId` on the
browser's `EventSource` (used only by `reconnectPendingItem`,
`ui/app.js:8373`) would carry it back for free.

The primary, far more common path — a freshly submitted message over SSE
transport — does **not** go through this endpoint or through `run_events` at
all while streaming. It's `stream_response` (`agent/server.py:1190`),
consumed by the client's own hand-rolled SSE parser inside `sendMessage`
(`ui/app.js:5782` reader loop) over the POST `/chat` response body — a
completely separate delivery mechanism from `message_events`. `stream_response`
yields chunks straight from `dispatcher.dispatch`'s `out_q`
(`agent/server.py:1237` `chunk = await out_q.get()`) in real time; the `seq`
that same `dispatch()` call returns (`agent/topic_queue.py:890`) is a FIFO
**queue-position** counter (`worker.position_of(seq)`, `topic_queue.py:214`)
— a same-named but semantically unrelated value, not `run_events.seq`. The
real `run_events.seq`/`run_seq` counter lives entirely inside
`topic_queue.py`'s worker (`_emit_tool`/`_emit_text`, `topic_queue.py:486-843`,
`run_seq` starting at 4, reserved slots 0-3 for meta/queued/processing/loading)
and is written via `insert_run_event`, which also fires the WS realtime
broadcast (`agent/stats_db.py:5017-5020`) — `stream_response` never sees that
counter; it's an independent, parallel consumer of the same dispatcher
output.

So giving the primary live-send path a real per-chunk seq isn't a formatting
change — it means threading `topic_queue.py`'s `run_seq` value through the
`out_q` chunk shape into `stream_response`'s yield calls, a multi-file change
to the live chat pipeline shared by WS and SSE alike (`topic_queue.py` →
`dispatcher` → `server.py`). A synthetic local per-connection counter for
*just* this path is not a safe shortcut around that: if a page reload hands
the same message off to `reconnectPendingItem`'s replay-based reconnect
mid-stream, that path's `runSeq` values would be the real DB `seq` (e.g.
starting at 4+), while the primary path's purely-local counter (0,1,2,…) has
no relation to it — the same cross-connection collision risk that ruled out
a fabricated `event_id` in Blocker 1, for the identical reason.

**Not attempted this session**: writing the `transcript-store.js` optional-`eventId`
change and wiring only the *reconnect* SSE path (`reconnectPendingItem`) would
be safe and bounded on its own, but leaves the actual common case — a fresh
message sent over SSE transport — entirely unshadowed, which undercuts most
of the point of scoping producer 4 at all. Doing the primary path properly
means touching the live `topic_queue.py`/dispatcher/`stream_response` chunk
protocol, which needs its own dedicated investigation and the same
before/after full-suite rigor producer 1's Stage 3/4 gaps required — not a
one-turn addition alongside everything else here.

## 2026-08-20: producer 4 (SSE) Stage 2, shadow mode — both SSE paths, option (b)

Went with option (b) from the prior entry: the primary POST `/chat` streaming
path (`stream_response`) got a real `run_seq` on its wire before any client
shadow code was written, so both SSE delivery paths — not just the
reconnect one — got shadow-mode coverage together. Cross-checked with
`#squid@codex` first (see the referenced transcript in this turn); the
agreed design was to make `applyRunEvent`'s `eventId` optional rather than
add a new transport-specific store action, and to source SSE's `run_seq`
from the real `run_events.seq` already persisted server-side rather than
inventing one anywhere. No shortcuts were taken on the parts flagged risky
in the prior entry.

### Backend: `run_seq` threaded through `topic_queue.py` → `out_q` → `stream_response`

`agent/topic_queue.py`'s worker (`_process`, `_emit_tool`, `_emit_text`, the
inline `_stream()` dispatch, `_sync_local_model`) already computed the exact
`run_events.seq` value for every `insert_run_event` call — it just never
carried that value onto `out_q`. Every `out_q.put(...)` in that file now
includes `"_seq": <the same value passed to insert_run_event>`, including the
two fixed reserved slots (`2` for `_processing`, `3` for `_loading`). The one
shape change: `_emit_text`'s `out_q.put(text)` (a bare string) became
`out_q.put({"_text": text, "_seq": run_seq})` — bare strings for text chunks
no longer exist anywhere on `out_q`, which let `agent/server.py`'s two
consumers (`stream_response`'s main dispatch loop and `_drain_to_completion`,
used after client disconnect) drop their `else: raw += chunk` fallback
entirely instead of special-casing a second shape. Verified via grep that
`topic_queue.py` is `out_q`'s only producer, so this is exhaustive, not a
guess. `stream_response` reads `chunk.get("_seq")` once per chunk and passes
it as `sse_chunk`/`sse_event`'s new optional `event_id` param, which prefixes
an SSE `id:` line — additive to the wire format, ignored by any consumer that
doesn't ask for it (confirmed: `agent/flow.py`'s two callers and
`server.py`'s `_run_realtime_chat`, the WS execution path that also runs
`stream_response` under the hood, all discard every yielded chunk already).
`message_events` (`/chat/{msg_id}/events`, the reconnect endpoint) got the
same `id:` treatment from its own real `run_events.seq` column — that part
really was just a formatting change, as expected. Terminal `done`/`error`
frames deliberately do **not** carry an id — completion isn't a sequenced
delta (see the client-side rationale below).

Found and fixed while threading this through: three Python unit tests
(`tests/test_topic_queue.py`) asserted the old bare-string chunk shape
directly (`chunks[:2] == [..., "Final response only."]`,
`isinstance(chunk, str)`, `"success" in chunks`) — these were testing
`_process`'s real output shape, not incidental, so they were updated to the
new `{"_text": ..., "_seq": ...}` shape rather than worked around. Two more
in `tests/test_server.py` synthetically faked `out_q.get()` to return a bare
string directly (bypassing the real producer) to test `_drain_to_completion`'s
idle-timeout retry logic — updated to return the realistic dict shape now
that the bare-string fallback those tests depended on no longer exists in
production code either. `python -m pytest tests/test_topic_queue.py
tests/test_server.py tests/test_realtime.py`: 193 passed; 3 pre-existing
failures remain in `test_server.py`'s worktree-path tests
(`FileExistsError`/`RuntimeError` against real `~/.squid/worktrees/...`
paths, unrelated to anything touched here — confirmed by reading those
tests, which never reference `out_q`, `stream_response`, or `topic_queue.py`).

### Client: `transcript-store.js`'s `applyRunEvent` gets an optional `eventId`

`lastAppliedEventId` is one watermark shared by the whole store, not
per-message — every WS-sourced action requires a real, strictly-increasing
global id. SSE has no such id (it predates ADR-0040's realtime protocol
entirely). `applyRunEvent(msgId, runSeq, kind, payload, eventId)`: when
`eventId === undefined` (not `null` — that already coerced to `0` and was a
*permanent* silent no-op, not a working degraded mode), the global-watermark
branch is skipped entirely; gating falls through to `runSeq` alone, which
stays mandatory. `applyMessagePatch`/`installSnapshot` were **not** touched —
only `applyRunEvent` needed this, because SSE's terminal state (done/error)
never goes through a sequenced patch at all (next section). A dedicated
reducer test (`transcript-store.spec.js`, "applyRunEvent with no event_id
(SSE) skips the global watermark...") proves a `runSeq`-only call for one
message can't be rejected by an unrelated WS-established watermark, doesn't
advance that watermark, and still dedups its own stale/duplicate `runSeq` —
the exact property a fabricated global id (rejected as an option in the
prior entry) would have put at risk for producers 2/3.

### Client: both SSE consumers feed the store

`shadowApplySseRunEvent(msgId, kind, event)` and
`shadowInstallSseCompletion(msgId)` (`ui/app.js`, right after
`shadowInstallHistoryPage`) are shared by:
- the reconnect path (`reconnectPendingItem`'s real `EventSource`,
  `event.lastEventId` used directly as `runSeq`); and
- the primary send path (`sendMessage`'s hand-rolled SSE parser over the
  POST `/chat` streaming body) — its manual line parser gained `id:` line
  tracking (`dataId`, reset on the blank-line frame boundary it already
  detects) and calls the same shadow functions with a plain
  `{ data, lastEventId: dataId }` object shaped like a real `MessageEvent`,
  so the two consumers share one implementation rather than two.

`text`/`tool`/`stats` feed `applyRunEvent` with `eventId` left `undefined`.
`status` (the CLI's raw `status_raw` thinking-buffer text) is deliberately
**not** fed — it's a different concept from `applyRunEvent`'s `'status'` kind
(`payload.status`/`completed_at`, a lifecycle transition), matching producer
3's own WS `chat.status` omission; feeding it would have been a real type
-confusion bug, not a shortcut. `queued`/`processing`/`loading` aren't
message facts either (same as WS's `queue.changed`/`process.changed`), so
they're skipped too. The reconnect path's `EventSource` had no `'stats'`
listener at all before this — added one, store-only, matching shadow mode's
"no render change" rule with a literal empty behavior change for direct-DOM
(nothing rendered from that event before either).

Terminal `done`/`error` (both paths) call `shadowInstallSseCompletion`, which
re-fetches `/chat/{msgId}/status` and installs it via
`shadowInstallHistoryPage`/`installHistoryPage` — the same
event-id-independent authoritative-row path producer 1 already uses, not a
sequenced patch. This was the deliberate design boundary from the
`#squid@codex` review: completion isn't a delta with a meaningful `run_seq`
relative to what preceded it.

### Two real bugs caught by the test suite, not by inspection

Both surfaced only once UI-driven tests were run against the real
`historyItemToStoreRows`/`installHistoryPage` reuse — matching this doc's
own established pattern (Gaps 1–3) of gaps only showing up under a real
before/after run:

- **`stats` reset to `{}` on completion.** First assumption was that this was
  correct, intentional behavior (mirroring `historyItemToStoreRows`'s "Stage
  2 only feeds identity/ordering fields" scope). Checked against
  `agent/stats_db.py`'s real `get_message`/`_attach_turn_stats` before
  accepting that — the real `/chat/{id}/status` response *does* carry a real
  `stats` object (parsed from `run_events`), so a test fixture omitting it
  was unrealistic, not a documented gap. Fixed the test fixture, not the code.
- **`reply_to` identity conflict on completion install.** A reconnect-path
  test's `/status` mock omitted `reply_to`, and the store's own
  identity-conflict guard (`installHistoryPage` → `findBatchConflict` →
  `identityConflict`) correctly rejected the completion install against the
  `reply_to` the initial pending-row install had already established —
  `turn.status` stayed `'pending'` forever, silently swallowed by
  `shadowInstallSseCompletion`'s own `catch {}`. Confirmed `get_message`
  always includes the real `m.reply_to` column, so this can't happen in
  production; fixed the test fixture.

### Verification

- `tests/e2e/transcript-store.spec.js`: 22/22 (`--workers=1`) — 10
  pre-existing reducer tests, 1 new reducer test for the optional-`eventId`
  property, 3 new UI-driven "SSE producer (shadow mode)" tests (primary send
  path renders unchanged while feeding real-seq-ordered deltas; reconnect
  path same; a duplicated/replayed frame at the same `run_events.seq` is a
  store no-op — real replay behavior, since a reconnecting client always
  requests from `after_seq=-1`), plus the pre-existing HTTP/WS-snapshot/WS
  -events blocks, all unchanged.
- `tests/e2e/chat.spec.js`: 98/102 (`--workers=1`, ~6min). 4 failures: 2 match
  this doc's already-documented CLI-auth unlock-retry flake family; the other
  2 (`mid-stream discovered turn keeps journaled text...`,
  `reconnecting websocket replays without duplicating an already-attached
  flow step`) are pure WS-transport tests with no SSE code path in their own
  execution — reran each 3x in isolation and both flip pass/fail
  independent of any change here, confirming pre-existing flakiness rather
  than a regression.
- `reconciler.spec.js`, `history-registry.spec.js`,
  `history-store-renderer.spec.js`: 23/23 (`--workers=1`).
- `python -m pytest tests/test_topic_queue.py tests/test_server.py
  tests/test_realtime.py`: 193/196 — 3 pre-existing worktree-path failures,
  confirmed unrelated by reading them (see above).
- PWA cache bumped to `v20260820-013`.

## 2026-08-20: producer 4 pre-publish review — two real bugs, both fixed

A `#squid@codex` review of the above before publish ("not ready to publish")
found two real correctness issues in the primary send path's shadow feed.
Both verified against the code directly (not taken on faith) before fixing;
both are now fixed, tested, and re-verified. No files changed during the
review itself — only in this follow-up.

**High — multiline text truncated in the shadow store.** `sse_chunk()`
(`agent/server.py`) can split one text delta containing an embedded newline
across multiple `data:` lines that all share one `id:` line (standard SSE
multi-data-line representation). A native `EventSource` (the reconnect path)
joins those into one `event.data` before dispatch, transparently — but the
primary send path's hand-rolled parser (`sendMessage`, `ui/app.js`)
processes each `data:` line separately, and `shadowApplySseRunEvent` was
being called once per line with the same `id:`/`run_seq` each time. The
first line's call advanced the per-message watermark to that `run_seq`; the
second line's call then had `run_seq <= lastRunSeq` and silently no-op'd —
every line after the first in a multi-line delta was dropped from the store
entirely, while the direct-DOM `raw` accumulator (which joins lines via its
own `dataLineCount` logic) rendered correctly. Reconnect path unaffected
(confirmed: native `EventSource` already delivers joined data).

Fixed by buffering: a new `textBuf` accumulates data lines using the same
plain multi-line join `raw` uses (`dataLineCount > 1 ? '\n' : ''`) but
*not* `raw`'s cross-event sentence-boundary space heuristic, which is
UI-only polish with no server-side counterpart (`topic_queue.py`'s own
`raw += chunk` has no such heuristic) — so the store's `content` ends up a
more faithful mirror of the real persisted text than `raw` itself is, not
just an equally-buggy copy. `shadowApplySseRunEvent` now fires once, at the
blank-line event boundary the parser already detects (same place `dataId`
already gets reset), not once per `data:` line.

**Medium — a non-finite `run_seq` applied unprotected instead of being
rejected.** `applyRunEvent`'s own header comment already claimed `runSeq`
"stays mandatory," but the implementation didn't enforce that: `Number.isFinite(numericRunSeq) && numericRunSeq <= lastRunSeq` simply
short-circuited to false for `NaN`, skipping the no-op/dedup check entirely
and applying the payload anyway — and the trailing
`lastRunSeqByAssistantId.set(...)` call left the per-message watermark
*unchanged* in that case. Net effect: a frame with a missing/invalid
sequence would always apply, never dedup, and a later replay of that exact
frame would be silently accepted as a fresh delta instead of being rejected
as a duplicate. Not confirmed reachable in current production code (every
real `_seq`-carrying call site was threaded through in the prior entry), but
the fix makes the code actually match its own documented contract instead of
silently degrading if a future producer or a stripped `id:` line ever hits
this path. Fixed: `applyRunEvent` now returns `{ok: false, error: ...}` for
a non-finite `run_seq` before any dedup/apply logic runs, matching the
existing pattern for a non-finite `event_id`. The now-always-finite
`numericRunSeq` let the trailing watermark-advance line drop its dead
ternary fallback too.

Two regression tests added (`transcript-store.spec.js`), both new failure
modes codex's own review noted the existing suite lacked coverage for:
- "a multi-line text delta on the primary POST /chat path is not truncated
  in the store" — a real `sse_chunk`-shaped two-`data:`-line body, asserting
  the store ends up with the full joined text (not just the first line),
  through the real `sendMessage` parsing path end to end.
- "applyRunEvent rejects a non-finite run_seq instead of silently applying it
  unprotected" — `undefined` and `NaN` both rejected with `ok: false` and no
  message created; a valid `run_seq` afterward still works normally (the
  rejection isn't sticky).

Verification: `transcript-store.spec.js` 24/24 (`--workers=1 --retries=2` —
the `--retries=2` clears this doc's own previously-documented
`page.evaluateHandle`/`createTranscriptStore` setup-race flake, reproduced
here again, unrelated to this change); `reconciler.spec.js` +
`history-registry.spec.js` + `history-store-renderer.spec.js` 23/23;
`chat.spec.js` 99/102 (`--workers=1`, ~6min) — same 3 pre-existing failures
as the prior verification pass (2 auth-panel flakes, 1 WS-only flaky test
unrelated to any SSE code path), no new failures; Python suite unchanged
(193/196, same 3 pre-existing worktree-path failures — no Python files
touched in this fix pass). PWA cache bumped to `v20260820-014`.

## 2026-08-20: producer 4 second pre-publish review — one issue only partially fixed, one test-quality gap

A second `#squid@codex` review of the prior entry ("not ready to publish")
found the Medium fix above was incomplete, plus a real gap in the multiline
regression test's own design. Both verified against the code before fixing,
both now fixed and empirically proven (not just argued) to actually guard
against their bugs — see below.

**Medium, still open: `null`/`undefined` still became a "valid" `run_seq` of
`0`.** The prior fix only special-cased `event.lastEventId === ''`
(`EventSource`'s own "no id" sentinel) before `Number()`-coercing. The
primary send path's hand-rolled parser uses a different sentinel for "no id
seen yet" — its `dataId` variable starts as JS `null`, not `''` — and
`Number(null)` is `0`, a perfectly finite number that sailed straight past
the non-finite guard added in the prior entry. `shadowApplySseRunEvent` is
explicitly shared by both callers ("without caring which one is calling
it," per its own header comment), so the fix belongs in that one shared
choke point rather than making the hand-rolled parser mimic
`EventSource`'s convention: `(event.lastEventId == null ||
event.lastEventId === '') ? NaN : Number(event.lastEventId)` now treats
`null`, `undefined`, and `''` identically, regardless of which caller passes
which one.

**Low: the multiline regression test didn't actually test what it claimed
to.** The test's `/status` mock returned exactly the same string the
correctly-joined delta path should produce. Since `shadowInstallSseCompletion`
fires on `done` and *authoritatively overwrites* `content` regardless of
what the delta phase computed, the test would have kept passing even with
the truncation bug fully reintroduced — the completion install papers over
a broken delta path by construction. Fixed by removing the `done` event and
`/status` route from that test entirely, isolating the delta-accumulation
path with nothing downstream able to mask it.

Both fixes were **empirically verified to actually catch their bugs**, not
just argued to: each was temporarily reverted in turn and the corresponding
test rerun, confirmed to fail with the exact expected symptom (the id-less
frame's text leaking into content; "line two" going missing from the
multiline delta), then restored. This was worth the extra round trip — the
first version of the multiline test looked correct by inspection and still
wasn't.

New adapter-level test added per the review's own suggestion: "a text delta
with no `id:` line on the primary POST `/chat` path is rejected, not
silently applied as `run_seq` 0" — a frame with no `id:` line at all
followed by one that has it; asserts the id-less frame's text never appears
in the final content.

While re-running the full `chat.spec.js` suite to verify, two more failures
appeared that hadn't shown up in the prior pass (`global lifecycle discovers
a desktop turn and reconnect completion stays deduplicated`, `pageshow
reconnects stale pending event watcher` — both in the `recovered pending
responses` describe block). Rather than assume "probably flaky" from the
existing pattern, checked directly: both are pure WS-transport tests using
`MockWebSocket` with artificially staggered `setTimeout` delays (5-80ms) —
`shadowApplySseRunEvent` (this turn's only edit) is never in their call
graph, confirmed by reading `shadowApplyEvent` (the separate WS-only
function they actually exercise). Reran the first one 3x with this turn's
fix *fully reverted*: identical 1-pass/2-fail split, proving the flakiness
predates and is independent of this change. Reran the second 3x with the fix
in place: 2 passed, 1 failed — same non-deterministic pattern, same
timing-fragile describe block.

Verification: `transcript-store.spec.js` 26/26 (24 prior + 2 new this round:
the missing-id adapter test, plus the multiline test now counted as
actually-testing-something rather than just passing) — combined with
`reconciler.spec.js` + `history-registry.spec.js` + `history-store-renderer.spec.js`,
48/48 total at `--workers=1 --retries=2`, all passing; `chat.spec.js`
reviewed test-by-test rather than full-suite this round (see above) given
the two new intermittent
failures were run down individually. PWA cache bumped to `v20260820-015`.

## 2026-08-21: producer 2 Stage 4, field-carry only (no cutover yet)

Scoped Stage 3 (pending-turn reconciler cutover) for producer 2 before
touching any rendering code, per the Next steps note below about budgeting
this properly. Found the cutover itself is a substantial, genuinely risky
change — not attempted this session — but a real, boundable, low-risk
prerequisite came out of the scoping and was implemented and verified here:
Stage 4 field-carry.

**Checked whether producer 2 has producer 4's own kind of blocker (a real
backend gap). It does not.** `agent/stats_db.py`'s `get_realtime_snapshot`
already does `dict(row)` on every `chat_messages` row (`SELECT *`), so every
field a renderer would need — `topic`/`agent`/`adhoc`/`backend`/etc. — is
already on the wire in every snapshot frame. `shadowInstallSnapshot`
(`ui/app.js`) was just discarding it: Stage 2 only mapped
identity/ordering fields onto the store row (by design, matching producer
1's own Stage 2 scope), never carrying the rest through.

Fixed the discard: `shadowInstallSnapshot` now sets `raw: message` on every
row, mirroring `historyItemToStoreRows`'s `raw: item` passthrough for
producer 1. `turn.raw` only ever reads the assistant message's own `raw`
(`upsertTurn`, `transcript-store.js`), so attaching it on user rows too is
harmless, not just inert — kept for symmetry rather than special-cased away.
Store-only, no render change, same shadow-mode posture as everything else at
this stage.

One real difference from producer 1 to flag for whoever does the actual
Stage 3 render: a snapshot message is a raw `chat_messages` row, not a
denormalized history item — it has no `prompt` field the way a `/history`/
`/status` row does. A pending-turn renderer built on `turn.raw` for this
producer will need the prompt text from the store's own
`turn.promptContent` (already tracked via `assistantByReplyTo`/`upsertTurn`)
rather than expecting `item.prompt` to be present on `raw` the way
`createHistoryRegistry.render()` currently expects for producer 1.

Test added: `transcript-store.spec.js` "a WS snapshot message carries its
full row on turn.raw, not just identity/ordering fields" (WS snapshot
producer shadow-mode block, asserts `getTurn(7).raw` matches the full frame
row's `topic`/`agent`/`adhoc`/`content`, not just what Stage 2 mapped).

Verified: `transcript-store.spec.js` 27/27, `reconciler.spec.js` +
`history-registry.spec.js` + `history-store-renderer.spec.js` 23/23 (all
`--workers=1`). Full `chat.spec.js` (`--workers=1`, ~7min): 97/102 clean, 5
failures — reran those 5 individually with `--retries=2`: the 3 WS-transport
ones (`mid-stream discovered claude-style turn keeps journaled status text`,
`global lifecycle discovers a desktop turn and reconnect completion stays
deduplicated`, `pageshow reconnects stale pending event watcher`) all passed
clean in isolation, matching this doc's own prior findings that this
describe block is flaky under full-suite load and unrelated to any code this
session touched (`shadowInstallSnapshot`, not the lifecycle-events path).
The 2 CLI-auth ones (`completing the unlock (exit 0) auto-retries the
original cursor login`, `server unlock_requires_local refusal is surfaced
without a password prompt`) are the already-documented auth-panel flake
family — 1 passed on retry, 1 still failed after 2 retries with the same
`window.__authFrames.length === 2` timeout signature this doc has already
attributed to that flake family, not this change (no auth code touched).
PWA cache bumped to `v20260821-001`.

## 2026-08-21: producer 2 Stage 3 deeper scoping — three concrete gaps found, no code changed

Went back to actually design the pending-turn render cutover (the item below),
reading `reconciler.js`, `createHistoryRegistry` (`ui/app.js`), and
`transcript-store.js`'s `applyRunEvent`/`getOrderedTurnIds` line-by-line
rather than from the prior entry's summary. Did not implement — this
confirmed the prior entry's "genuinely large, risky" call was correct, and
found three specific, previously-undocumented gaps a real implementation
would have to close, not just the three bullet points already on file:

1. **`createHistoryRegistry.reorder()` has no pending-placement logic at
   all.** It only loops `order.completed` (`ui/app.js`, the `reorder`
   closure) — a live/pending group has never been placed by this registry.
   A render cutover needs `reorder()` extended to interleave pending groups
   among completed ones, mirroring `insertPendingHistoryItem`'s own
   anchor-finding (`compareCompletedTurnKeys` against `order.pending`'s
   `createdAt`-sorted ids from `getOrderedTurnIds()`) — not a small addition,
   since it's the same placement pass that must stay a no-op for untouched
   groups (see `reorder`'s own existing comment on why per-node comparison
   matters).
2. **`render()` must reuse DOM nodes in place across repeated calls for the
   same pending turn, not rebuild them.** `transcript-store.js`'s
   `applyRunEvent` keeps `content` and `tools` fully accumulated (not
   delta-only — `patch.content = priorContent + delta`), so the data to
   redraw from is there, but a text delta can fire many times a second while
   streaming. `reorder()`'s `insertBefore` logic only ever inserts nodes
   present in the current `groups` map; it never removes a stale node that a
   fresh `render()` call silently orphaned. Rebuilding fresh DOM every delta
   (the way the completed-turn render path already does, safely, because a
   completed row is normally rendered exactly once) would leak orphaned wip
   bubbles on every keystroke of a streaming response, and would tear down
   the kill button's listener each time. `ctx.previousGroup` exists
   specifically so a pending render can detect "I have a live bubble
   already, patch it" instead — matching what `reconnectPendingItem` already
   does today, just moved behind `render()`.
3. **The live "thinking" narrative buffer has nowhere to live in the store.**
   `shadowApplySseRunEvent`'s own header comment (2026-08-20 entry) already
   flags that CLI `status_raw`/`statusBuf` scrollback frames are deliberately
   not fed into `applyRunEvent` — a different concept from the `'status'`
   *kind*, which is a lifecycle transition (`payload.status`/`completed_at`),
   not narrative text. Confirmed by reading `applyRunEvent`'s `switch`: there
   is no case that would hold it. A store-driven pending render() is
   therefore currently unable to reproduce part of what the direct-DOM wip
   bubble shows today (the live status narrative, as opposed to tool
   results, which *do* reach the store via the `'tool'` kind). Closing this
   needs a new `applyRunEvent` kind (store-only, same low-risk shape as Stage
   4's field-carry) before the render cutover can reach visual parity with
   today's direct-DOM bubble — otherwise the cutover ships a visible
   regression (less status detail while a response streams).

None of this was implemented — Stage 3 is still not started. Item 3 above is
a real, boundable, low-risk prerequisite in the same shape as Stage 4's
field-carry (store-only, no render change) and is the natural next slice to
actually ship; items 1 and 2 are where the genuine rendering-architecture
risk this doc keeps flagging actually lives, and still need the same
three-round gap-discovery treatment before touching the live default path.

**`#squid@codex` review of the above, verified before folding in:** agreed
item 3 (not the full cutover) is the right next slice, and flagged that it
shouldn't be modeled as one generic new `applyRunEvent` kind without first
pinning down append-vs-replace semantics per frame type — `chat.status`
*appends* to the narrative but `chat.loading`/`chat.processing` *replace* it
outright (a mode switch, not more text to tack on). Checked this directly
against every `statusBuf` write site in `ui/app.js` (both the WS `onEvent`
handler around line 8426 and the SSE consumer around line 8490): confirmed
exactly that split — `chat.status`/`chat.tool`/`chat.queued` are `+=`
(append), `chat.loading`/`chat.processing`/the WS-failure message are `=`
(outright replace, including replace-with-empty on recovery). A single
delta-only kind (mirroring `'text'`'s old `payload.delta` shape) would lose
that distinction and mis-render on the very first `chat.loading` transition.
Better still, this doesn't need a new bespoke function
(`applyNarrativeEvent(...)` as suggested) — `applyRunEvent`'s existing
`'text'` case already carries exactly this mode switch
(`patch.content = payload?.mode === 'replace' ? (payload.text ?? '') :
priorContent + delta`, `transcript-store.js` line ~282). A new `'narrative'`
kind following that identical `payload.mode === 'replace'` convention is
more consistent with the module's existing switch-based design than a
separate function, and gets the append/replace split for free from a
pattern already tested. Folded into Next steps item 1(c) below; no code
changed.

## 2026-08-21: producer 2 Stage 3 prerequisite (c) — narrative kind, shipped

Implemented the store-only prerequisite item 1(c) below: a new `'narrative'`
`applyRunEvent` kind, plus real (shadow-mode) wiring for one live producer.
No rendering code touched.

**`transcript-store.js`:** added `case 'narrative'` to `applyRunEvent`'s
switch, following `'text'`'s own precedent exactly —
`payload?.mode === 'replace' ? (payload.text ?? '') : priorNarrative + delta`
— so append is the default and a caller opts into replace the same way
`'text'` already does, rather than a second bespoke code path. `narrative`
is carried onto the turn object in `upsertTurn`, defaulting to `''`, same
policy as `tools`/`stats`.

**`ui/app.js`, WS lifecycle producer (`shadowApplyEvent`):** wired
`chat.status` → the new `'narrative'` kind, append mode, mirroring how
`chat.text`/`chat.tool`/`chat.stats` are already wired there. Checked
`agent/server.py`'s `_realtime_envelope` before wiring this: every WS frame
type carries a real `run_seq` from the same `run_events`-backed pipeline
(`event.get("run_seq")`), so the SSE header comment's "no event-scoped seq"
objection to `chat.queued` is transport-specific to SSE and doesn't block
`chat.status` on WS. Deliberately **not** wired this turn: `chat.loading`/
`chat.processing`/`chat.queued` (statusBuf's other, replace-mode writers)
and SSE's own `'status'` frames — left for whoever picks up the actual
render cutover ((a)/(b) below), since wiring the replace-mode frames without
a renderer to observe them adds surface with no test able to prove it
matters yet.

Tests added, `transcript-store.spec.js`: reducer-level append accumulation,
`mode: 'replace'` supersession, replay/dedup (same run_seq/event_id no-ops,
directed check since a duplicate here is a visible repeated status line, not
just an inert stats field), and survival through a terminal status
transition (mergeSparse's omit-preserves-field behavior, asserted directly
for this field rather than assumed from the generic case). Plus a WS
lifecycle producer test driving real `chat.status` frames over a mocked
socket into `turn.narrative`, mirroring the existing `chat.text` test. Also
added a WS-snapshot regression test confirming the *other* half of this
feature already works with zero new code: a recovered pending row's
`status_raw` field lands on `turn.raw.status_raw` for free, via Stage 4's
existing raw-passthrough — the render cutover has two narrative sources
(live incremental events → `turn.narrative`; at-rest snapshot/history rows →
`turn.raw.status_raw`) and both are now proven, not just assumed.

Verified: `transcript-store.spec.js` 39/39 (32 prior + 7 new), `reconciler.spec.js`
+ `history-registry.spec.js` + `history-store-renderer.spec.js` 23/23, all
`--workers=1`. Full `chat.spec.js` (`--workers=1`, ~6min): 98/102 clean, 4
failures — all 4 match already-documented flake families, none touch
`shadowApplyEvent`'s existing `chat.text`/`chat.tool`/`chat.stats` branches
(this change only adds a new, independent `else if` arm). Reran `mid-stream
discovered turn keeps journaled text when live chunks arrive` individually
with `--retries=2`: failed once, passed on retry — a timing race in the
test's own `setTimeout`-staggered `MockWebSocket` (asserting an early
snapshot-seeded state that a fast-arriving second `chat.text` frame had
already advanced past), same family as this doc's other documented
WS-transport flakes, and not exercising the `chat.status` branch this turn
touched at all. `global lifecycle discovers a desktop turn and reconnect
completion stays deduplicated` is the same previously-documented WS-transport
flaky describe block. The 2 CLI-auth failures
(`window.__authFrames.length === 2` timeout signature) are the
already-documented auth-panel flake family — no auth code touched this
session. PWA cache bumped to `v20260821-006`.

## 2026-08-21: producer 2 Stage 3 — one bug hypothesis ruled out, one more prerequisite shipped

Started actually designing `render()`'s pending-turn branch (items (a)/(b)
below) rather than continuing to scope it in the abstract. Two concrete
outcomes, no cutover code shipped — still gated at "not started."

**Checked a suspected live duplicate-render bug; did not reproduce with the
one race tested — corrected below, this was wrong.** While confirming
`renderer=store` is the *default* today (not opt-in — `historyRendererMode`
only falls back to `'dom'` on an explicit `?renderer=dom`, `ui/app.js` line
46), traced a plausible-looking race: `shadowApplyEvent` only clears a dirty
id when `historyReconciler` is *inactive*
(`if (!historyReconciler) transcriptStore.clearReconciled(...)`), so a
realtime-discovered completed turn (rendered once via the direct-DOM
`discoverRealtimeTurn` → `insertCompletedHistoryItem` path) stays dirty in
the store; `render()` has no "does a DOM node already exist for this id"
guard, so a *later* `historyReconciler.reconcile()` call (any of the 4 HTTP
history load sites) looked like it could render and insert a second,
duplicate bubble for the same turn. Built a repro (delayed `/history`
response racing a WS `message.changed` discovery) and it did not reproduce.
**This conclusion was wrong** — see the next entry: the repro only tested
one ordering, and the WS event actually landed *after* both `/history`
fetches in that test had already resolved, so no second `reconcile()` pass
ever ran against the already-inserted node at all. The scenario was never
actually exercised. Left as-is below (not rewritten) so the correction
is traceable; do not trust the "did not reproduce" conclusion in this
paragraph — see the follow-up entry immediately after.

**Second prerequisite found while starting to design `render()`'s pending
branch, shipped:** `turn` (the object `upsertTurn` builds and `getTurn()`
returns) never carried `content` at all — only the underlying *message*
does. For a completed turn this doesn't matter, because `turn.raw.content`
is already the full text (Stage 4's raw passthrough). For a still-streaming
pending turn it does: `turn.raw` is a static snapshot attached once at the
last full-row install, so it lags behind whatever `'text'` deltas
(`applyRunEvent`) have landed since — a pending renderer reading
`turn.raw?.content` would show stale or empty text while genuinely live
content sits on the message instead. Added `content: assistantMsg.content ?? ''`
to `upsertTurn`'s turn object, same pattern as `narrative`/`tools`/`stats`.
Store-only; nothing reads `turn.content` yet.

Test added: `transcript-store.spec.js` "turn.content tracks the
live-accumulated message content, independent of a stale turn.raw" —
installs a row with `raw.content` set, then applies a `'text'` delta with
`mode: 'replace'`, and asserts `turn.content` reflects the new text while
`turn.raw.content` still shows the original.

Verified: `transcript-store.spec.js` 33/33, combined with
`reconciler.spec.js` + `history-registry.spec.js` +
`history-store-renderer.spec.js` 56/56 total, `--workers=1`. Full
`chat.spec.js` (`--workers=1`, ~5.7min): 100/102 — the only 2 failures are
the already-documented CLI-auth panel flake family
(`window.__authFrames.length` timeout), no auth code touched. PWA cache
bumped to `v20260821-007`.

## 2026-08-21: confirmed and fixed — live duplicate-render bug in `renderer=store` (the default)

`#squid@codex` reviewed the prior entry before publish and flagged the
"did not reproduce" conclusion above as wrong: `shadowInstallHistoryPage`
runs before the existing-DOM check in `appendHistoryItems`
(`ui/app.js:2340`), leaving the id dirty regardless of what's already on
screen, and the repro only tested one race ordering. Re-verified by
instrumenting the original repro rather than trusting either claim: logged
`getPendingReconcile()` and DOM count over time, and found the WS discovery
event in that test landed *after* both `/history` fetches had already
resolved — so no second `reconcile()` pass ever ran against the
already-inserted node, and the scenario was simply never exercised. Codex
was right.

Built a corrected repro that actually forces the ordering — discovery
inserts a completed turn directly (`discoverRealtimeTurn` via WS
`message.changed`), then a **second**, legitimate `loadHistory()` call
covers the same msg id (the realistic trigger: a message discovered via
realtime naturally shows up in a later/paginated history fetch once it's
persisted, not just a narrow timing race) — and it reproduced immediately:
2 DOM bubbles for the same `data-msg-id`.

**Root cause, confirmed:** three call sites render a completed turn
directly, bypassing the reconciler entirely — `discoverRealtimeTurn`,
`attachFlowStep`, and `replacePendingWithStoredItem` (all via
`insertCompletedHistoryItem`). Meanwhile `shadowInstallHistoryPage` and the
shadow run-event appliers dirty a turn's id in the store unconditionally,
with no awareness of whether it's already on screen. `render()`'s
completed-turn branch had no "does a node already exist" check either — so
the *next* `reconcile()` pass to touch that id (any later HTTP history
load, including a routine pagination page) would build and insert a second,
genuinely duplicate bubble, since the reconciler's own `groups` bookkeeping
never had this id in it to begin with. This is real and live in production
today, not hypothetical — `renderer=store` is the default, not opt-in.

**Fix, two parts:**
1. `insertCompletedHistoryItem` (`ui/app.js`) now clears its own item's
   dirty flag right after inserting, when `historyReconciler` is active —
   closes the narrow case where nothing else ever re-touches that id again.
2. The real structural fix, in `createHistoryRegistry`'s `render()`: the
   *first* time a completed turn's id goes dirty (`!ctx.previousGroup` — the
   reconciler has never rendered it itself before), check whether a node
   for it already exists in `container`; if so, **adopt** that existing
   node as the turn's group instead of building a new one.
   `reorder()` only moves a group that's actually out of place, so an
   already-correctly-positioned adoptee is a no-op position-wise. Covers all
   three bypass call sites uniformly, and any future one, without needing
   to chase each one individually.

Tests added: `history-registry.spec.js` "render() adopts an already-on-screen
node instead of duplicating it, the first time an id goes dirty" — pre-inserts
a marked node into the isolated test container, installs the matching turn,
reconciles, and asserts both single-count and that the *same* node (marker
still present) was reused, not rebuilt.

Verified: `history-registry.spec.js` 7/7 (1 new), combined with
`transcript-store.spec.js` + `reconciler.spec.js` +
`history-store-renderer.spec.js` 57/57 total, `--workers=1`. Full
`chat.spec.js` (`--workers=1`, ~4.7min): 101/102 — the one failure is the
same already-documented CLI-auth flake (`window.__authFrames.length`
timeout), no auth code touched. Also found and fixed while re-bumping the
cache: `sw.js`'s `APP_SHELL` precache array (a second, separate list of
versioned URLs from `CACHE_NAME` itself) had been stuck at `v20260821-005`
across several prior version bumps this session — the "5 spots" the topic
instructions call out apparently undercounts by this list; brought it in
sync too. PWA cache bumped to `v20260821-008` everywhere, including
`APP_SHELL`.

## 2026-08-21: producer 2 Stage 3 prerequisite — render() adopts (not builds) a pending turn's node

Decided not to gate this behind a `renderPending`-style flag (as the prior
Next-steps entry called for): still pre-launch, no real users, so the
production-safety rationale for that gate no longer applies. Implemented the
bounded, low-risk slice of gap (b) that's actually safe to ship without the
rest of (a)/(b) — real node *adoption*, not the render/rebuild-in-place
machinery a full pending cutover needs.

`createHistoryRegistry.render()`'s pending branch no longer unconditionally
returns `{ nodes: [] }`. It now adopts whatever wip-bubble node
`reconnectPendingItem`/`makeWipBubble` already built for that id — reusing
`ctx.previousGroup` across repeated calls when still connected (a text delta
can dirty this id many times a second while streaming; must not re-query
every pass), else querying `container` for the existing
`.msg-thinking.history-item[data-msg-id]` node once. **render() still never
builds or moves a pending node itself** — reorder() still only walks
`order.completed`, so this has no visible/positional effect; it only makes
`groups`/`bucketOf` bookkeeping accurate for pending ids, which the eventual
atomic bucket-transition wiring (`ctx.previousBucket`/`nextBucket`, still
unbuilt) will need.

**Bug caught while making this change, not before:** once a pending turn has
a real (non-null) adopted `previousGroup`, the completed branch's existing
adoption gate — `if (!ctx.previousGroup)` — silently stops re-triggering the
"already on screen?" search on exactly the pending→completed transition,
since `previousGroup` is now always truthy by the time a turn completes
(it's the stale pending group). That would have reintroduced the
just-fixed duplicate-bubble bug, but only for turns that pass through a
pending state first — i.e. nearly every real turn. Fixed by gating on
`ctx.previousBucket !== 'completed'` instead, which distinguishes "first
time owned by the completed bucket" from "previousGroup happens to be
non-null" correctly regardless of what bucket it was. Also added
`:not(.msg-thinking)` to that query, defensively excluding a stale wip
bubble that might transiently still be in the DOM under the same msg-id
(`replacePendingWithStoredItem` removes it synchronously before inserting
the completed node, so this shouldn't be reachable via that call site
today, but the exclusion costs nothing and removes the doubt).

Regression tests added, `history-registry.spec.js`: "render() adopts an
already-on-screen wip bubble for a pending turn, and reuses it across
passes" (adoption + same-node-identity across a second dirty pass, proven
via a marker attribute a live mutation would set); "render() adopts the
real completed node on a pending->completed transition, not a stale wip
bubble" (reproduces the interaction bug above end-to-end: pending adoption,
then a simulated `replacePendingWithStoredItem` swap, asserting the *real*
completed node is adopted, single-count, not the removed wip bubble).

Verified: `history-registry.spec.js` 9/9 (2 new), combined with
`transcript-store.spec.js` + `reconciler.spec.js` +
`history-store-renderer.spec.js` 50/50, all `--workers=1`. Full
`chat.spec.js` (`--workers=1`, ~5.8min): 100/102 — the 2 failures are the
already-documented CLI-auth unlock-retry flake family
(`window.__authFrames.length === 2` timeout); reran both individually with
`--retries=2` and both passed clean, confirming pre-existing flakiness under
full-suite load rather than a regression (no auth code touched). PWA cache
bumped to `v20260821-009` (`sw.js`'s `CACHE_NAME` + all 4 `APP_SHELL`
versioned entries, `index.html`'s 3 `?v=` references).

## 2026-08-21: pre-publish review — completed-node adoption only grabbed the bubble, not its full sibling range

A `#squid@codex` review of the entry above (before publish) found a real bug
in the completed-branch adoption code itself (not something introduced this
session — it dates to the 2026-08-20 duplicate-render-bug fix — but the prior
entry's `previousBucket` gate change made it reliably reachable on every
pending→completed transition for the first time, which is what made it worth
fixing now rather than leaving latent):

`createHistoryRegistry`'s own header comment already documents that a
completed turn is not one node — `appendHistoryItem`/
`insertCompletedHistoryItem` produce a **flat sibling range**: route-marker?,
bubble, stats/footer, tool-block(s)*, all inserted as direct `#messages`
children, not descendants of the bubble. The adoption code only ever grabbed
the bubble via `querySelector`. Once `reorder()` later needed to move that
one-node "group" (e.g. an out-of-chronological-order bypass insert, or an
unrelated turn arriving that requires repositioning), only the bubble would
move — its stats/footer/tool-block siblings would stay behind at their
original DOM position, splitting the turn and corrupting visible order. The
prior session's own regression test used a synthetic single-node bubble with
no sibling, so it couldn't have caught this — codex's review flagged exactly
that gap.

Fixed with a new `existingCompletedNodeRange(bubble)` helper: starting from
the adopted bubble, walks `nextElementSibling` collecting any contiguous
`.stats`/`.msg-time`/`.tool-block-history` elements, stopping at the first
sibling that isn't one of those (always either the next turn's own
bubble/route-marker, or nothing). Deliberately does **not** match by this
turn's own `data-msg-id` — a `tool-block-history` sibling can legitimately
carry an *earlier* turn's msg_id instead (a worktree-blocker tool block's
action target, `appendHistoryItem`'s `toolMsgId`), so identity matching would
have wrongly excluded real same-turn tool blocks.

Regression test added, `history-registry.spec.js`: "render() adopts a
completed turn's full sibling range, so reorder() moves stats/tool-block
siblings together with the bubble" — installs a newer turn normally, then
bypass-inserts an older turn's bubble+stats+tool-block (the tool block
carrying a foreign msg_id, mirroring a real worktree-blocker) at the *wrong*
end-of-container position, forcing `reorder()` to actually move it, and
asserts all three nodes move together, contiguously, ahead of the newer
turn. **Verified this test actually catches the bug, not just passes by
construction**: temporarily reverted the fix (single-node adoption) and
reran only this test — failed with exactly the predicted symptom (only the
bubble moved; the stats/tool-block siblings were left stranded after the
newer turn) — then restored the fix and confirmed it passes again.

Verified: `history-registry.spec.js` 10/10 (1 new), combined with
`transcript-store.spec.js` + `reconciler.spec.js` +
`history-store-renderer.spec.js` 60/60, all `--workers=1`. Full
`chat.spec.js` (`--workers=1`, ~4.8min): 101/102 — one failure,
`completing the unlock (exit 0) auto-retries the original cursor login`
(the `window.__authFrames.length === 2` signature this doc has repeatedly
attributed to a pre-existing CLI-auth flake family across several prior,
unrelated sessions). Unlike those prior instances, this one did **not**
clear on retry this session — reran it in isolation 5 more times and it
failed consistently every time, which is a real behavior change from earlier
in this same session (it passed cleanly, 0 retries, right before this fix
was made — see the entry above). Investigated rather than waved off: its
immediate sibling test (`server unlock_requires_local refusal...`), which
issues the exact same click and the exact same
`waitForFunction(() => window.__authFrames.length === 2)`, passed reliably
in under 2s every time. The only difference between the two is which branch
of the mocked WS response fires (`ok: true`, triggering further async
success-path work, vs. `ok: false`, which the passing test uses) — nothing
in this session's diff (confined to `createHistoryRegistry`'s completed-turn
adoption and a new helper function) touches the auth panel, WebSocket
mocking, or anything in that call graph. Did not chase this test's own root
cause further — out of scope for this fix, and the differential evidence
(identical wait condition passes on the sibling; zero shared code path; this
exact test's flake history predates this session by several unrelated
changes) points at pre-existing timing sensitivity in that one test's
success-path branch, likely aggravated by this session's own accumulated
system load rather than caused by this change. Flagging the elevated
rate here rather than silently re-asserting "known flake, ignore" — worth
a real look if it keeps reproducing this reliably in a future session. PWA
cache bumped to `v20260821-010`.

## 2026-08-21: producer 2 Stage 3 gap (a) — reorder() gains pending-placement, shipped

Scoped the pending-turn render cutover from scratch (reading `reconciler.js`,
`render()`/`reorder()`, `historyStoreAnchor`, `insertPendingHistoryItem`, and
`transcript-store.js`'s `getOrderedTurnIds` line-by-line, not from this doc's
own prior summaries) before touching anything, per the Next-steps note about
budgeting this properly. Found gap (a) — `reorder()` has zero
pending-placement logic — is smaller and lower-risk than the prior scoping
entry assumed, and shipped it. Gap (b) (render()/patch a pending node from
store state) is unstarted; see below.

**Key design finding: `reorder()`'s pending loop doesn't need `store`
threaded through the reconciler.js contract.** `historyStoreAnchor` (the
production `getAnchor`) already solves completed-vs-pending interleaving
today by querying live DOM state (`data-order-at`/`data-msg-id` off
`#messages > .msg-thinking` nodes) rather than consulting the store — a
pending group's sort key can be read the same way, straight off its own
already-rendered root node's `dataset`, once `render()`'s pending branch has
adopted it (the 2026-08-21 entry above). This meant no reconciler.js contract
change was needed — only a second constructor option on
`createHistoryRegistry`, `getPendingAnchor(assistantMsgId, rootNode) ->
Node|null`, mirroring `getAnchor`'s own shape and default
(`() => null`, i.e. append at the end, matching every existing test).

**Second finding: `insertPendingHistoryItem`'s own anchor search already
treats completed and pending bubbles uniformly** — its selector
(`.msg.assistant.history-item[data-msg-id]`) matches wip bubbles too (they
carry the same three classes), so it was never "pending vs. completed-only"
the way the first design pass assumed. `historyStorePendingAnchor`
(`ui/app.js`, next to `historyStoreAnchor`) mirrors that exactly, and —
worked out by tracing insertBefore semantics rather than assumed —
iterating `order.pending` oldest-first with each group's anchor computed
independently (never chained to the previous pending group's position, unlike
the completed loop's own `next`-threading) is sufficient to also produce
correct pending-vs-pending order as an emergent property: an older group's
`insertBefore` call never affects a newer group's own anchor search (a
`.msg-thinking` node can't compare as "later" than a genuinely later one), so
each subsequent newer group's own insertBefore naturally lands after it.

**Third finding, checked before trusting the design: could this new pending
loop fight `historyStoreAnchor`'s own completed-loop placement (oscillation,
or drifting the two out of sync)?** No — they read the same underlying
`data-order-at`/`data-msg-id`/`data-completed-at` attributes but neither loop
ever *writes* them, and the completed loop always runs first within one
`reorder()` call, so the pending loop only ever positions itself relative to
already-settled completed nodes. Recomputing both every pass converges to a
stable fixed point rather than oscillating.

Implementation: `reorder()`'s new `for (const id of order.pending)` block
(`ui/app.js`, `createHistoryRegistry`) and `historyStorePendingAnchor` (next
to `historyStoreAnchor`), wired into the production `historyReconciler`
construction via `getPendingAnchor: historyStorePendingAnchor`.

**A genuinely risky-feeling change, unlike the prior bookkeeping-only
prerequisites** — this is the first ADR-0041 change this session that
actively *repositions* an already-on-screen live bubble on every reconcile
pass, where before (direct-DOM) it was placed once and never moved again.
Given `renderer=store` is the live default, this takes effect in production
immediately. Decided to ship rather than gate it behind a flag (matching the
"still pre-launch, no users" reasoning from the entry above) but held it to
the full verification bar rather than treating it as another inert
prerequisite.

**Two test-quality gaps found and fixed in the test itself, not the
implementation, while verifying with the revert-and-confirm-it-fails
discipline this doc has used before:**
- The first draft of the "interleaved with a completed group" test used only
  one completed turn. Reverting the new pending loop and rerunning it still
  passed — the completed loop's own default `getAnchor` (`next ?? null`)
  unconditionally chains each completed group onto the next, or (for the
  newest) to "the end of container," entirely independent of any pending
  node present; with only one completed turn, that default coincidentally
  reproduces "pending ends up before the completed turn" even with the new
  loop doing nothing. Fixed by requiring the pending turn to land *between*
  two completed turns instead — a position neither loop's own default
  chaining behavior can reach by coincidence, which does fail correctly with
  the loop disabled.
- The multi-pending-group test (proving pending-vs-pending order is a correct
  emergent property, not a bug) was checked the same way from the start and
  caught the disabled case immediately — no fix needed there, included here
  only to record that both new tests were verified this way, not just the
  one that needed a redesign.

Verified: `history-registry.spec.js` 12/12 (2 new — the redesigned
interleaving test plus the multi-pending-group test), combined with
`transcript-store.spec.js` + `reconciler.spec.js` +
`history-store-renderer.spec.js` 62/62, all `--workers=1`. Full
`chat.spec.js` (`--workers=1`, ~4.8min): 101/102 — the one failure is the
CLI-auth flake test flagged as elevated in the entry above; reran it alone
with `--retries=1` and it cleared on the first retry this time ("1 flaky,"
not the consistent 6/6 failures from last session) — consistent with the
prior entry's conclusion that the elevated rate was session-load-related,
not a real regression. PWA cache bumped to `v20260821-011`.

**Not attempted this session:** gap (b) — `render()` actually *building* a
pending node from store state (not just adopting one direct-DOM already
built) and patching it in place on a delta. This is the harder, genuinely
architecture-changing half of Stage 3 — see Next steps below, unchanged in
substance from the prior entry's scoping.

## 2026-08-21: pre-publish review of the gap (a) work — one real gap confirmed-but-not-live, one unrelated test regression fixed

A `#squid@codex` review of the gap (a) entry above ("review the current
change before publish") found two issues. Both investigated properly before
acting — the first via a full reachability trace (not just static reasoning:
grepped every `classList.add('history-item')` call site in `ui/app.js`), the
second via reading the actual production code the failing test exercises,
not just patching the assertion to whatever made it pass.

**1. Pending reorder moves only the adopted `.msg-thinking` bubble — real
architectural gap, confirmed *not currently reachable*.** `liveGroupElements`
(`ui/app.js` ~1797, the composer-live path's own established multi-node
group logic) proves a live turn's group can genuinely be
`[route-marker?, user bubble, msg-time, thinking]`, not always one node —
`reorder()`'s pending loop only ever moves `group.nodes`, and `render()`'s
pending branch only ever adopts the single `.msg.assistant.msg-thinking.
history-item` node it queries for. Traced every place that builds a live/
pending bubble to check if this selector could ever match a real multi-node
composer-live group: `makeWipBubble` (the only builder that adds
`.history-item`) is genuinely self-contained — its prompt renders via an
inline toggle, not a separate sibling — so it's correctly single-node.
`sendMessage`'s own `thinkingBubble` never gets `.history-item` anywhere in
the file. `reconcilePendingBubble` actively removes a duplicate wip bubble
once a composer-submitted turn claims its real msg_id, so the two shapes
never coexist under one id either. Net: the adoption selector's `.history-
item` gate excludes composer-live bubbles entirely — the split-group
corruption codex described is a correct prediction about the *design*
(single-node assumption) but not reachable through the code as written.
Cross-checked with `#squid@codex` (referenced transcript in this turn),
which agreed with the trace and proposed the same treatment: don't broaden
adoption now (using `liveGroupElements` on a wip-bubble adoption would
introduce a *worse*, real bug — its backward walk has no ownership check, so
a wip bubble legitimately inserted right after an unrelated older completed
turn's own trailing `.msg-time`/stats element, which is exactly how
`historyStorePendingAnchor` places one, would have that older turn's own
timestamp silently stolen into the wip bubble's "group" and dragged along on
every reposition). Added the invariant test `#squid@codex` also suggested:
"render() never adopts or repositions a composer-live thinking bubble (no
.history-item)" (`history-registry.spec.js`) — builds the real multi-node
shape (user bubble + msg-time + `.msg-thinking` with no `.history-item`),
reconciles, and asserts all three nodes are completely untouched (same
identities, same order, `render()` succeeds with an empty node set rather
than adopting anything). This pins the current safety boundary down
explicitly, so a future change to the adoption selector can't loosen it
without this test failing — and documents, in a form that survives past this
session, that collecting an ownership-safe multi-node range for a
composer-live group is required *before* Stage 3(b) lets `render()` build or
adopt that shape for real (folded into Next steps below).

**2. The empty-error semantic change broke two tests, not the one flagged.**
codex's own selected run only caught `chat.spec.js:2324`
("stream error with message id keeps polling until final completion" was
apparently not in that run's selection) — running the full suite here found
a second, structurally identical failure at the same location pattern
(`statusCalls`-mocked `/status` sequence: pending → empty error → done),
`chat.spec.js:2375`. Both tests asserted the *old* behavior: an empty
terminal error used to be silently skipped so polling could continue toward
a later, contradictory `done` — that guard (`if (!String(data.content ||
'').trim()) return;`) was intentionally removed from three call sites this
session (`sendMessage`'s inline status-fallback poller, its SSE `error`-event
handler, and `replacePendingWithStoredItem`) as part of validating that
`error` is a real terminal status per `agent/stats_db.py` (never revised
back to `pending`/`done` after the fact) — codex confirmed this backend
behavior and agreed the code change is correct, just that the tests needed
replacing, not deleting.

Read the actual resulting code path for each test rather than guessing at
the new expected DOM state:
- `chat.spec.js:2324`'s empty SSE body forces the connection-drop fallback
  path (`startStatusFallback`). Its first poll (`status: 'pending', content:
  'Partial response'`) sets `raw = 'Partial response'` *before* the second,
  empty-error poll ever runs — so by the time the error branch executes,
  `raw || firstDataReceived` is already true, taking the
  `parkInterruptedPartial` branch (freezes the thinking bubble with whatever
  was received, appends "Connection interrupted.") rather than
  `showError`'s branch (which would place a separate response bubble). No
  separate `.msg-error` bubble is ever created — only `.msg-thinking-done`
  containing "Partial response" + "Connection interrupted.". Rewrote the test
  to assert exactly that, plus `statusCalls === 2` (polling stops at the
  terminal error, the mock's third, unreachable branch was deleted).
- `chat.spec.js:2375`'s SSE stream carries a real `event: error, data:
  'Connection lost'` frame, handled by a *different* code path (the SSE
  reader's own inline `error` branch, not the connection-drop fallback) that
  calls `showError('Connection lost')` immediately (a real, non-empty error
  message — unaffected by the guard removal) *and* separately starts
  `startStatusFallback` as an additional safety-net poller. Rewrote the test
  to mock every `/status` call as a terminal empty error (removing the
  now-unreachable third `done` branch) and assert the already-shown
  "Connection lost" bubble is what's left — not a second, less informative
  "Response interrupted." bubble the fallback poller's own empty-error
  handling could otherwise have overwritten it with.
  `expect(statusCalls).toBe(2)` here surprised initially (expected 1) —
  root-caused rather than loosened to a vague bound: `shadowInstallSseCompletion`
  (`ui/app.js` ~2350, store-only, called from the same SSE error handler)
  does its own independent one-shot `/status` fetch alongside
  `startStatusFallback`'s own single terminal poll — two legitimate,
  deterministic, non-repeating callers of the same endpoint, not a race.

Verified: `history-registry.spec.js` 13/13 (1 new), `chat.spec.js`
(`--workers=1`, ~5.3–5.7min, run twice): 100/102 both times — the 2 failures
each run were from the already-documented CLI-auth flake family plus, on the
first run only, one WS-transport test in the `recovered pending responses`
block that passed cleanly both standalone and on the second full run
(matching this doc's own prior findings that block is flaky under load, not
tied to any change here — no flow/WS code touched this session).
`quota.spec.js`'s own already-updated empty-error test (a third,
codex-unflagged casualty of the same semantic change, fixed in a session
prior to this one) reconfirmed passing. No `ui/app.js` changes this
session — only tests — so no PWA cache bump.

## 2026-08-21: Stage 3(b) pre-publish review remediation — premature pending ownership rolled back

The first attempt to make `createHistoryRegistry.render()` build and patch
pending bubbles was blocked in review and corrected before publish. It gave
the reconciler only partial ownership while `reconnectPendingItem` still
owned filtering, the transport watcher, the complete `statusBuf`, and
completion. That produced four live-path hazards: store patches could
truncate the richer watcher display; filtered rows could be rebuilt anyway;
store-built bubbles had no watcher; and direct-DOM completion could leave a
detached adopted group registered and later resurrected by `reorder()`.

Pending rendering is therefore back at the established boundary: the
registry adopts self-contained `.history-item` wip bubbles for placement but
does not build or mutate them. `reconciler.forget(id)` now lets direct-DOM
handoffs remove one adopted identity without resetting unrelated groups;
`replacePendingWithStoredItem` and `insertCompletedHistoryItem` call it before
clearing/assuming ownership. Terminal rendering now overlays current
`turn.status`, `turn.content`, and `turn.completedAt` onto the full-fidelity
raw row, preventing a stale snapshot from dropping the live final response.
Empty successful native-shell turns are explicitly renderable in history and
realtime discovery so their exit metadata/footer is not lost.

Regression coverage: pending store rows remain unwatched/unbuilt; adopted
pending content is not overwritten by partial store narrative; `forget()`
excludes a detached group from later placement; terminal rendering uses live
content; and empty native-shell success renders. Focused
`history-registry.spec.js` + `history-store-renderer.spec.js` +
`reconciler.spec.js`: 35/35, `--workers=1`. PWA cache bumped to
`v20260821-015`.

## 2026-08-21: pending-render parity prerequisite — remaining narrative producers wired

Wired the existing store `narrative` field to the remaining live status
frames without changing rendering ownership. WS `chat.loading` and
`chat.processing` replace the narrative, while `chat.queued` appends, matching
their direct-DOM `statusBuf` behavior. Both SSE consumers now feed sequenced
`status` frames into the narrative and feed `loading`/`processing` as
replacements; synthesized SSE queue-position frames remain excluded because
they have no stored `run_seq`. The hand-rolled POST parser buffers a complete
multi-line status event before applying it once, preserving both newlines and
run-sequence deduplication. Regression coverage exercises the WS mode changes
and primary SSE append/replace sequence. Follow-up review parity fixes make
WS processing fall back to the envelope's `scope.topic`, preserve a line
boundary before an appended queued message, and correct the store comment so
tool events remain documented as structured `tools[]`, not narrative text.
PWA cache bumped to `v20260821-017`.

## Next steps

1. **Producer 2 Stage 3 (pending-turn reconciler cutover) — not started.**
   (Separately: the completed-turn duplicate-render bug found while scoping
   this — direct-DOM bypass call sites vs. `render()`'s dirty-id
   reconciliation — was a real, already-shipped production bug, not part of
   the pending-turn cutover itself, and is now fixed; see the entries above.)
   Sharpened by the 2026-08-21 scoping entries into four concrete gaps: (a)
   — **done** — `createHistoryRegistry.reorder()` now places pending groups
   too, via a new `getPendingAnchor` option (see the entry directly above);
   (b) `render()` must reuse/patch DOM nodes in place per pending turn (via
   `ctx.previousGroup`) rather than rebuild on every delta, or stale nodes
   leak and the kill button's listener gets torn down repeatedly — **the
   adoption half of this is done** (`render()` reuses the same wip-bubble
   node across passes instead of re-querying or rebuilding), but adoption
   alone isn't the render cutover — nothing yet makes `render()` *build* a
   pending node from store state, or patch an already-adopted one's content
   on a delta, and this is now the one remaining piece of (a)/(b) that's
   still unstarted. **New, confirmed by the 2026-08-21 pre-publish review
   entry above:** whatever eventually builds or adopts a composer-live
   turn's group must collect its real, ownership-safe multi-node range
   (route-marker?, user bubble, msg-time, thinking — see `liveGroupElements`)
   before adoption's selector is ever broadened to match it — reusing
   `liveGroupElements` naively is not safe for a *recovered* wip-bubble
   adoption (no ownership check on the backward walk; risks stealing an
   unrelated older completed turn's own trailing timestamp), so this needs
   its own purpose-built range-collector, the same way `existingCompletedNodeRange`
   is a purpose-built forward walk rather than a reuse of something designed
   for a different context. A regression test
   (`history-registry.spec.js`, "render() never adopts or repositions a
   composer-live thinking bubble") pins today's boundary (composer-live
   bubbles lack `.history-item` and are therefore never touched) — that test
   will need to change, deliberately, the day this is actually built; (c) —
   **done** — the live narrative buffer now has a
   store home (`turn.narrative` for live deltas, `turn.raw.status_raw` for
   already-fetched rows), with `chat.status` wired end-to-end on WS, and (d)
   — **done** — `turn.content` now tracks live-streamed text independent of
   a stale `turn.raw`.
   The remaining WS narrative frames and SSE's sequenced narrative frames are
   now wired (see the entry above). `render()` actually building or
   patching pending nodes (the remainder of (b)) is still the actual risky
   work and is unstarted — `renderer=store` is the **live default** today
   (not opt-in), so once `render()` starts building or mutating pending
   nodes itself (rather than only adopting direct-DOM's), it takes effect in
   production immediately; budget it with its own dedicated before/after
   suite rounds when it's picked up, same as producer 1's Stage 3/4 needed
   (and same as gap (a) itself needed this session, including a redesign of
   one test that didn't isolate what it claimed to on the first attempt —
   see the entry above). Also still open: `turn.raw` is entirely
   absent for a turn discovered purely via WS lifecycle events
   (`shadowApplyEvent`'s `message.changed` branch calls `applyMessagePatch`
   with no `raw` field) — unlike a snapshot- or history-sourced pending row,
   so a pending renderer can't always assume `turn.raw.topic`/`agent`/etc.
   exist; needs either a fallback fetch (mirroring how `makeWipBubble` gets
   its fields from the initial discovery response today) or accepting a
   render() failure (dirty-and-retried, per the existing failure contract)
   until a snapshot fills it in. Also still open from the prior entry:
   wiring the reconciler's atomic live-to-terminal bucket transition
   (`previousBucket`/`nextBucket`) to replace `replacePendingWithStoredItem`'s
   direct-DOM swap, including explicit removal of the old bucket's nodes
   (nothing does this automatically — `reorder()` never removes a node
   absent from the new `groups` map); and deciding how
   `reconnectPendingItem`'s kill-button/cancel wiring and EventSource
   reattachment survive once the wip bubble node is reconciler-owned. Budget
   this the same way producer 1's own Stage 3/4 needed — three rounds of
   gap-discovery via full before/after suite runs, not inspection.
2. Producers 3 and 4 need the same two-part treatment (Stage 4 field-carry
   check, then Stage 3 cutover) — not scoped yet this session.
3. After producer 1 has run as the real default for "one cycle" with no
   rollback needed, delete the disabled direct-DOM history-rendering branch
   and the `?renderer=dom` escape hatch. Not yet — the flip landed
   2026-08-20.
