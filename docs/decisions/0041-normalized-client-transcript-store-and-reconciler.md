---
status: proposed
date: 2026-08-13
updated: 2026-08-20
---
# ADR-0041: Normalized client transcript store and reconciler

## Context

ADR-0040 introduced one versioned real-time application protocol and, in its
"Client state and transcript reconciliation" section, specified invariants
the browser must uphold: messages keyed by globally unique `msg_id`, one live
registration per pending message with an atomic terminal transition,
composer route and explicit history filter kept separate from delivery, and
completed turns ordered by `completed_at` with `msg_id` as tiebreaker.

The [2026-08-12 WebSocket UI migration postmortem](../postmortems/2026-08-12-websocket-ui-regression.md)
found those invariants violated in practice, not because the wire protocol
was wrong, but because the browser routed live events through
`reloadHistory()`, a destructive function, and had no dedicated
reconciliation layer keyed by message identity. Its "Prevention and required
migration gates" section requires targeted, idempotent reconciliation but
does not mandate a specific client architecture to achieve it.

Today `ui/app.js` satisfies (or fails to satisfy) these invariants through
discipline at each call site: history loading, live bubbles, filters, flow
markers, tool state, scroll anchoring, and real-time recovery each mutate
`#messages` directly, independently re-deriving correct behavior for every
new timing combination. For example, the initial HTTP history page and global
snapshot discovery could both write the collection during bootstrap; each
path deduplicated by `msg_id`, but their competing insertion anchors could
still leave an older completed turn at the bottom. A bootstrap barrier fixed
that timing, but not the broader multiple-owner architecture.

A normalized client state layer with one idempotent reconciler would enforce
these invariants once, as data-structure properties, instead of re-verifying
them per call site and per new producer. Nothing prevents building it. The
constraint is migration risk: the breadth of existing direct DOM ownership
and incomplete regression coverage. Building it inside the still-in-flight
WebSocket transport migration would recreate the exact failure pattern the
postmortem describes — multiple coupled behavioral changes landing in one
patch.

## Decision

Introduce one normalized, transport-agnostic in-memory transcript store and
one idempotent reconciler that is the only code path allowed to apply
store-owned mutations to `#messages`. HTTP history, SSE, WebSocket snapshots,
and WebSocket events become producers that dispatch normalized actions into
the store; after a producer is migrated, it does not mutate the DOM directly.

The normalized state has three explicit layers:

- `messagesById: Map<msg_id, Message>` owns server message facts. A user prompt
  and assistant response remain separate messages linked by `reply_to`.
- `turnsByAssistantId: Map<assistant_msg_id, Turn>` derives the renderable turn
  group, including its prompt message, assistant message, tools, stats, and
  flow/route metadata. DOM-only rows such as timestamps and route markers are
  projections of a turn, not independent message records.
- `transcriptView` owns the explicit filter/search mode, loaded page IDs and
  boundaries, active-window membership, and scroll-follow/anchor state. The
  composer route remains separate composer state; it is an input to sending a
  turn, never transcript-view state.

Session metadata and route turn counts are not transcript-message facts. They
remain in a separate session cache keyed by `(topic, agent, session_id)` and
may update a turn projection only after validating that complete identity and
the stable render target. This ADR does not move them into `messagesById`.

The store and reconciler enforce, as invariants of the data structure rather
than per-call-site discipline, the properties already required by
ADR-0040's "Client state and transcript reconciliation" section and the
postmortem's "Reconcile by stable identity" gates:

- exactly one canonical message record per `msg_id` and one turn projection per
  assistant `msg_id`;
- at most one live registration for a pending `msg_id`, atomically replaced
  by exactly one completed-history representation on terminal transition;
  repeated terminal or duplicate snapshot actions are no-ops;
- completed turns ordered by authoritative `completed_at` with `msg_id` as
  the deterministic tiebreaker; live ordering is independent of that;
- composer route, explicit history filter, pagination boundary, and scroll
  anchor are state separate from message facts, and no message action may
  mutate them;
- an unknown message is added to the visible collection only when it belongs
  to the explicitly visible scope; otherwise the cache updates without
  changing the transcript.

This is a client-internal architecture decision. It does not change the wire
protocol, `docs/realtime-protocol-v1.md`, or the server-side event and
persistence model defined by ADR-0040.

### Actions, identity, and merge semantics

Every producer adapts its input to a small shared action vocabulary rather
than inventing transport-specific store operations:

- `installHistoryPage(page, boundary)` merges a bounded HTTP page and records
  its pagination boundary without replacing messages outside that page;
- `installSnapshot(snapshot, event_id)` merges authoritative active-window
  state at its declared watermark;
- `applyMessagePatch(msg_id, patch, event_id)` merges lifecycle metadata;
- `applyRunEvent(msg_id, run_seq, kind, payload, event_id)` applies ordered
  text, tool, status, and stats data for one assistant message; and
- `setVisibleScope(scope)` is an explicit navigation action, never emitted by
  a transport adapter.

Actions are applied serially. The store records the last applied global
`event_id` and the last `run_seq` per assistant message. Events at or below
those applicable watermarks are no-ops. `event_id` controls transport
application and `run_seq` controls one run's deltas; neither controls display
order. A terminal message state is monotonic: a duplicate or older pending
patch cannot reopen it. Sparse event patches never erase fields they omit,
while history pages and snapshots declare which fields are authoritative.
Malformed or identity-conflicting actions fail before advancing or
acknowledging their cursor.

Each successful store transaction adds affected assistant message IDs to a
store-owned `pendingReconcile` set. The reconciler touches only those
turn groups unless an explicit navigation action changes the visible
collection, and removes an ID only after that group reconciles successfully.
A duplicate event that is already reflected in message state still returns
any outstanding dirty IDs, so a prior rendering failure can be retried rather
than being hidden by event deduplication. Cursor persistence and
acknowledgement happen only after the store transaction succeeds and all dirty
IDs required through that cursor reconcile successfully.

### Collection and DOM ownership

One turn group has one stable render-registry identity keyed by the assistant
message ID. The reconciler owns the complete ordered node set for that key:
the user prompt, live/completed assistant representation, route marker,
timestamp, tool blocks, stats, and other ancillary nodes. The implementation
may use a wrapper element or a keyed DOM range, but must not require each
ancillary row to rediscover its owner from adjacency after an asynchronous
operation. This prevents route markers and tool rows from being orphaned when
the assistant representation changes without forcing a particular DOM shape.

Completed groups are ordered by `(completed_at, assistant_msg_id)`. Pending
groups use their authoritative creation/queue order and are not silently mixed
into completion order. On terminal transition the reconciler atomically
removes the live registration and watch, replaces the live projection, and
places the completed group at its completion-order position.

Pagination is membership state, not message ownership. Merging a page may add
IDs to the visible window but cannot remove live turns or records learned from
realtime delivery. A bounded snapshot updates its declared active window; it
does not claim that older paginated records are absent. Cache eviction, when
needed, may remove only records outside all loaded pages, the active window,
and live operations, and must not mutate the visible transcript as a side
effect.

### Migration sequence

Staged, with the previous direct-DOM path retained as a running comparison
at every step:

1. Add the store and action reducer. No producer feeds it yet; no rendering
   changes.
2. Feed one producer at a time into normalized actions in shadow mode. The
   existing direct-DOM path remains the sole renderer while tests compare its
   observable result with the store projection. Shadow mode must not perform a
   second DOM write.
3. Add the single idempotent reconciler and switch one producer at a time to
   render from the store instead of mutating `#messages` directly. Exactly one
   renderer owns a given turn group at any instant; a feature flag selects the
   old or new owner for rollback, never both.
4. Move completion ordering, pending registration, route-marker placement,
   and deduplication into the reconciler as each producer switches over.
5. Disable a producer's direct `#messages` mutation only after its invariant
   and shadow-equivalence tests pass against the store-driven path; remove the
   disabled code after the rollback window.
6. Repeat per producer until no direct DOM mutation path remains outside the
   reconciler.

This work begins only after the current WebSocket chat-submission migration
(ADR-0040's "Migrate new chat submission and streaming" step) reaches its
required parity gates. It is not bundled with the remaining ADR-0040
migration steps (Flow, CLI authentication) — those may proceed against the
existing direct-DOM path and get migrated onto the reconciler afterward, one
producer at a time, per step 6 above.

## Benefits

The primary benefit is that transcript correctness becomes a property of one
state model and one write path, rather than a convention every asynchronous
producer must reproduce. Concretely, this architecture:

- prevents collection-wide flicker and loss of local UI state because a live
  event reconciles only affected turn IDs instead of destroying and rebuilding
  `#messages` through `reloadHistory()`;
- makes command responses, history pages, SSE, snapshots, replay, reconnect,
  and duplicate events converge on the same result regardless of arrival order,
  eliminating a broad class of timing-combination bugs;
- guarantees one visible turn and one live registration per message identity,
  so terminal transitions cannot leave duplicate responses, stale watchers, or
  orphaned route, tool, stats, and timestamp nodes;
- preserves the user's selected history scope, pagination window, scroll
  anchor, and composer route independently, preventing delivery events from
  accidentally changing navigation or filtering state;
- produces deterministic completion ordering from authoritative data rather
  than DOM insertion timing, so reconnects and delayed events do not reorder
  the conversation unpredictably;
- gives every transport and future producer one small normalized action
  contract, reducing integration work and preventing transport-specific DOM
  behavior from accumulating again;
- enables fast reducer-level permutation and idempotency tests for invariants
  that otherwise require slower, less precise browser tests; and
- narrows failures and rollback to one producer or renderer owner at a time,
  reducing the risk of completing the WebSocket migration and later client
  changes.

The resulting user-visible benefit is a stable transcript under streaming,
reconnect, pagination, and filtering. The engineering benefit is that adding a
new event source no longer requires re-solving transcript identity, ordering,
deduplication, terminal cleanup, and DOM ownership at each call site.

## Consequences

- Good: transcript identity, ordering, deduplication, and lifecycle invariants
  have one implementation and one owner. Session-count races still require the
  separate identity validation described above.
- Good: transports and future producers integrate through normalized actions
  without acquiring DOM ownership.
- Good: reducer tests can verify state invariants directly, while end-to-end
  tests remain responsible for the final projection and user-visible behavior.
- Good: reconciler risk is decoupled from transport migration risk; each can be
  verified, shipped, and rolled back independently.
- Bad: old and new implementations temporarily coexist per producer for
  comparison and rollback, although only one may render a turn group at a
  time. This adds cost on top of ADR-0040's dual-transport period.
- Bad: further refactor investment beyond what ADR-0040 required, layered
  onto an already multi-stage migration.
- Bad: the same `reloadHistory()`-style coupling could reappear at the
  action-authoring level if a new store action does not respect the
  boundary invariants; the reconciler constrains rendering, not action
  authorship.

## Required verification

- All invariant and regression tests already required by ADR-0040's
  "Required verification" section and the postmortem's "Add regression
  tests before enabling the path" section must pass against the
  store-and-reconciler path for every producer it has taken over.
- A development-mode assertion (per ADR-0040's "Development builds should
  enforce these invariants where practical") flags any mutation of a
  store-owned message region outside the reconciler.
- During a producer's migration window, the store-driven and direct-DOM
  paths must produce equivalent normalized turn groups and final DOM for the
  same event sequence, mirroring ADR-0040's SSE/WebSocket parity requirement.
- A producer switches onto the reconciler only after its existing
  end-to-end suite passes unchanged against the store-driven path.
- Reducer tests must permute history pages, snapshots, sparse lifecycle
  patches, and run events and prove duplicate suppression, monotonic terminal
  state, field-preserving sparse merges, `event_id`/`run_seq` handling, and
  deterministic `(completed_at, msg_id)` ordering.
- Reconciler tests must prove stable turn-group registry identity, atomic live
  to terminal replacement, ownership of route/tool/stats ancillary nodes,
  pagination merge without active-window loss, retention and retry of dirty
  IDs after a simulated render failure, and cursor acknowledgement only after
  successful reconciliation.

## Relationship to ADR-0040

This ADR narrows and implements ADR-0040's "Client state and transcript
reconciliation" section as one concrete client architecture, rather than a
set of invariants upheld separately at each call site. ADR-0040 continues to
own the wire protocol, event production, and snapshot/replay semantics;
ADR-0040 points to this ADR for the client-side store and reconciler design
rather than duplicating it.
