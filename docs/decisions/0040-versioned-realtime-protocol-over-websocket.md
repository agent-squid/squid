---
status: proposed
date: 2026-08-12
updated: 2026-08-12
---
# ADR-0040: Versioned real-time application protocol over WebSocket

## Context

Squid's live features currently use several transport patterns:

- `POST /chat` returns an SSE stream, while `GET /chat/{msg_id}/events`
  replays persisted run events after a disconnect.
- CLI authentication uses an SSE output stream plus separate HTTP requests
  for input, resize, and cancellation (ADR-0035).
- Squid Flow clients poll for newly created steps.
- Process, queue, and message status are refreshed with HTTP requests.

These work locally, but do not provide one bidirectional channel for immediate
multi-device updates. ADR-0039's Shore broker needs a host connection and a
browser connection that can remain open, carry commands and streamed output,
and resume after ordinary mobile network changes. Defining a separate broker
protocol while retaining unrelated local SSE behavior would create two live
application protocols and two sets of lifecycle semantics.

Moving every HTTP operation to WebSocket would create the opposite problem.
Static assets, login and recovery, history and search queries, configuration
CRUD, health checks, and large file transfers are naturally bounded HTTP
requests. Treating WebSocket as a generic RPC replacement would add connection
state and retry ambiguity without improving those operations.

## Decision

Introduce one versioned, transport-independent real-time application protocol,
carried over a single WebSocket endpoint (`/ws/v1`) for direct local and
Tailscale access. ADR-0039's Shore path will relay the same logical protocol
between browser and host, with its additional authentication and end-to-end
encryption requirements.

WebSocket becomes the primary transport for live commands and updates, not a
replacement for the whole HTTP API. Existing HTTP and SSE routes remain
available during a staged migration and consume the same underlying event
producers. SSE is removed only after WebSocket reaches behavioral and test
parity.

### Protocol envelope

Every frame is a bounded JSON envelope. Binary payloads and large files are
transferred by HTTP rather than embedded in frames.

```json
{
  "v": 1,
  "type": "chat.text",
  "event_id": 1842,
  "request_id": "01J...",
  "msg_id": 417,
  "payload": {}
}
```

- `v` selects the protocol version. Unsupported versions fail explicitly.
- Every client mutation has a globally unique `request_id`. Retried mutations
  are idempotent and return the prior result rather than executing twice.
- Every replayable server event has a database-global, monotonically increasing
  `event_id`; domain-local sequences remain explicit payload fields.
- Domain identifiers such as `msg_id`, `flow_run_id`, and `session_id` remain
  explicit fields; clients must not infer identity from arrival order.
- Frames have documented size limits. Unknown optional fields are ignored;
  unknown required message types receive a protocol error.

Initial message families are:

- Connection: `hello`, `subscribe`, `unsubscribe`, `resume`, `snapshot`,
  `ack`, `ping`, `pong`, `error`.
- Chat: `chat.start`, `chat.cancel`, `chat.meta`, `chat.queued`, `chat.status`,
  `chat.loading`, `chat.processing`, `chat.tool`, `chat.text`, `chat.stats`,
  `chat.done`, `chat.error`.
- Flow and state: `flow.step.created`, `process.changed`, `queue.changed`, and
  `message.changed`.
- CLI authentication: `auth.start`, `auth.input`, `auth.resize`, `auth.cancel`,
  `auth.output`, and `auth.done`.

Exact schemas belong in the API specification, not only in this ADR.
The version 1 wire contract is defined in
[`docs/realtime-protocol-v1.md`](../realtime-protocol-v1.md).

An attempted browser migration on 2026-08-12 exposed UI ownership and
reconciliation requirements that are not wire-protocol concerns. Its failure,
impact, and required migration gates are documented in the
[WebSocket UI migration postmortem](../postmortems/2026-08-12-websocket-ui-regression.md).

### Implementation status (2026-08-12)

The ADR is partially implemented. The current browser use is deliberately
narrower than the server protocol: WebSocket reattaches pending assistant
messages after page load or reconnect, while the ordinary new-chat path still
submits and streams over SSE.

| Area | Status | Current behavior |
| --- | --- | --- |
| `/ws/v1`, v1 envelopes, `hello`, version rejection | Implemented | The server exposes one JSON WebSocket endpoint and accepts protocol version 1. |
| Subscribe, unsubscribe, scoped replay, snapshot fallback | Partial | Topic/agent scopes, cursor replay, a 500-event rollover, retained-cursor rollover, and 20-message snapshots exist. Scope authorization, replay byte/age/compatibility/gap limits, and a transactionally buffered snapshot boundary do not. |
| Durable global event log and wake-up | Implemented | `realtime_events` supplies global cursors; commits wake sockets through a process-global generation notifier with a 20-second safety poll. Seven-day/100,000-event cleanup exists. |
| Chat event publication | Implemented on the server | Persisted run events publish `chat.queued`, `chat.loading`, `chat.processing`, `chat.status`, `chat.tool`, `chat.text`, `chat.stats`, `chat.done`, and `chat.error`; message mutations publish `message.changed`. |
| Running-message browser reattachment | Implemented | The UI prefers WebSocket snapshots/events for pending messages, reconnects with jittered exponential backoff, persists its cursor, sends acknowledgements, and falls back to SSE when WebSocket is unavailable. |
| New chat submission and cancellation | Partial | The server implements idempotent `chat.start` and `chat.cancel` commands and persisted results. The browser still uses the existing HTTP/SSE submission path and does not send these commands over WebSocket. |
| Process and queue state | Partial | Snapshots include current process and queue state, but no `process.changed` or `queue.changed` events are published or consumed by the UI; existing HTTP refresh remains authoritative. |
| Flow | Not implemented | `flow.step.created` is not published or consumed; the browser polls for new Flow steps. |
| CLI authentication | Not implemented | `auth.*` messages are not implemented; ADR-0035's SSE-plus-HTTP transport remains in use. |
| Backpressure and frame limits | Not implemented | Sends are direct and there is no bounded/coalescing outbound queue, `slow_consumer` handling, or configured inbound frame-size enforcement. |
| Heartbeat and acknowledgements | Partial | `ping` receives `pong` and the UI sends `ack`, but the server does not initiate heartbeat pings or use acknowledged cursors to manage delivery. |
| Protocol compatibility | Partial | Unsupported versions fail explicitly, but only v1 is supported rather than the current and immediately previous versions. |
| Shore relay | Not implemented | ADR-0039's broker transport, pairing, encryption, capabilities, and audit work remain future work. |

SSE endpoints therefore remain required. WebSocket is currently the primary
transport only for reattaching an already-running message, not for all live
commands and updates described by this decision.

### Event production and persistence

Execution must be separated from transport:

```text
runner/dispatcher -> persist event -> publish event -> SSE and WebSocket
```

The runner, topic dispatcher, and authentication session publish domain events
without writing to a socket. A slow or disconnected client therefore cannot
stall or cancel an agent process.

The existing `run_events` log remains the durable, per-message source for
detailed chat recovery. A separate `realtime_events` log assigns every
replayable published event a database-generated, globally increasing
`event_id`. Concurrent producers may execute in parallel, but their event-log
inserts commit in one observable order; that order, not execution start or
completion order, defines the real-time cursor. The global cursor lets one
connection acknowledge and resume a mixed set of subscriptions without trying
to compare unrelated `run_events.seq` values. Domain-local sequence numbers
remain in payloads where they are needed, especially for streamed text.

Ephemeral signals may be coalesced. Durable state is always recoverable through
a snapshot; in-memory subscriptions and socket queues are never sources of
truth.

Live delivery uses a hybrid wake-up model. The event and materialized state are
committed first; only then does the publisher increment one process-global
generation and wake connected sockets. Each socket drains the durable log from
its cursor and applies its own scope filter. A 20-second safety poll handles a
missed notification and bounds delivery if Squid is accidentally run with more
than one server process. Squid's supported deployment remains one server
process; notification is an optimization, never a source of truth.

The realtime log is retained for seven days and capped at 100,000 events by
default. Idempotency results are retained for seven days. Cleanup runs at
startup and operational maintenance points. A cursor older than the retained
minimum always rolls over to a snapshot rather than appearing caught up.

Events are incrementally rolled into authoritative current state as they are
produced:

```text
event -> persist when durable -> update materialized state -> publish live
```

Snapshots read this materialized state rather than rebuilding the current UI
by replaying its complete history. Existing `chat_messages` rows are the
starting point for current message content and status, while `run_events`
retains the finer-grained chat replay window. Runtime state that must survive a
server restart must likewise have a persisted representation; an in-memory
registry alone cannot satisfy that requirement.

### Connection lifecycle and delivery

- A connection subscribes only to the topics, messages, flows, processes, or
  auth sessions it is authorized to observe.
- Reconnect uses the last acknowledged event cursor. The server replays the
  retained range or sends a current snapshot when that range is unavailable.
- WebSocket ordering applies only to one live connection. Application-level
  IDs, acknowledgements, idempotency, replay, and snapshot reconciliation
  handle disconnects and retries.
- Per-connection outbound queues are bounded. Coalescible state updates replace
  older pending values; non-coalescible overflow closes the connection with an
  explicit resumable error instead of consuming unbounded memory.
- Heartbeats detect dead peers. Clients reconnect with jittered exponential
  backoff and do not interpret a disconnect as cancellation.
- Agent runs and other durable operations outlive sockets. Cancellation occurs
  only through an authorized, idempotent cancellation command.
- Multiple clients may subscribe concurrently and converge on the same
  persisted state.

### Bounded catch-up and snapshot rollover

A reconnecting client sends its last applied global cursor. A scope newly added
after that cursor receives a current snapshot rather than replaying events the
client was not previously authorized and subscribed to observe.
The server chooses between two catch-up modes:

1. **Replay** when the missing range is retained, contiguous, understood by
   the client, and small enough to send efficiently. The server sends the
   missing events in order and then switches to live delivery.
2. **Snapshot rollover** when replay would be too large, old, incomplete, or
   incompatible. The server skips intermediate changes, sends authoritative
   current state at a declared watermark, and then sends events after that
   watermark.

Rollover is selected when any configured limit is exceeded, including event
count, serialized byte size, event age/retention, or protocol compatibility.
It is also required when the retained range has a gap or depended on ephemeral
state lost during restart. Exact limits are operational configuration; the
protocol guarantee is that clients do not have to consume an unbounded replay.

The server must establish a consistent snapshot boundary:

1. Capture authoritative state as of event cursor `N`.
2. Buffer events created after `N` for that connection.
3. Send the snapshot marked with `event_id: N`.
4. Send buffered events beginning at `N + 1`.
5. Switch to ordinary live delivery.

The snapshot and cursor must be transactionally consistent, or created with an
equivalent retry/check mechanism. Sending an unwatermarked "latest" snapshot
is invalid because an event occurring during the read could be lost between
the snapshot and live stream.

State-like updates such as queue position, process status, message status,
flow progress, presence, and quota indicators may discard their intermediate
values during rollover. A snapshot supplies their latest authoritative value.

Streaming text is different: `chat.text` frames are deltas, so a snapshot of a
pending response includes the complete accumulated content plus its last
applied per-run sequence. The client replaces its local pending content with
that value and applies only later deltas. Sending only the newest text fragment
would corrupt the response. Durable tool results required to render the
conversation are included; transient tool progress is collapsed to current
tool state.

Snapshot installation is atomic at the subscription scope. It replaces the
client's corresponding cached state and advances its cursor to the snapshot
watermark before later events are applied. Duplicate events at or below the
installed cursor are ignored.

Snapshots contain only the bounded working set needed to render the current
screen: the latest 20 messages, every pending operation even when older than
that window, current queue/process/message state, and relevant metadata. Older
conversation pages and expanded historical tool details remain pull-based HTTP
resources. Scrolling upward fetches those pages; the WebSocket keeps the active
window synchronized.

### HTTP boundary

HTTP remains the preferred transport for:

- initial HTML, JavaScript, styles, and bootstrap data;
- account login, magic links, recovery, and WebSocket connection-ticket issue;
- health checks and bounded history, search, stats, and configuration queries;
- file browsing, upload, download, diffs, and other large payloads.

Historical pagination is deliberately independent of the WebSocket event
cursor. An HTTP page provides records at its documented database boundary,
while live events reconcile records in the active client window by stable
domain ID.

History could technically be requested over WebSocket, but it is a bounded
query rather than a live subscription. HTTP gives each page an independent
request lifecycle, ordinary cancellation/retry/cache behavior, direct status
codes, and no head-of-line competition with chat deltas on the socket. Keeping
history on HTTP also avoids inventing request/response multiplexing and page
backpressure inside the real-time protocol. This is a design boundary, not a
WebSocket limitation.

The browser authenticates a direct local/Tailscale WebSocket consistently with
the local security model. Shore browser connections use a secure session or a
short-lived, single-use connection ticket. Long-lived credentials must not be
placed in a WebSocket URL.

For local and Tailscale phase one, the browser persists a random `client_id` and
sends it during subscription. Mutation idempotency is keyed by the authenticated
session plus that client ID; a reverse-proxy source IP is never an identity.
Shore replaces the local session component with its authenticated device or
session identity. The client ID is an idempotency namespace, not authorization.

### Migration sequence

1. Specify and test envelopes, schemas, authorization, ordering, replay,
   idempotency, backpressure, and version negotiation.
2. Extract a transport-neutral event publisher from the current SSE generators.
3. Add `/ws/v1` while retaining all current HTTP/SSE behavior.
4. Migrate process/queue/message state and running-message reattachment first.
5. Migrate new chat submission and streaming.
6. Migrate Flow and CLI-auth PTY interaction in later phases.
7. Implement ADR-0039 over the proven protocol, including its device pairing,
   signed encrypted envelopes, capabilities, and audit requirements.
8. Retire superseded SSE endpoints only after compatibility fallback is no
   longer needed and parity tests pass.

During migration the UI supports `auto`, `websocket`, and `sse` transport
modes. `auto` prefers WebSocket and falls back to SSE. Both paths invoke the
same application operations and consume the same persisted events.

The server supports the current and immediately previous protocol versions.
SSE remains the compatibility fallback during migration; removal requires a
separate compatibility decision after the supported browser population has
moved to WebSocket.

### Relationship to Tailscale and Shore

Tailscale Serve and Shore may operate simultaneously. Tailscale clients reach
the local `/ws/v1` endpoint through the tailnet; Shore clients use the broker.
The application message semantics are identical, while the security wrappers
differ by path.

ADR-0039 is an architectural consumer of this protocol. This ADR should be
implemented before the Shore broker so the broker does not become the place
where Squid's application behavior is first defined. This does not require
converting every HTTP endpoint before Shore work begins.

ADR-0035's SSE-plus-POST choice remains valid until the CLI-auth message family
is migrated. At that point this ADR supersedes only ADR-0035's transport
choice, not its scoped PTY lifecycle or security decisions.

## Consequences

- Good: direct, Tailscale, and Shore access share one real-time application
  protocol.
- Good: chat-like streaming and multi-device state updates no longer require
  polling.
- Good: persisted replay and snapshots make reconnect behavior explicit rather
  than transport-specific.
- Good: retaining HTTP for bounded and bulk operations keeps the architecture
  simpler than a WebSocket-only API.
- Bad: the server and UI must temporarily support two streaming transports.
- Bad: idempotency, replay windows, snapshots, backpressure, and version skew
  become first-class implementation and testing obligations.
- Bad: existing SSE-heavy browser tests require protocol-level equivalents
  before SSE can be retired.

## Required verification

Before WebSocket is the default, automated tests must cover disconnects before
and after chat metadata, reconnect replay, replay-window expiry and snapshot
fallback, count/byte/age threshold rollover, an update racing snapshot
creation, full accumulated pending text followed by later deltas, duplicate
mutation requests, slow consumers, cancellation races, server restart during a
run, multiple simultaneous clients, terminal-event deduplication, historical
pagination alongside live updates, and client/server protocol-version skew.
Tests must also cover commit-before-notify ordering, a publish racing the wait
transition, worker-thread publication, retained-cursor rollover, safety-poll
recovery, and notifier shutdown.
