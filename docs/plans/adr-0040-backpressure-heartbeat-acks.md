# Plan: ADR-0040 remaining steps 1–2 — backpressure, heartbeat, acks, version machinery

Execute the steps in order. Each step has objective, files, actions, and an
acceptance check. Steps 1–2 and 4 are server-only; step 3 is also effectively
server-only (see the "Client already conforms" note below). All are verifiable
by the tests in step 5. Line numbers reference the current tree and drift as
you edit — anchor on function names, not line numbers.

**Key anchors (verified against current tree):**
- `realtime_v1` — `agent/server.py:4054` (the `/ws/v1` handler).
- Auth handlers: `_handle_auth_input` (3943), `_handle_auth_resize` (3965),
  `_handle_auth_cancel` (3986), `_handle_realtime_mutation` (4012).
- The receive is **wrapped in a task**: `receive_task =
  asyncio.create_task(websocket.receive_json())` (4078); the frame is read at
  `frame = receive_task.result()` (4091). Frame parsing/size logic in step 2
  therefore lives at the *result* site, not the create-task site.
- `hello` sent directly at 4062 (`heartbeat_seconds` hardcoded to 20 at 4068).
- Inbound `v` check at 4122; silent `ack`/`pong` accept via the else-guard at
  4184 (`elif message_type not in {"ack", "pong"}`).
- `_authorize_realtime_scopes` (3767), `_realtime_envelope` (3711),
  `_REALTIME_REPLAY_LIMIT` (3636), `_RealtimeNotifier` (3643).

**Client already conforms — do NOT rebuild it.** The realtime client in
`ui/app.js` (the `connect()`/`onmessage` closure, ~7247–7416) *already*:
answers `ping` with `pong` (`7389`, placed after the `event_id` branch so it
is not swallowed — a `ping` frame carries no `event_id`); sends `ack`
`{event_id: cursor}` on every apply (`markApplied`, 7295); resumes from the
persisted `squid-realtime-v1-cursor` on reconnect (`subscribe` sends `cursor`,
7286); and reconnects with jittered backoff on **every** close code — its
`onclose` (7391) does not read or special-case any code, so a `1013`
`slow_consumer` close already resumes correctly. Server error frames without a
`request_id` (`slow_consumer`, `frame_too_large`, `unsupported_version`) fall
through the `onmessage` chain harmlessly and the ensuing close drives the
reconnect. **This work therefore needs no client edit** (see step 3).

Scope: ADR-0040 "Remaining implementation sequence" items 1 and 2
(`docs/decisions/0040-versioned-realtime-protocol-over-websocket.md:139-147`).
Item 3 (verification-gap audit) is covered by step 6. Item 4 (ADR-0039 Shore)
is out of scope.

## Design decisions (already settled — do not revisit)

- **Sender-task architecture.** All outbound frames go through one
  per-connection bounded `asyncio.Queue` drained by a dedicated sender task.
  The receive loop never awaits `websocket.send_json` directly. This is what
  makes inbound `chat.cancel`/`ping`/`ack` responsive while a slow client
  backlogs outbound traffic, and is the only way to bound memory.
- **Coalescing set = state-replacement types only.** `process.changed` and
  `queue.changed` are authoritative full-state replacements (protocol doc:
  "Both replace the corresponding snapshot collection"). When enqueueing one,
  drop any still-queued frame of the same type. Everything else
  (`chat.*`, `flow.step.created`, `message.changed`, `snapshot`, `subscribed`,
  `command.result`, `auth.*`, `pong`, `error`, `hello`) is non-coalescible.
  `message.changed` is NOT coalesced: the client's `msg_id`-keyed discovery
  dedup (`discoverRealtimeTurn`'s DOM check plus the `realtimeDiscoveries`
  set) tolerates duplicates but status transitions feed
  `refreshComposerSessionCount`, and the protocol doc only authorizes
  discarding state-like updates during rollover, not live delivery. Keep the
  coalescing set minimal.
- **Overflow closes with `slow_consumer`.** When the queue is full and the
  incoming frame is non-coalescible: best-effort direct send of
  `{"v":1,"type":"error","payload":{"code":"slow_consumer","resumable":true}}`
  (bypassing the full queue, swallowing its own failure), then
  `websocket.close(code=1013)`. The client resumes from its last acked/applied
  cursor via the existing subscribe-cursor path — no new recovery machinery.
- **Acks are bookkeeping, not flow control.** The server records
  `last_acked_cursor` per connection for observability and for tests to
  assert resumption, and logs it on `slow_consumer` closes. Do NOT gate
  enqueues on ack progress — the bounded queue is the flow control; ADR-0040
  requires acks to "manage delivery," which the client-side persisted cursor
  (sent in `subscribe`) already provides on reconnect.
- **Heartbeat is application-level.** The protocol defines `ping`/`pong`
  envelope types; do not rely on WebSocket transport pings. Server sends
  `ping` every `heartbeat_seconds` (already advertised as 20 in `hello`),
  and closes the connection if no inbound frame of any type arrives within
  2 missed intervals (40s default). Any inbound frame (ack, pong, command)
  counts as liveness.
- **Version machinery without a v2.** Only protocol v1 exists. "Support
  current and immediately previous" lands as: a single
  `_REALTIME_SUPPORTED_VERSIONS` tuple driving `hello.supported_versions`,
  inbound `v` validation, and the `unsupported_version` error payload — so
  adding v2 later is a one-line additive change plus a handler fork. Do NOT
  invent a speculative v2.

## Invariants — do NOT touch

- The commit-before-notify path: `set_realtime_commit_listener`,
  `_RealtimeNotifier.notify_committed`, generation-counter `wait()`, and the
  20s `_REALTIME_SAFETY_POLL_SECONDS` safety poll (`agent/server.py:3643-3708`).
- Durable replay: `get_realtime_replay`, `_realtime_replay_rollover_reason`,
  snapshot watermark semantics, and `_REALTIME_V1_REPLAY_TYPES`
  (`agent/server.py:3710-3760`). The rollover reasons and limits are settled.
- `_authorize_realtime_scopes` and scope SQL. No new scopes.
- `agent/auth_sessions.py` entirely: PTY allowlist, idle reaper, ring buffer,
  listener registry (`_detach_auth_listener` contract stays).
- The SSE compatibility paths and `realtime.transport` config. SSE removal is
  a separate future decision.
- Envelope shape from `_realtime_envelope`. Adding optional fields is fine;
  renaming/removing is a protocol break.

## Step 1 — Per-connection outbound queue and sender task

**Objective:** replace every direct `send_json` in the `/ws/v1` path with a
bounded queue + sender task; close `slow_consumer` on non-coalescible
overflow.

**Files:** `agent/server.py`, `agent/config.py`.

**Actions:**

1. Add constants near `_REALTIME_REPLAY_LIMIT` (`server.py:3636`):
   - `_REALTIME_OUTBOUND_QUEUE_LIMIT = 256`
   - `_REALTIME_MAX_FRAME_BYTES = 256 * 1024` (inbound; chat prompts and
     base64 `auth.input` chunks must fit — confirm against any existing HTTP
     body cap for chat submission before lowering)
   - `_REALTIME_HEARTBEAT_SECONDS = 20` (matches the `hello` advertisement)
   - `_REALTIME_HEARTBEAT_MISS_LIMIT = 2`
   - `_REALTIME_COALESCIBLE_TYPES = frozenset({"process.changed", "queue.changed"})`
2. Config plumbing, mirroring `realtime_transport` (`agent/config.py:35-42`):
   optional `realtime.outbound_queue_limit`, `realtime.max_frame_bytes`,
   `realtime.heartbeat_seconds` (ints; validate positive), exported like
   `REALTIME_TRANSPORT`. Server constants above become the defaults.
3. Add a `_RealtimeOutbound` helper class near `_RealtimeNotifier`:
   - Holds `asyncio.Queue(maxsize=limit)`, a `deque`-backed scan for
     coalescing (or use the Queue's internal `._queue` — no; keep a plain
     `collections.deque` + `asyncio.Event` "not-empty" flag instead of
     `asyncio.Queue`, since coalescing needs in-place removal of pending
     entries and `maxsize` enforcement is then manual but trivial).
   - `enqueue(frame: dict) -> bool`: if `frame["type"]` in
     `_REALTIME_COALESCIBLE_TYPES`, drop queued frames of the same type
     first; if len == maxsize and frame is non-coalescible, return False;
     else append, set the event, return True.
   - `async get()` waits on the event and pops left.
4. In `realtime_v1` (`server.py:4054`):
   - Instantiate `_RealtimeOutbound` and a sender task
     `asyncio.create_task(_ws_sender(websocket, outbound))` right after
     `accept()` (4060). `_ws_sender` loops `await websocket.send_json(await
     outbound.get())`. The `hello` frame at 4062 may stay a direct send (it is
     the first frame, before any backlog can exist) or go through the queue —
     either is fine; do not overthink it.
   - Add the sender task to every `asyncio.wait` wait_set alongside
     `receive_task`/`notify_task`/`output_task`. If the sender task is in
     `done`, its exception (e.g. `WebSocketDisconnect` on send) must tear
     down the connection: re-raise/break into the existing
     `except WebSocketDisconnect` cleanup.
   - Replace ALL `await websocket.send_json(...)` in `realtime_v1`
     (4062–4199), `_handle_realtime_mutation` (4012), `_handle_auth_input`
     (3943), `_handle_auth_resize` (3965), `_handle_auth_cancel` (3986), and
     the auth-output/auth-done pump inside `realtime_v1` (4109–4120) with
     `outbound.enqueue(...)`; pass `outbound` into the handler functions as a
     new parameter. Note the handlers currently take `(websocket, frame, ...)`
     — add `outbound` and stop passing `websocket` for send purposes (they may
     still need it for nothing else; audit each).
   - On `enqueue` returning False: best-effort direct
     `await websocket.send_json({"v": 1, "type": "error", "payload":
     {"code": "slow_consumer", "resumable": True}})` inside try/except, then
     `await websocket.close(code=1013)`, log at WARNING with principal,
     `last_acked_cursor`, and queue depth, then `return` (existing finally/
     except path must still detach the auth listener).
   - Ensure the sender task is cancelled and awaited on every exit path,
     next to the existing `_detach_auth_listener` call (a `finally` block is
     the clean way; today cleanup lives only in `except WebSocketDisconnect` —
     restructure to `try/except/finally`).

**Acceptance:** existing realtime websocket tests pass unchanged
(`tests/test_realtime.py` — the TestClient/websocket tests must not observe
ordering changes: enqueue order is FIFO and the sender drains promptly, so
frame order is preserved).

## Step 2 — Inbound frame-size enforcement and malformed-frame handling

**Objective:** enforce `frame_too_large`; convert JSON decode failures into
`invalid_frame` instead of an unhandled exception.

**Files:** `agent/server.py`.

**Actions:**

The receive is task-wrapped: `receive_task =
asyncio.create_task(websocket.receive_json())` at 4078, read as
`frame = receive_task.result()` at 4091. Do the size/parse work at the *result*
site, not the create-task site.

1. Change the task to `websocket.receive_text()` (4078); it still raises
   `WebSocketDisconnect` on disconnect, caught by the outer `try` as today.
   At the result site (4091), let `frame_raw = receive_task.result()` (now a
   `str`). If `len(frame_raw.encode("utf-8")) > _REALTIME_MAX_FRAME_BYTES`:
   direct-send `{"v":1,"type":"error","payload":{"code":"frame_too_large"}}`
   (bypass the queue — the connection is about to die and the queue may be
   full), then `await websocket.close(code=1009)` and return.
2. Wrap `json.loads(frame_raw)`; on `ValueError` enqueue the existing
   `invalid_frame` error envelope and `continue`. Require the result to be a
   `dict`; non-dict JSON (arrays, scalars) also gets `invalid_frame`. The old
   flow set `frame = receive_task.result()` unconditionally — the new flow
   assigns `frame` only after a successful decode, so guard the downstream
   `if frame is not None:` block accordingly.
3. Binary frames: Starlette's `WebSocket.receive_text()` reads
   `message["text"]` and raises **`KeyError`** when a bytes frame arrives.
   Catch it alongside the decode failure and treat a binary frame as
   `invalid_frame` + `continue` (do not close). Document this choice in the
   protocol doc.

**Acceptance:** new tests (step 5) for oversized frame → `frame_too_large` +
close, and for `not json` / `[1,2]` → `invalid_frame` and the connection
stays open.

## Step 3 — Server-initiated heartbeat and dead-peer detection

**Objective:** server sends `ping` every `heartbeat_seconds`; closes
connections with no inbound frame for `miss_limit` intervals.

**Files:** `agent/server.py` only. **No client change is required** — the
client already replies `pong` (`ui/app.js:7389`); see the "Client already
conforms" note in the preamble. Do not edit `ui/app.js`, and therefore do not
bump the PWA version (the mandatory 5-spot bump only applies when `ui/app.js`,
`style.css`, or `index.html` actually change).

**Actions:**

1. In `realtime_v1`: track `last_inbound = loop.time()`, updated on every
   successfully received frame (any type — ack, pong, command). Add a
   heartbeat entry to the select loop. **Use an absolute deadline, not a bare
   `asyncio.sleep(interval)` recreated each iteration** — the loop re-wakes on
   *every* outbound event too, and a recreated sleep would be cancelled and
   restarted each wake, so on a chatty-but-dead-inbound peer the timer would
   never fire. Instead compute `heartbeat_task =
   asyncio.create_task(asyncio.sleep(max(0, next_ping_at - loop.time())))`
   where `next_ping_at` is advanced by `_REALTIME_HEARTBEAT_SECONDS` only when
   the sleep actually completes. When it completes: if `loop.time() -
   last_inbound > _REALTIME_HEARTBEAT_SECONDS * _REALTIME_HEARTBEAT_MISS_LIMIT`,
   close (code 1001 "going away"; document) and return; else
   `outbound.enqueue({"v": 1, "type": "ping", "payload": {}})` and set
   `next_ping_at = loop.time() + _REALTIME_HEARTBEAT_SECONDS`.
   - The safety poll caps `notifier.wait()` at 20s, so the loop wakes at least
     that often regardless; the deadline check above makes ping timing robust
     without any precision machinery.
2. Source `hello.payload.heartbeat_seconds` from the constant/config instead
   of the hardcoded `20` (`server.py:4068`).
3. Client `ping`→`pong`: **already implemented** at `ui/app.js:7389`,
   correctly placed after the `else if (frame.event_id)` branch (a `ping`
   carries no `event_id`, so it is not swallowed). Nothing to do — just
   confirm it during review.
4. Client `slow_consumer`/1013 reconnect: **already works.** `onclose`
   (`ui/app.js:7391`) reconnects on every close code with jittered backoff and
   resumes from the persisted cursor; there is no fatal special-case to
   remove. Skip the optional close-code `console.warn` — adding it would force
   a `ui/app.js` edit and the 5-spot PWA version bump (invalidating every
   client's cache) for a diagnostic log, which is not worth it. Leave the
   client untouched.

**Acceptance:** new server test (step 5): a client that never sends anything
receives a `ping` within ~1.5× interval (patch the constants small) and is
closed after the miss limit; a client that sends `pong` (or any frame) stays
open. No e2e/client assertion needed — the client path is unchanged.

## Step 4 — Ack bookkeeping and version machinery

**Objective:** parse and record `ack`; centralize the supported-version set.

**Files:** `agent/server.py`, `docs/realtime-protocol-v1.md`,
`docs/decisions/0040-versioned-realtime-protocol-over-websocket.md`.

**Actions:**

1. Add `_REALTIME_SUPPORTED_VERSIONS = (1,)` next to the other constants.
   Use it for: `hello.payload.supported_versions`, the inbound `v` check
   (`frame.get("v") not in _REALTIME_SUPPORTED_VERSIONS` at `server.py:4122`),
   and the `unsupported_version` error payload. No behavior change today.
2. Replace the silent `ack` ignore (`server.py:4184`) with a real branch:
   - Validate `payload.event_id` is an int ≥ 0; else `invalid_frame`.
   - Clamp to the server's current cursor (an ack ahead of the server is a
     client bug; clamp and log at DEBUG, do not error).
   - Store `last_acked_cursor = max(last_acked_cursor, acked)` on the
     connection state (plain local var is fine — it's per-connection and
     dies with the socket).
   - Keep `pong` silently accepted.
3. Include `last_acked_cursor` in the `slow_consumer` WARNING log from step 1
   and, if the server has a realtime/diagnostic health surface, expose count
   of open connections + min/max acked cursor there only if such a surface
   already exists — do not build a new endpoint for this.
4. Protocol doc: state that acks are advisory bookkeeping on the server
   (resumption is driven by the subscribe cursor), that `ping` is
   server-initiated at `heartbeat_seconds` with a 2-miss close, and the
   chosen close codes for `slow_consumer` (1013), `frame_too_large` (1009),
   and heartbeat timeout (1001). Update the error-code list to match
   implementation exactly.
5. ADR-0040 status table (`0040-...:115-117`): move "Backpressure and frame
   limits" and "Heartbeat and acknowledgements" to Implemented with one-line
   descriptions; update "Protocol compatibility" to note the machinery is in
   place with only v1 defined. Update the "Remaining implementation sequence"
   to drop items 1–2 and renumber.

**Acceptance:** `test_websocket_rejects_protocol_version_skew`
(`tests/test_realtime.py:169`) still passes and now asserts the error echoes
the configured set; new ack tests from step 5 pass.

## Step 5 — New server tests

**File:** `tests/test_realtime.py` (append; follow the existing
tmp_path/monkeypatch + websocket client fixtures used from line 133 onward).
Patch the new constants to tiny values via monkeypatch where timing matters.

1. `test_outbound_queue_coalesces_process_changed` — fill the queue with a
   stalled sender (monkeypatch the sender to block), publish two
   `process.changed` events, unblock, assert the client receives one (the
   latest).
2. `test_slow_consumer_closes_with_resumable_error` — stall the sender,
   enqueue > limit non-coalescible events (e.g. `chat.text` via the durable
   log), assert client gets the `slow_consumer` error then a 1013 close, and
   that reconnecting with the last applied cursor replays the missed events.
3. `test_frame_too_large_closes_connection` — send a frame just over the
   (patched-down) limit; assert `frame_too_large` + close 1009.
4. `test_malformed_frames_get_invalid_frame` — non-JSON text and a JSON
   array both yield `invalid_frame`, connection stays open.
5. `test_server_initiated_heartbeat_and_dead_peer_close` — patched 0.1s
   interval: client receives `ping`; a client that never responds is closed
   after the miss limit; a client that sends `pong` (or any frame) stays
   open.
6. `test_ack_cursor_is_recorded_and_clamped` — send `ack` with a valid and
   then an ahead-of-server cursor; assert no error and (via the slow_consumer
   log assertion or an exposed-for-test hook) the clamped value.
7. `test_subscribe_replay_under_outbound_pressure_preserves_order` — with a
   small queue and many retained events, assert event_id ordering is
   monotonic across a replay drain.

## Step 6 — Verification-gap audit (ADR item 3, partial)

Cross-check the "Required verification" list (`0040-...:443-457`) against
`tests/test_realtime.py`, `tests/test_flow.py`, and `tests/e2e/chat.spec.js`.
Already covered (do not rewrite): replay-window expiry/snapshot fallback
(`test_pruned_cursor_rolls_over_to_snapshot`), count/byte/age rollover
(lines 377/398), update racing snapshot (line 202), worker-thread publication
(line 234), notifier shutdown (line 121), retained-cursor rollover,
commit-before-notify. With step 5 done, slow consumers and version skew are
also covered. For each remaining item — disconnect before/after chat
metadata, full accumulated pending text + later deltas, duplicate mutation
requests, cancellation races, server restart during a run, multiple
simultaneous clients, terminal-event dedup, historical pagination alongside
live updates, safety-poll recovery — either point to the existing test by
name or write the missing test (prefer server tests; browser tests only for
the parity items listed in the ADR's browser section). Record the mapping as
a checked list appended to the ADR's verification section.

Do NOT remove SSE or flip the default transport; that is the separate
compatibility decision the ADR reserves.

## Completion criteria

- All tests in `tests/test_realtime.py` (old + new) pass; `tests/test_flow.py`
  and `tests/e2e/chat.spec.js` unaffected or extended per step 6.
- ADR-0040 status table and remaining-sequence list updated; protocol doc
  error/heartbeat/ack sections match the implementation.
- **No `ui/` files changed** (this work is server-only — see step 3), so the
  PWA version bump does NOT apply. Only bump the 5 version spots
  (`ui/sw.js` + `ui/index.html`) if you end up editing `ui/app.js`,
  `style.css`, or `index.html` for a reason this plan did not anticipate.
