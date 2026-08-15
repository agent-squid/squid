---
status: proposed
date: 2026-08-12
updated: 2026-08-15
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
- The chat transcript is one multi-participant collection keyed by globally
  unique `msg_id`. Topic, agent, adhoc, and flow route are message metadata and
  authorization attributes, not client transcript identities.
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

### Implementation status (2026-08-15)

The ADR is partially implemented. WebSocket is now the browser's primary chat
transport in `auto` and `websocket` modes: it submits and cancels turns, streams
their lifecycle events, and reattaches pending assistant messages after page
load or reconnect. The HTTP/SSE path remains available as a compatibility
fallback and through the explicit `sse` migration mode.

| Area | Status | Current behavior |
| --- | --- | --- |
| `/ws/v1`, v1 envelopes, `hello`, version rejection | Implemented | The server exposes one JSON WebSocket endpoint and accepts protocol version 1. |
| Subscribe, unsubscribe, scoped replay, snapshot fallback | Implemented for direct local access | Topic/agent and global lifecycle scopes are explicitly authorized, and malformed or unsupported scopes fail with `unauthorized_scope`. Cursor replay, bounded rollover, retained-cursor rollover, and 20-message snapshots exist. Snapshot state and its watermark share one database read transaction, followed by an immediate durable-log drain of events committed after the watermark. Shore authorization remains part of ADR-0039. |
| Durable global event log and wake-up | Implemented | `realtime_events` supplies global cursors; commits wake sockets through a process-global generation notifier with a 20-second safety poll. Seven-day/100,000-event cleanup exists. |
| Chat event publication | Implemented on the server | Persisted run events publish `chat.queued`, `chat.loading`, `chat.processing`, `chat.status`, `chat.tool`, `chat.text`, `chat.stats`, `chat.done`, and `chat.error`; message mutations publish `message.changed`. |
| Running-message browser reattachment | Implemented | The UI prefers WebSocket snapshots/events for pending messages, reconnects with jittered exponential backoff, persists its cursor, sends acknowledgements, and falls back to SSE when WebSocket is unavailable. |
| New chat submission and cancellation | Implemented | The browser uses idempotent `chat.start` and `chat.cancel` commands in WebSocket mode. Auto mode falls back to the HTTP/SSE compatibility path only when a command was not submitted; SSE mode retains the compatibility path. |
| Process and queue state | Implemented | Snapshots and authoritative `process.changed`/`queue.changed` events update the browser status model; HTTP refresh remains only as pre-snapshot and SSE compatibility recovery. |
| Flow | Durable executor and recovery implemented; realtime migration pending | New Flow submissions persist a canonical `flow_runs`/`flow_steps` DAG in `durable` mode. Client-owned origins are claimed and linked only by their corresponding submitted turns; the scheduler never synthesizes or redispatches an origin. Successful, failed, and cancelled worker outcomes drive persisted step/run transitions, and newly eligible continuation steps use dependency-gated atomic claims rather than transcript inference. Delayed continuations use materialized `due_at` values. Startup recovery reconciles unprepared continuation claims, persisted terminal output, interrupted running turns, and due work; a periodic safety scan handles due work and stale claims and restores delayed wakeups. Ambiguous work, including an abandoned origin claim, is made terminal rather than automatically redispatched. Pre-cutover shadow and legacy runs remain with the transcript executor. `flow.step.created` is accepted by the replay type registry but is not yet published or consumed, and the browser still polls for new Flow steps. |
| CLI authentication | Not implemented | `auth.*` messages are not implemented; ADR-0035's SSE-plus-HTTP transport remains in use. |
| Backpressure and frame limits | Not implemented | Sends are direct and there is no bounded/coalescing outbound queue, `slow_consumer` handling, or configured inbound frame-size enforcement. |
| Heartbeat and acknowledgements | Partial | `ping` receives `pong` and the UI sends `ack`, but the server does not initiate heartbeat pings or use acknowledged cursors to manage delivery. |
| Protocol compatibility | Partial | Unsupported versions fail explicitly, but only v1 is supported rather than the current and immediately previous versions. |
| Shore relay | Not implemented | ADR-0039's broker transport, pairing, encryption, capabilities, and audit work remain future work. |

### Remaining implementation sequence

The Flow executor now owns newly submitted runs through ADR-0042's durable
store. It claims and links origins, advances persisted lifecycle state from
worker outcomes, claims eligible continuations, and uses persisted dependency
message links to prepare handoffs. Startup and periodic recovery reconcile
claimed/running state, dispatch due work, and restore delayed wakeups. Existing
shadow and transcript-only runs remain on the legacy executor and are never
promoted.

Complete the Flow milestone in this order:

1. Publish `flow.step.created` in the same database transaction as the step
   state/message linkage, and include authoritative Flow state in scoped
   snapshots. Notification remains strictly after commit.
2. Consume `flow.step.created` through the browser's existing stable-message
   reconciliation path. Stop the 1.5-second Flow-step poll while WebSocket is
   active; retain it only for `sse` mode and `auto` fallback until SSE
   retirement.
3. Close the milestone with server tests for restart, cancellation, stale
   claims, event-after-commit, and duplicate-dispatch races, plus browser tests
   for live delivery, reconnect/replay without duplicate steps, snapshot
   rollover, and SSE/WebSocket rendering parity.

After Flow, the remaining ADR-0040 work is, in order:

1. Migrate ADR-0035's CLI-auth PTY interaction to the `auth.*` family while
   retaining its scoped session and security model.
2. Add bounded outbound queues, coalescing, `slow_consumer` closure, inbound
   frame-size enforcement, and server-initiated heartbeat handling.
3. Use acknowledgements for delivery bookkeeping and support the current and
   immediately previous protocol versions.
4. Close the remaining required-verification gaps, then make a separate
   compatibility decision before removing SSE.
5. Implement ADR-0039's Shore relay over the proven protocol.

SSE endpoints therefore remain required for migration fallback, CLI
authentication, and the live families not yet moved to WebSocket. WebSocket is
currently primary for browser chat commands and lifecycle updates, but not yet
for all live commands and state updates described by this decision.

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

- An authorized Squid UI receives the lightweight global chat lifecycle needed
  to keep its active transcript window synchronized. Composer route and history
  filter never change that delivery coverage.
- High-volume text and tool deltas may use per-`msg_id` watches. Flow, process,
  and auth resources may retain explicit resource subscriptions where their
  authorization or lifecycle requires them.
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

A reconnecting client sends its last applied global cursor. If its authorization
expands after that cursor, newly authorized state arrives in a current snapshot
rather than being reconstructed from events the client was not authorized to
observe. The server chooses between two catch-up modes:

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

Snapshot installation is atomic for the authorized active window. It reconciles
that window by stable domain ID and advances the cursor to the snapshot
watermark before later events are applied. It must not replace unrelated HTTP
history pages or reset the visible transcript. Duplicate events at or below the
installed cursor are ignored.

Snapshots contain only the bounded working set needed to render the current
screen: the latest 20 messages, every pending operation even when older than
that window, current queue/process/message state, and relevant metadata. Older
conversation pages and expanded historical tool details remain pull-based HTTP
resources. Scrolling upward fetches those pages; the WebSocket keeps the active
window synchronized.

### Client state and transcript reconciliation

SSE, WebSocket events, snapshots, and HTTP history pages feed one normalized
turn model. Transport code must not maintain a separate visual representation
or directly reset transcript navigation. The client maintains application
state independently from DOM state. The concrete client-side store and
idempotent reconciler that implement these invariants as one architecture,
rather than per-call-site discipline, are specified in
[ADR-0041](0041-normalized-client-transcript-store-and-reconciler.md).

- messages are keyed by globally unique `msg_id`;
- the user prompt and assistant response form one turn group through
  `reply_to`;
- one pending `msg_id` has at most one registered live group and one optional
  high-volume message watch;
- `event_id` orders transport application, while `run_seq` deduplicates one
  message's streamed deltas; neither determines transcript display order; and
- completed turns are ordered by authoritative `completed_at`, with `msg_id`
  as the deterministic tiebreaker.

A terminal transition is one idempotent application operation. It finalizes
content, tools, stats, and completion time; unregisters the per-message watch;
removes the live registration; and installs exactly one completed turn at its
completion-order position. Duplicate snapshots or terminal events do nothing
after that transition has been applied. The user prompt remains part of the
turn throughout; it is not independently hidden merely because the assistant
response is pending.

Realtime reconciliation preserves composer route, explicit history filter,
search state, pagination boundaries, and scroll anchor. Messages outside the
active window may update the client cache but do not force pagination or broad
DOM replacement. The destructive history reset is reserved for explicit
navigation such as changing a history filter, leaving search, or jumping back
to the latest window; realtime handlers must never call it.

Development builds should enforce these invariants where practical:

- no more than one live group or completed turn exists for a `msg_id`;
- terminal messages have no live registration or per-message watch;
- composer-route changes do not mutate history-filter or delivery state; and
- applying a realtime event never invokes the destructive history reset.

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

### Migration control

The completed migration phases established the v1 envelope and durable event
publisher, added `/ws/v1` alongside HTTP/SSE, and moved browser chat,
reattachment, process, queue, and message state to the new transport. Current
status and the single authoritative order for unfinished phases are maintained
in [Remaining implementation sequence](#remaining-implementation-sequence).

During migration the operator can select the browser transport in
`~/.squid/squid.yaml`:

```yaml
realtime:
  transport: auto  # auto | websocket | sse
```

The server validates this setting and includes the effective mode in the UI
bootstrap configuration. `auto` is the default when the section or key is
absent; it prefers WebSocket and falls back to SSE. `websocket` forces the
WebSocket path and surfaces connection or protocol failures rather than
silently switching transports. `sse` disables browser WebSocket use and keeps
all live operations on the compatibility HTTP/SSE paths. A configuration
change takes effect after the server restart required for YAML changes.

This is an application-wide migration control, not a per-tab preference, so
local storage and query-string overrides must not supersede it. The setting may
be removed in the separate compatibility decision that retires SSE. Both paths
invoke the same normalized reconciliation operations and produce the same turn
lifecycle and final rendering.

Transport selection does not change the conversation schema, transcript
ordering, or indexes. Domain writes to `chat_messages` and `run_events`, and
publication to `realtime_events`, remain transport-neutral even in `sse` mode;
continuing to publish the realtime log permits comparison, replay testing, and
switching modes without a data migration. `realtime_events.event_id` orders
delivery through the global realtime cursor, while `run_events.seq`/`run_seq`
orders and deduplicates deltas within one message. Neither controls transcript
display order, which remains `completed_at` with `msg_id` as its deterministic
tiebreaker. The existing `(topic, agent, event_id)` realtime index is therefore
independent of the selected transport and no transport-specific index is
introduced.

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

Browser parity tests must additionally prove that SSE and WebSocket produce the
same normalized turn and final DOM; a pending `msg_id` has one live group and
none after completion; duplicate or reordered terminal events create no extra
turn; final turns are placed by `completed_at` plus `msg_id`; changing the
composer route does not change the history filter or lifecycle feed; explicit
filters, pagination, and scroll anchors survive realtime events; and snapshots
reconcile the active window without emptying or replacing the transcript.
