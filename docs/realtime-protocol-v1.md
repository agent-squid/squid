# Squid real-time protocol v1

This document specifies the version 1 application protocol selected by
ADR-0040. JSON messages are carried by `/ws/v1`; SSE remains a migration
fallback. Phase one covers chat and process, queue, and message state. Flow and
CLI authentication are reserved message families for later phases.

## Ordering and cursor model

Each replayable server event is inserted into `realtime_events` and receives a
database-generated integer `event_id`. It is global to one Squid database and
totally orders publication commits. It does not claim that parallel work ran in
that order.

A connection has one applied cursor: the greatest delivered `event_id` whose
relevant effects it has installed. Subscription filters do not create separate
number spaces. Event IDs can therefore jump: a client can advance from event 40
to event 44 when 41-43 belonged to other scopes. On resume, the server scans
the global range and sends only authorized, subscribed events; it never reveals
the skipped payloads.

`run_events.seq` remains local to one assistant message. A `chat.text` payload
contains `run_seq`, allowing complete-text snapshots and later deltas to be
deduplicated independently of the global cursor.

## Envelope

Every message is UTF-8 JSON with these common fields:

```json
{
  "v": 1,
  "type": "chat.text",
  "event_id": 1842,
  "request_id": null,
  "scope": {"topic": "squid", "agent": "codex"},
  "payload": {}
}
```

- `v`: required integer protocol version.
- `type`: required message type.
- `event_id`: required on replayable server events and snapshots; absent on
  client commands and ephemeral server messages.
- `request_id`: required UUID or ULID on mutations and echoed by their result.
- `scope`: stable authorization and subscription identifiers.
- `payload`: type-specific object.

Unknown optional fields are ignored. Unknown types produce `error` with code
`unsupported_type`. Frames above the configured limit close the connection
with `frame_too_large`.

## Connection lifecycle

1. Server sends `hello` with `supported_versions`, limits, heartbeat interval,
   and current server cursor.
2. Client sends `subscribe` with version, scopes, an optional last applied
   cursor, and a persisted random `client_id` of 16-128 URL-safe characters.
3. Server authorizes every scope and responds with `subscribed`, then either
   replays a bounded contiguous range or sends `snapshot` at watermark `N`.
4. Client atomically installs events and periodically sends `ack` containing
   its greatest applied delivered `event_id`.
5. `ping`/`pong` detects dead peers. Reconnect does not cancel work.

Changing subscriptions does not rewind the cursor. A newly added scope receives
a snapshot at the current watermark; it cannot use a cursor accumulated while
that scope was unsubscribed to reconstruct unseen history.

After a publication transaction commits, the single Squid server process wakes
connections through a process-global generation counter. Connections query the
durable log after waking; notifications carry no event payload and may be lost
without affecting correctness. A 20-second safety poll uses the same drain path.
Reconnect and restart recovery always use the durable cursor.

## Commands and results

Phase-one client mutations are `chat.start` and `chat.cancel`. The server
persists the `(principal, request_id)` result before acknowledging it. A retry
returns the same `command.result`; it does not execute again. Conflicting reuse
of a request ID produces `request_id_conflict`.

For direct local/Tailscale connections, `principal` combines the authenticated
local session with the browser's persisted `client_id`; proxy source IP is not
used. Shore uses its authenticated device/session identity. `client_id` scopes
idempotency only and grants no access.

Connection commands are `subscribe`, `unsubscribe`, `ack`, `ping`, and `pong`.
They are not domain mutations and do not enter the realtime event log.

Phase-one replayable server events are `chat.meta`, `chat.queued`,
`chat.status`, `chat.loading`, `chat.processing`, `chat.tool`, `chat.text`,
`chat.stats`, `chat.done`, `chat.error`, `process.changed`, `queue.changed`, and
`message.changed`. Terminal events are idempotent by `event_id` and domain ID.

## Snapshot

A subscription snapshot contains:

- the latest 20 messages in each requested conversation scope;
- all pending operations in scope, even if older than those 20 messages;
- current process, queue, and message state;
- complete accumulated content and `run_seq` for pending assistant messages;
- durable tool results needed to render that active window.

The server reads state consistently at watermark `N`, buffers later matching
events, sends the snapshot with `event_id: N`, then sends events after `N`.
Installation replaces the subscribed client state atomically. Events at or
below the installed cursor and text deltas at or below installed `run_seq` are
ignored.

Replay rolls over to a snapshot when configured count, byte, age, retention,
continuity, restart, or compatibility limits fail.

Version 1 defaults retain events for seven days with a 100,000-row cap and
idempotency results for seven days. Cleanup never renumbers event IDs. A cursor
below the retained minimum minus one receives a snapshot.

## History boundary

Messages older than the active 20-message window are fetched through the
existing bounded HTTP history API using a stable database-page boundary. The
client merges pages and live events by message ID. Historical pages do not
alter the WebSocket cursor.

Although WebSocket can carry such responses, history is deliberately HTTP: it
is finite query traffic with independent cancellation, retry, caching, status,
and pagination semantics. It must not compete with live deltas or require a
second RPC protocol inside the socket.

## Compatibility and errors

The server supports the current and immediately previous protocol versions.
Negotiation failure returns `unsupported_version` with the supported set before
closing. Other stable error codes include `unauthorized_scope`, `invalid_frame`,
`unsupported_type`, `frame_too_large`, `request_id_conflict`, `replay_gap`, and
`slow_consumer`. Errors state whether reconnect/resume is safe and never expose
events from unauthorized scopes.

Outbound queues and frames are bounded. Coalescible state replaces older queued
state for the same domain object. Overflow involving non-coalescible events
closes with `slow_consumer`, after which the client resumes from its last ack.
