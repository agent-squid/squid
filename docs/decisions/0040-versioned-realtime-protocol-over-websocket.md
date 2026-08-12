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
- Every replayable server event has a monotonically increasing `event_id`
  within its documented stream scope.
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

### Event production and persistence

Execution must be separated from transport:

```text
runner/dispatcher -> persist event -> publish event -> SSE and WebSocket
```

The runner, topic dispatcher, and authentication session publish domain events
without writing to a socket. A slow or disconnected client therefore cannot
stall or cancel an agent process.

The existing `run_events` log remains the durable source for chat replay and
is generalized only where durable replay is required. Ephemeral signals may be
coalesced. Durable state is always recoverable through a snapshot; in-memory
subscriptions and socket queues are never sources of truth.

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

### HTTP boundary

HTTP remains the preferred transport for:

- initial HTML, JavaScript, styles, and bootstrap data;
- account login, magic links, recovery, and WebSocket connection-ticket issue;
- health checks and bounded history, search, stats, and configuration queries;
- file browsing, upload, download, diffs, and other large payloads.

The browser authenticates a direct local/Tailscale WebSocket consistently with
the local security model. Shore browser connections use a secure session or a
short-lived, single-use connection ticket. Long-lived credentials must not be
placed in a WebSocket URL.

### Migration sequence

1. Specify and test envelopes, schemas, authorization, ordering, replay,
   idempotency, backpressure, and version negotiation.
2. Extract a transport-neutral event publisher from the current SSE generators.
3. Add `/ws/v1` while retaining all current HTTP/SSE behavior.
4. Migrate flow-step discovery, process/queue state, and running-message
   reattachment first.
5. Migrate new chat submission and streaming, then CLI-auth PTY interaction.
6. Implement ADR-0039 over the proven protocol, including its device pairing,
   signed encrypted envelopes, capabilities, and audit requirements.
7. Retire superseded SSE endpoints only after compatibility fallback is no
   longer needed and parity tests pass.

During migration the UI supports `auto`, `websocket`, and `sse` transport
modes. `auto` prefers WebSocket and falls back to SSE. Both paths invoke the
same application operations and consume the same persisted events.

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
fallback, duplicate mutation requests, slow consumers, cancellation races,
server restart during a run, multiple simultaneous clients, terminal-event
deduplication, and client/server protocol-version skew.
