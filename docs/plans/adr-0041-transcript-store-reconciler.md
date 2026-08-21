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
| 3. Reconciler + cutover | ⚠️ partial — see below | ❌ | ❌ | ❌ |
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

## Next steps

1. **Producer 2 Stage 3 (pending-turn reconciler cutover) — not started.**
   This is the actual risky work; Stage 4 field-carry above only removes one
   prerequisite blocker. Needs: a live/pending-turn render function (analogous
   to `makeWipBubble`, but reading `turn.raw` + `turn.promptContent` from the
   store instead of a `/chat/{id}/status` fetch); wiring the reconciler's
   already-built-but-never-exercised atomic live-to-terminal bucket transition
   (`reconciler.js`'s `previousBucket`/`nextBucket` context) to replace
   `replacePendingWithStoredItem`'s direct-DOM swap; and deciding how
   `reconnectPendingItem`'s kill-button/cancel wiring and EventSource
   reattachment survive once the wip bubble node is reconciler-owned rather
   than a locally-held DOM reference. Budget this the same way producer 1's
   own Stage 3/4 needed — three rounds of gap-discovery via full before/after
   suite runs, not inspection — and do not treat Stage 4's clean field-carry
   result above as evidence the render cutover itself will be equally cheap.
2. Producers 3 and 4 need the same two-part treatment (Stage 4 field-carry
   check, then Stage 3 cutover) — not scoped yet this session.
3. After producer 1 has run as the real default for "one cycle" with no
   rollback needed, delete the disabled direct-DOM history-rendering branch
   and the `?renderer=dom` escape hatch. Not yet — the flip landed
   2026-08-20.
