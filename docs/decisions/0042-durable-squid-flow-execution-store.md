---
status: accepted
date: 2026-08-15
updated: 2026-08-15
---
# ADR-0042: Durable Squid Flow execution store

## Context

ADR-0032 defines Squid Flow syntax and execution semantics. The current v0.1
executor persists the resulting conversation in `chat_messages` and detailed
turn output in `run_events`, but it does not persist Flow execution state as
such. It reconstructs progress from transcript rows, uses an in-memory set to
deduplicate concurrent dispatch, and represents delayed steps with sleeping
async tasks. A restart can therefore lose a scheduled delay, and transcript
inference cannot unambiguously distinguish waiting, claimed, failed, or
cancelled work.

ADR-0040's Flow realtime migration needs an authoritative snapshot source and
durable events. The same state should later support a web view of scheduled and
running flows without treating the transcript as an operations database.

## Decision

Add a durable Squid Flow execution model alongside the existing transcript.
`chat_messages` remains the conversation record; the Flow store owns run and
step lifecycle, scheduling, dispatch identity, and restart recovery.

### Data model

Add two persisted entities:

- `flow_runs`: `flow_run_id`, canonical route, original prompt message,
  overall status, created/started/completed timestamps, and terminal error or
  cancellation metadata.
- `flow_steps`: stable `step_id`, `flow_run_id`, branch/leg/repeat identity,
  dependency and target metadata, status, `due_at`, dispatch key, claim time,
  attempt number, associated user/assistant message IDs, timestamps, and
  terminal error metadata.

The exact SQL belongs in the data-model documentation. These invariants are
part of this decision:

- A dispatch key is unique within a Flow run and identifies one logical step.
- A step links to at most one prepared chat turn for an attempt. Retrying a
  claim must not create a second turn for the same dispatch key.
- Dependencies refer to stable step identities, not message arrival order.
- Origin steps use `branch_index = -1`: an origin may be shared by several
  fan-out branches or participate in a join, so assigning it to one branch
  would be misleading. Target and return steps carry their actual branch.
- `flow_run_id` and `flow_step_id` remain on related transcript rows so the
  operational and conversation views can be joined without sharing ownership.
- Run status is derived from, or transactionally maintained with, step state;
  it must not drift as an independently editable summary.
- `execution_mode` is an explicit activation boundary. Pre-cutover plans are
  `shadow` and must never be claimed or recovered; executor-owned plans are
  `durable`. Only new runs may enter durable mode—shadow runs are not promoted.

### Lifecycle

Initial step states are `pending`, `scheduled`, `claimed`, `running`, `done`,
`error`, and `cancelled`. Initial run states are `scheduled`, `running`,
`completed`, `failed`, and `cancelled`.

A step becomes eligible only when its persisted dependencies are terminal in
the required successful state. The persisted plan retains a relative
`delay_seconds`; when the dependencies complete, the executor materializes the
step's absolute `due_at` transactionally. A delayed step without `due_at` is
not claimable, and process sleep time is only a wake-up optimization. Claiming
eligible work is an atomic conditional database update. The claimant then
idempotently prepares the associated chat turn and records its message IDs
before dispatch.

On startup, and at a periodic safety interval, the single supported server
process scans for due scheduled work, unclaimed eligible work, and stale claims.
Recovery reconciles a claim with its linked message/queue state before either
continuing or reclaiming it. It never assumes that process death means the
external agent did not start. No automatic retry policy is introduced by this
ADR; an error is terminal unless a later decision defines retry semantics.

Cancellation is persisted at the run or step boundary and prevents new claims.
Cancellation of an already running chat turn uses the existing authorized chat
cancellation mechanism. A run reaches a terminal state only when no step can
still be claimed or run.

### Transaction and publication boundary

Creating or transitioning a Flow step and writing its corresponding durable
realtime event occur in the same database transaction. `realtime_events` is
the transactional outbox; notification occurs only after commit. A crash may
delay delivery, but cannot expose an event for state that was not committed or
permanently hide a committed transition.

ADR-0040 owns the `flow.*` wire schemas, replay, snapshots, and browser polling
migration. The initial required event is `flow.step.created`; later status
events should be added when the web operations view consumes them.

### Query boundary and web visibility

Bounded HTTP endpoints provide Flow run lists and run details, including
scheduled time, current step, completed steps, linked messages, and terminal
errors. WebSocket events update an already loaded view. History and filtering
remain HTTP concerns; the WebSocket is not used as a general Flow query API.

The durable schema must support this visibility, but building a complete Flow
monitoring UI is not required for the first persistence/realtime milestone.

### Migration

Existing transcript-only Flow runs remain readable as legacy history. They are
not backfilled into synthetic operational state because their intermediate
lifecycle and scheduling facts cannot be recovered reliably. New runs use the
durable store. Boot recovery continues to recognize incomplete legacy v0.1
runs during a bounded compatibility period.

Plans recorded before executor cutover use `execution_mode = shadow`. They are
excluded from every claim, due-time materialization, and recovery query and are
pruned at startup when older than seven days. Cutover creates only new runs as
`durable`, preventing already transcript-executed shadow plans from being
dispatched again.

## Consequences

- Good: scheduled and active flows survive server restarts with explicit state.
- Good: live completion and boot recovery share one idempotent dispatch claim.
- Good: realtime snapshots and a future web Flow monitor have an authoritative
  source independent of DOM or transcript inference.
- Good: transcript rendering remains based on ordinary messages.
- Bad: run/step transitions and transcript creation require coordinated
  transactions and reconciliation logic.
- Bad: stale-claim recovery must account for ambiguous external-process state.
- Constraint: the supported deployment remains one server process; this is a
  durable scheduler boundary, not a distributed worker system.

## Required verification

Tests must cover atomic duplicate claims, restart before and after chat-turn
preparation, restart during a delay, stale-claim reconciliation, cancellation
races, dependency gating, terminal run-state calculation, event-after-commit
ordering, snapshot consistency, and legacy-run compatibility. Replaying or
reprocessing the same dispatch key must never create a duplicate logical step
or chat turn. Delay tests must prove that `due_at` is based on dependency
completion, is materialized once, and survives restart without early or
duplicate dispatch.
