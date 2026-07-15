---
status: accepted
date: 2026-07-15
---
# ADR-0033: Terminal Status Capture for Cancelled Turns

## Context

`chat_messages` rows go through `pending` → a terminal status. Before this
change the only terminal statuses were `done` and `error`, and cancellation
(`POST /cmd stop`, `stopall`, `deq`, `stop_msg`) was purely a process signal:
Squid killed the subprocess but never wrote anything to the row. A cancelled
turn stayed `pending` until either:

- the worker's own completion handler eventually wrote to it (racing the
  kill), or
- the server restarted and `mark_orphaned_pending()` swept it into `done`
  (if a final run-event snapshot existed) or `error` (if not).

Consequences of that gap:

- Cancelled turns were invisible in Stats until a restart, and even then
  indistinguishable from a genuine mid-flight crash.
- Aggregate/by-turn/by-topic/by-agent stats queries only counted rows that
  had a `stats` run_event, i.e. the CLI had gotten far enough to report usage.
  A turn killed before that point (including one killed before it even
  started, e.g. via `/cmd deq`) didn't show up in Stats at all — not as a
  turn, not as a count, nothing.
- There was no completion timestamp. Time-bucketed stats keyed off
  `created_at` (dispatch time), which is wrong for any turn that finishes
  much later than it was queued.

The goal: make every terminal outcome — done, error, or cancelled —
immediately visible and correctly time-bucketed in Stats, without waiting on
a restart-time sweep.

## Decision

1. **New terminal status**: `chat_messages.status` can now be `cancelled` in
   addition to `pending` / `done` / `error`.

2. **New column**: `chat_messages.completed_at` (TEXT, ISO8601). Set once,
   via `COALESCE`, the first time a row reaches any terminal status — never
   overwritten by a later terminal write. Backfilled onto existing databases
   via a guarded `ALTER TABLE` in `init_db()`.

3. **`mark_assistant_cancelled(msg_id, reason)`**: a pending-only UPDATE —
   `WHERE status = 'pending'`. Before writing the terminal row it reconstructs
   every available assistant-side attribute from `run_events`: partial text
   becomes `content`, streamed status/thought trace becomes `status_raw`, tool
   events become assistant `context`, and a `session_id` from stats events is
   used if the row does not already have one. If any of those are unavailable,
   the free-text reason fills `content`/`status_raw`. Because it only matches
   pending rows, it's a no-op if the turn already finished, so a cancel request
   racing a real completion can never clobber a done/error result.

4. **Known resumed `session_id` is attached at dispatch time**:
   `POST /chat` copies the existing `topic_sessions.session_id` onto the
   newly-created assistant row before the CLI starts. This preserves JSONL /
   run-event traceability for resumed turns that are cancelled before any
   stats or final message update arrives. This attachment is metadata-only:
   it does not assign `session_turn_index`, which remains tied to the normal
   stats/final-completion path.

5. **Cancellation is recorded at stop-request time, before the kill signal**:
   - `stop_topic` / `stopall_topic` (backing `/cmd stop` and `/cmd stopall`)
     call `mark_assistant_cancelled` for every actively-running `msg_id` in
     scope *before* calling `kill_procs_by_topic`.
   - `/cmd stop_msg` does the same for the single targeted `msg_id`.
   - `TopicWorker.drain()` (backing `/cmd deq` and internal queue clears)
     marks queued-but-not-yet-dispatched items as cancelled
     (`"Cancelled before start"`) before dropping them from the queue, so
     turns that never ran at all still get a terminal row.

6. **Final-write races are resolved by `only_if_pending`**: the worker's own
   completion write (`update_assistant_message(..., only_if_pending=True)`)
   only succeeds if the row is still `pending`. Whichever writer — the cancel
   request or the worker's natural completion — reaches the pending row
   first wins; the loser's write is silently dropped. Squid does not attempt
   to preserve output produced between the cancel mark and process death if
   the worker loses that race.

7. **Stats queries count terminal rows even without a `stats` run_event**.
   `get_aggregated_stats`, `get_stats_by_turn`, `get_stats_by_breakdown`,
   `get_stats_by_agent`, and `get_stats_by_topic` all gained a parallel
   "count-only" query path (`LEFT JOIN` instead of requiring a `stats`
   event) that counts any `chat_messages` row with
   `status IN ('done', 'error', 'cancelled')`, with zeroed usage fields when
   no usage was ever reported. Each aggregate now reports `done_turns`,
   `error_turns`, and `cancelled_turns` alongside `total_turns`.

8. **Time bucketing prefers `completed_at`**: queries that previously bucketed
   `chat_messages` rows by `created_at` now use
   `COALESCE(cm.completed_at, cm.created_at)`, so a turn that sat in queue
   or ran long is bucketed by when it actually finished.

9. **SSE replay treats `cancelled` like a terminal error**:
   `GET /chat/{msg_id}/events` emits an `error` event (with the cancellation
   reason as the message) when it sees `status='cancelled'`, so a client
   reconnecting mid-stream gets a clean stream close instead of hanging on a
   `pending` row forever.

10. **UI**: `cancelled_turns` and `error_turns` were added as selectable
   Stats chart metrics and table measures ("Cancelled" / "Errors"),
   sum-only (they're counts, not distributions — no avg/percentile).

## Consequences

- Every terminal outcome is visible in Stats immediately, including
  zero-output kills — no server restart or orphan sweep required.
- `completed_at` gives an accurate finish time for time-bucketing,
  independent of both dispatch time (`created_at`) and whether a `stats`
  run_event ever arrived.
- Cancelled/errored turns with no `stats` run_event show zeroed
  tokens/cost/duration in by-turn drilldowns — they contribute to
  `cancelled_turns`/`error_turns` counts, not to usage totals.

**Deferred from the original design** (see prior discussion) — this pass
covers the DB/stats plumbing, not the full durable-event-log design:

- **No `cancel_requested` / `cancelled` `run_events` rows.** The terminal
  state lives only in `chat_messages.status`. There's no replayable event
  marking *when* a cancel was requested versus when the process actually
  died — just the one `completed_at` timestamp.
- **No structured failure/cancellation payload.** Cancellation reconstructs
  partial text/status/tool/session data that already reached `run_events`, but
  still does not write `{kind, exit_code, signal, stderr_tail}`. Errors from
  process crashes still rely on whatever `status_raw` the harness driver
  already produced.
- **No raw JSONL capture on kill.** Partial thoughts/tool traces already
  written to `run_events` before the kill are preserved (they're a separate,
  untouched log), but nothing new is drained from stdout/stderr *after*
  SIGTERM before the process is force-killed.
- **`mark_orphaned_pending()` (crash/restart recovery path) was not
  updated** — it still writes `done`/`error` directly without setting
  `completed_at`. A turn recovered this way falls back to bucketing by
  `created_at`, which can be significantly earlier than when it actually
  ended. This path is now the rarer of the two (in-process cancel/kill no
  longer depends on it), but it's an open inconsistency.
