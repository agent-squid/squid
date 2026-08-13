# Postmortem: WebSocket UI migration regression

**Date:** 2026-08-12  
**Status:** Reverted before release  
**Related decision:** [ADR-0040](../decisions/0040-versioned-realtime-protocol-over-websocket.md)

## Summary

An attempted migration of new chat commands and conversation updates from
HTTP/SSE to the versioned WebSocket protocol caused the chat transcript to
flicker, reorder messages, appear to apply topic filters automatically, and
show inconsistent route turn counts.

The server protocol was not the primary failure. The browser connected
real-time conversation events to `reloadHistory()`, a destructive function
designed for deliberate history-filter navigation. Each snapshot or assistant
`message.changed` event removed most of the transcript and asynchronously
loaded the newest history page. A normal turn emits several message changes,
so one turn could repeatedly destroy and reconstruct the screen.

The migration was stashed and the application returned to the prior SSE chat
path. No persisted chat data was lost.

## User impact

- The entire chat view visibly flickered while messages changed state.
- Pending/live messages and completed history could move relative to each
  other, breaking the user's expected conversational order.
- The persistent composer topic chip influenced WebSocket subscription scope,
  making history changes look like automatic topic filtering even though the
  user had not selected a history filter.
- Asynchronous route-session fetches rewrote chip, marker, and latest-context
  turn counts after transcript rebuilds, producing stale or misplaced counts.
- Multi-agent routes multiplied refreshes because events from every subscribed
  scope could rebuild the same global transcript.

## What changed

The attempted browser migration added three coupled behaviors:

1. `setTopicChip()` synchronized WebSocket conversation subscriptions from the
   persistent composer route.
2. Initial snapshots and every assistant `message.changed` event scheduled a
   call to `reloadHistory(historyFilter)`.
3. Route turn-count refreshes mutated several already-rendered DOM surfaces
   after asynchronous session requests completed.

Individually these operations were plausible. Together they crossed boundaries
that the existing UI depends on:

- The composer route selects where a new message goes; it is not the history
  filter that selects what the transcript shows.
- `reloadHistory()` is a navigation reset, not a state-reconciliation API.
- Live groups have lifecycle and DOM-position rules that differ from paginated
  completed history.
- A turn count belongs to a specific route/session/render target, not simply
  the last matching DOM element at callback time.

## Root cause

The migration treated a real-time notification as an instruction to refetch
and rerender the visible collection. Squid's transcript is not a disposable
projection: it combines paginated history, live turns, flow markers, status,
stats, scroll anchoring, and explicitly selected filters. Reconstructing that
collection from a bounded history response cannot preserve all of those local
invariants.

The key implementation error was routing `snapshot` and `message.changed`
through `reloadHistory()`. That function intentionally:

- resets pagination and history-window state;
- removes completed messages, timestamps, stats, and route markers;
- preserves only selected in-flight groups at their current DOM positions;
- fetches a five-item page and inserts it independently; and
- reapplies the current explicit history filter.

Repeated calls therefore mixed two ordering models: preserved live DOM order
and database completion-time history order. They also exposed any accidental
coupling between subscription scope, sticky composer state, and explicit
history-filter state.

## Contributing factors

- The migration changed command transport, recovery, subscription lifecycle,
  transcript refresh, filtering behavior, and turn-count presentation in one
  browser patch.
- Protocol tests established delivery behavior but did not establish UI
  invariants under repeated snapshots and events.
- There was no dedicated client-side reconciliation layer keyed by message ID.
- Function naming did not communicate that `reloadHistory()` is destructive.
- The snapshot's bounded working set was mistaken for a replacement for the
  complete visible transcript.
- Async turn-count callbacks located render targets after awaiting network
  responses instead of retaining and validating stable identities.

## Prevention and required migration gates

### Preserve UI ownership boundaries

- Composer routing, history filtering, and real-time subscription scope must
  remain separate state. Changing a composer chip must not alter visible
  history unless the user explicitly invokes a history-filter action.
- Only explicit navigation actions may call the destructive history reset.
  Rename it to `resetHistoryForNavigation()` before further migration work so
  call sites communicate that contract.
- Transport adapters may publish normalized domain events; they must not own
  transcript navigation or broad DOM replacement.

### Reconcile by stable identity

- Apply `message.changed` to one message model keyed by `msg_id`.
- Update an existing pending bubble in place. On terminal transition, move it
  according to the existing completion-placement rule exactly once.
- Add an unknown message only when it belongs to the explicitly visible scope;
  otherwise update the cache without changing the transcript.
- Install snapshots into the subscribed state cache, then reconcile affected
  identities. A snapshot must not replace unrelated visible history.
- Bind turn-count updates to `(topic, agent, session_id)` and a stable render
  identity. Discard callbacks whose target or session is no longer current.

### Stage transport migration

Migrate one behavior at a time, retaining the previous path as a comparison:

1. Observe WebSocket events without rendering them and compare them with SSE.
2. Reattach pending messages through WebSocket using the existing renderer.
3. Reconcile terminal state for those pending messages.
4. Move new-chat submission while keeping response rendering behavior fixed.
5. Move cancellation separately.
6. Add broader conversation discovery only after explicit-scope semantics and
   collection reconciliation are tested.

Do not combine transport migration with route-count presentation changes.

### Add regression tests before enabling the path

End-to-end tests must assert all of the following while snapshots and duplicate
or repeated `message.changed` events are delivered:

- Existing message DOM nodes retain identity; the transcript is never emptied.
- Completed and pending turns preserve the specified ordering.
- Scroll position is stable unless the user was already following the bottom.
- A sticky composer topic does not activate or modify the history filter.
- Explicit topic, agent, adhoc, flow, bookmark, and bad-response filters remain
  unchanged.
- Events outside the explicit visible scope do not add or remove transcript
  rows.
- Route and context turn counts remain attached to the correct session and
  render target after delayed responses.
- Reconnect snapshots, duplicate events, and multi-scope routes are idempotent.
- The WebSocket and SSE paths produce equivalent final rendered content during
  the migration period.

The browser WebSocket path must remain opt-in until these tests pass in CI and
the current SSE suite passes unchanged.

## Recovery guidance

If a future real-time change causes collection-wide flicker or unexplained
filter/order changes:

1. Disable the new browser transport path without changing persisted protocol
   or database state.
2. Confirm whether any real-time callback calls the destructive history reset.
3. Compare the visible DOM before and after a single event by message ID.
4. Verify composer state, explicit filter state, and subscription state
   independently.
5. Re-enable only after the event is handled through targeted reconciliation.

## Lessons

Real-time delivery does not require real-time full rerendering. The durable log
defines what changed; it does not redefine which collection the user chose to
view. For Squid, correctness requires targeted, idempotent reconciliation that
preserves transcript, filter, ordering, scroll, and session identity.
