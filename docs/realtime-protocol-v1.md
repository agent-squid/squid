# Squid real-time protocol v1

This document specifies the version 1 application protocol selected by
ADR-0040. JSON messages are carried by `/ws/v1`; SSE remains a migration
fallback. Phase one covers chat, Flow-step discovery, process, queue, and
message state, and CLI authentication over the socket.

## Ordering and cursor model

Each replayable server event is inserted into `realtime_events` and receives a
database-generated integer `event_id`. It is global to one Squid database and
totally orders publication commits. It does not claim that parallel work ran in
that order.

A connection has one applied cursor: the greatest delivered `event_id` whose
relevant effects it has installed. The ordinary Squid UI receives lightweight
chat lifecycle events as one authorized global transcript rather than changing
topic/agent subscriptions with the composer route. Event IDs can still jump
when intervening events are unauthorized or belong to unwatched high-volume
resources. On resume, the server never reveals skipped payloads.

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
- `scope`: stable authorization metadata and, for explicitly watched resources,
  subscription identifiers. Chat topic/agent values do not define separate UI
  transcripts.
- `payload`: type-specific object.

Unknown optional fields are ignored. Unknown types produce `error` with code
`unsupported_type`. Frames above the configured byte limit close the connection
with `frame_too_large` and close code 1009. A binary WebSocket frame (rather
than UTF-8 JSON text) is treated as `invalid_frame`; the connection stays open.

## Connection lifecycle

1. Server sends `hello` with `supported_versions`, limits, heartbeat interval,
   and current server cursor.
2. Client sends `subscribe` with version, its requested global lifecycle feed,
   any explicit resource watches, an optional last applied cursor, and a
   persisted random `client_id` of 16-128 URL-safe characters.
3. Server authorizes the request and responds with `subscribed`, then either
   replays a bounded contiguous range or sends `snapshot` at watermark `N`.
4. Client atomically installs events and periodically sends `ack` containing
   its greatest applied delivered `event_id`. Acknowledgements are advisory
   bookkeeping on the server (recorded for observability and clamped to the
   current cursor); resumption is driven by the cursor the client sends in
   `subscribe`, not by the server's ack history.
5. `ping`/`pong` detects dead peers. The server initiates a `ping` every
   `heartbeat_seconds` and closes the connection (code 1001) if no inbound
   frame of any type arrives within two intervals. Any inbound frame — ack,
   pong, or command — counts as liveness. Reconnect does not cancel work.

Changing the composer route or history filter does not change subscriptions.
If authorization expands, or an explicitly watched resource is newly added, it
receives a snapshot at the current watermark; a cursor accumulated without
access to that state cannot reconstruct it.

After a publication transaction commits, the single Squid server process wakes
connections through a process-global generation counter. Connections query the
durable log after waking; notifications carry no event payload and may be lost
without affecting correctness. A 20-second safety poll uses the same drain path.
Reconnect and restart recovery always use the durable cursor.

## Commands and results

Phase-one client mutations are `chat.start`, `chat.cancel`, and `auth.start`.
The server persists the `(principal, request_id)` result before acknowledging
it. A retry returns the same `command.result`; it does not execute again.
Conflicting reuse of a request ID produces `request_id_conflict`. `auth.cancel`
is not in this set — see "CLI authentication" below, where it is grouped with
`auth.input`/`auth.resize` as fire-and-forget instead.

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

`process.changed` carries `payload.processes`, the authoritative current process
list. `queue.changed` carries `payload.queue`, the authoritative current queued
item list. Both replace the corresponding snapshot collection rather than
being applied as deltas.

`flow.step.created` is committed with a durable Flow step's message linkage.
Its payload carries `flow_run_id`, `step_id`, `user_msg_id`,
`assistant_msg_id`, `route`, and the linked step's current `status`.

## CLI authentication

CLI authentication (`auth.*`) moves the ADR-0035 PTY login/install/model
sessions onto `/ws/v1`, mirroring `chat.*`. The PTY and security model is
unchanged; SSE remains the migration fallback. Messages:

- `auth.start` — idempotent mutation via `request_id`. Payload
  `{harness, mode, model?, provider?, cols, rows}`. Result
  `{ok, session_id, harness, command}` or `{ok:false, error, detail}`.
  `mode` is one of `login`, `install`, `pull`, `remove`, or `unlock`.
  Supply `provider` for a dedicated Codex or Claude login. The server validates the
  provider and resolves its credential store. Omitting it retains host login.
  See [ADR-0058](decisions/0058-persistent-credential-profiles.md) and
  [ADR-0059](decisions/0059-dedicated-claude-subscription-providers.md).
  On macOS, a paired browser can explicitly request `mode: "unlock"` with
  `harness: "keychain"` over HTTP or WebSocket. Squid runs the fixed
  `security unlock-keychain` command. The initiating browser session owns
  the prompt, input, and output replay. Older Keychain unlock settings in
  `squid.yaml` are ignored. See [ADR-0016](decisions/0016-security-model.md).

- `auth.input` — fire-and-forget, NOT persisted. Payload `{session_id, data}`;
  `data` is the raw UTF-8 input string (as the HTTP route accepts it). Result
  `{ok:true}` or `{ok:false, error:"unknown_session"}`.
- `auth.resize` — fire-and-forget, NOT persisted. Payload `{session_id, cols,
  rows}`. Result `{ok:true}` or `{ok:false, error:"unknown_session"}`.
- `auth.cancel` — fire-and-forget, NOT persisted (not an idempotent mutation,
  despite `request_id` still being required on the envelope like every other
  command). Payload `{session_id}`. Result `{ok, cancelled, session_id}`.
  Calling it more than once for the same session is already safe without an
  idempotency guard: a session already gone by the second call just yields
  `cancelled: false`.
- `auth.output` (server→client) — `{v:1, type:"auth.output",
  payload:{session_id, data}}`; `data` is a base64-encoded PTY chunk.
  **Transient — not stored in `realtime_events` and not replayable.**
- `auth.done` (server→client) — `{v:1, type:"auth.done",
  payload:{session_id, returncode}}`. **Transient.** `returncode` is always
  numeric — the process's real exit code on a natural exit, or `-1` when the
  session was already reaped (idle timeout / server-side cancel), so a reaped
  login is never reported as success.

`auth.start` registers the calling socket as the session's output listener:
`auth.output` / `auth.done` frames are pushed directly to that socket, so no
`subscribe` scope covers them and they never enter the durable log. A reconnect
that resends `auth.start` with the same `request_id` re-attaches the socket to
the still-live session and replays its ring buffer. **Known limitation:** the
server supports that re-attach, but the current client sends `auth.start` once
and issues `auth.input`/`auth.resize`/`auth.cancel` fire-and-forget — it does
not resend on reconnect, so a socket dropped mid-login does not resume output.
Acceptable for short login flows, but the re-attach path is not currently
reachable from the UI.

## Snapshot

A subscription snapshot contains:

- the latest 20 authorized messages in the global transcript active window;
- all authorized pending operations, even if older than those 20 messages;
- current process, queue, and message state;
- complete accumulated content and `run_seq` for pending assistant messages;
- durable tool results needed to render that active window.
- authoritative run and step state for active durable Flows and for Flows
  represented in the active message window; scoped subscriptions receive only
  their authorized steps.

The server reads state consistently at watermark `N`, buffers later matching
events, sends the snapshot with `event_id: N`, then sends events after `N`.
Installation reconciles the authorized active-window cache atomically by
stable domain ID. It does not replace unrelated HTTP history pages or reset the
visible transcript. Events at or below the installed cursor and text deltas at
or below installed `run_seq` are ignored.

A snapshot includes `payload.cursor_reset: true` when it establishes a fresh
cursor or recovers from a client cursor ahead of the server (for example after
a database restore). The client must accept that snapshot even when its
`event_id` is lower than the locally persisted cursor, replace the cursor with
the snapshot watermark, and resume from that watermark.

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

The accepted WS-only target in ADR-0040 supersedes the original HTTP-only
history design. Explicit paging/navigation still use HTTP during migration.
Automatic recovery never rebuilds history over HTTP.

`history.check` is a local read query using `request_id` / `command.result`
correlation. Its payload is `{ "ids": [positive integer, ...] }`, capped at 100
IDs. Optional `bookmarked` and `marked_bad` boolean filters restrict the result
to current annotation membership (both filters apply when both are true).
It requires a subscribed local identity and returns `{ "ok": true,
"items": [...] }` with authoritative stored rows for exactly the requested IDs;
missing IDs indicate deletion or failure to match the requested annotation
filters. Invalid IDs, missing identity, and replies over
the frame byte limit return `ok: false` with `invalid_ids`,
`client_identity_required`, or `history_result_too_large`. Clients split large
batches. For an exact single ID, clients can request `chunked: true`. An oversized
reply then arrives as correlated `history.chunk` frames with zero-based `index`
and a `text` fragment of the serialized JSON result. A final `command.result`
with `ok: true` and `chunk_count` marks completion. Clients assemble the result
only after receiving all chunks in order. Each frame fits the same byte limit.
Exact checks share the local history query admission and read timeout with
paging. This read neither advances the durable event cursor nor uses mutation
idempotency records.

The UI verifies rendered completed rows after subscription and health recovery,
updates only changed rows, and captures position after replies arrive. Pending
streams retain their transport owners. SSE turn streaming also subscribes to
the WS global lifecycle feed for discovery. Shore recognizes `history.check`
as a protocol type but its existing `dashboard.read.v1` grant does **not**
authorize it; no remote capability is widened by this query.

## Connection health

Local `health.check` is a correlated read with an empty payload and a required
subscribed client identity. `command.result` returns `ok`, `status: "ok"`,
`boot_time`, and `version`; missing identity returns `client_identity_required`.
This is a lightweight application-server status reply, not the full provider,
harness or settings metadata returned by the legacy HTTP health endpoint.
It creates no durable event or mutation/idempotency record.

The browser connection probe has no HTTP fallback or periodic HTTP health timer.
It queries on subscription and explicit recovery/navigation, coalesces concurrent
requests on the same socket, and ignores obsolete replies after reconnect.
Server heartbeats are answered with `pong`; missing traffic for two advertised
heartbeat intervals plus one second triggers reconnect (suspended pages resume
through foreground recovery). Socket liveness does not imply history recovery
or application-health query success. Connection-state messages preserve the
reading position and never rebuild history. History verification remains a
separate callback on subscription/heartbeat.

Shore recognizes the query but does not grant it through `dashboard.read.v1`.
Existing authorized ping/pong behavior and remote capability grants are unchanged.
Other HTTP health metadata consumers remain migration debt under ADR-0040.

## Compatibility and errors

Only protocol version 1 is defined; the supported-version set is centralized so
supporting an immediately previous version later is an additive change.
Negotiation failure returns `unsupported_version` with the configured supported
set before continuing. Stable error codes are `unsupported_version`,
`invalid_client_id`, `unauthorized_scope`, `client_identity_required`,
`invalid_frame`, `unsupported_type`, `frame_too_large`, `request_id_conflict`,
and `slow_consumer`. `replay_gap` and the other rollover reasons are not error
frames — they trigger a snapshot fallback. Errors never expose events from
unauthorized scopes.

Outbound queues and frames are bounded. Coalescible state (`process.changed`,
`queue.changed`) replaces older queued state of the same type. Overflow
involving a non-coalescible event sends a best-effort `slow_consumer` error
(`resumable: true`) and closes with code 1013, after which the client resumes
from the cursor it sends in `subscribe`. An oversized inbound frame closes with
`frame_too_large` and code 1009. A heartbeat timeout closes with code 1001.

### Bounded history query preparation

Local `history.page` and `history.around` queries return correlated
`command.result` payloads with `ok` and the same history fields as the existing
history handlers. They require an established client identity. Paging uses
`offset`/`limit` or completion-time cursors, independently of replay event IDs.
Page limits are 1–100; around-window `before`/`after` counts are 0–100. Unknown
fields and invalid types fail with `invalid_history_query`. Oversized results
fail with `history_result_too_large`; there is no truncation or HTTP fallback
inside the query handler. Queries are read-only and may be retried.

These handlers prepare the browser migration; the browser still uses HTTP
history, and oversized-result transfers remain unimplemented. Shore recognizes
these types but its existing capability grants do not authorize them. Browser
rollout must include stale-result handling, bounded-transfer support and remote
capability parity before retiring HTTP callers.

History page/around request IDs must be nonempty strings of at most 128
UTF-8-encodable characters; invalid IDs receive `error` / `invalid_request_id`
without echoing the invalid value. The complete outbound JSON envelope is checked against the
frame limit, including the ID and UTF-8 encoding.

Each socket admits at most two concurrent history page/around reads. Additional
queries receive correlated `history_query_busy` results. Reads run separately
from the receive loop so health, heartbeat, and control frames remain responsive.
A read taking longer than five seconds receives `history_query_timeout`; an
unexpected read failure receives `history_query_failed`. Timed-out reads retain
their admission slot until the underlying operation finishes, and their late
results are discarded. Disconnect cancels connection-owned query tasks; an
already-running database thread may finish after disconnect. These limits do
not provide transfer chunking or query cancellation commands.

The socket receive and authentication-output queue reader remain alive across
history-query completion wakeups. Only consumed readers are replaced; changing
authentication sessions cancels the old session's reader. This prevents a frame
or output chunk arriving after the wait snapshot from being discarded during
pending-task cleanup.
