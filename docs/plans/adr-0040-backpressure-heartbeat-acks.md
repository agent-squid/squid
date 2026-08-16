# Plan: ADR-0040 remaining steps 1–2 — backpressure, heartbeat, acks, version machinery

Execute the steps in order. Each step has objective, files, actions, and an
acceptance check. Steps 1–4 are server-only and verifiable by the tests in
step 5 before any client work. Line numbers reference the tree at authoring
time (post-`ba330ca`) and drift as you edit — anchor on function names.

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
  `message.changed` is NOT coalesced: the client's reconciler tolerates
  duplicates but status transitions feed `refreshComposerSessionCount`, and
  the protocol doc only authorizes discarding state-like updates during
  rollover, not live delivery. Keep the coalescing set minimal.
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
4. In `realtime_v1` (`server.py:4053`):
   - Instantiate `_RealtimeOutbound` and a sender task
     `asyncio.create_task(_ws_sender(websocket, outbound))` right after
     `accept()`. `_ws_sender` loops `await websocket.send_json(await
     outbound.get())`.
   - Add the sender task to every `asyncio.wait` wait_set alongside
     `receive_task`/`notify_task`/`output_task`. If the sender task is in
     `done`, its exception (e.g. `WebSocketDisconnect` on send) must tear
     down the connection: re-raise/break into the existing
     `except WebSocketDisconnect` cleanup.
   - Replace ALL `await websocket.send_json(...)` in `realtime_v1`,
     `_handle_realtime_mutation`, `_handle_auth_input`, `_handle_auth_resize`,
     `_handle_auth_cancel`, and the auth-output/auth-done pump
     (`server.py:3891-4210`) with `outbound.enqueue(...)`; pass `outbound`
     into the handler functions as a new parameter.
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

1. Replace `websocket.receive_json()` (`server.py:4078`) with
   `frame_raw = await websocket.receive_text()` (catch
   `WebSocketDisconnect` as today). If
   `len(frame_raw.encode("utf-8")) > _REALTIME_MAX_FRAME_BYTES`: direct-send
   `{"v":1,"type":"error","payload":{"code":"frame_too_large"}}` (bypass the
   queue — the connection is about to die and the queue may be full), then
   `close(code=1009)` and return.
2. Wrap `json.loads(frame_raw)`; on `ValueError` enqueue the existing
   `invalid_frame` error envelope and `continue`. Require the result to be a
   `dict`; non-dict JSON (arrays, scalars) also gets `invalid_frame`.
3. `receive_bytes` frames: Starlette's `receive()` would surface them via
   `receive_text` raising — verify behavior and treat any non-text receive as
   `invalid_frame` + continue, or close with 1003. Pick one, document it in
   the protocol doc.

**Acceptance:** new tests (step 5) for oversized frame → `frame_too_large` +
close, and for `not json` / `[1,2]` → `invalid_frame` and the connection
stays open.

## Step 3 — Server-initiated heartbeat and dead-peer detection

**Objective:** server sends `ping` every `heartbeat_seconds`; closes
connections with no inbound frame for `miss_limit` intervals.

**Files:** `agent/server.py`, `ui/app.js`.

**Actions:**

1. In `realtime_v1`: track `last_inbound = loop.time()`, updated on every
   successfully received frame (any type). Add a heartbeat wait entry to the
   select loop: `asyncio.create_task(asyncio.sleep(_REALTIME_HEARTBEAT_SECONDS))`
   in the wait_set (recreate each iteration like `notify_task`). When it
   completes: if `loop.time() - last_inbound > _REALTIME_HEARTBEAT_SECONDS *
   _REALTIME_HEARTBEAT_MISS_LIMIT`, close (code 1001 or 1011 — pick 1001
   "going away"; document) and return; else `outbound.enqueue({"v": 1,
   "type": "ping", "payload": {}})`.
   - Note the safety poll already caps `notifier.wait()` at 20s; the
     heartbeat sleep aligns with that, so worst-case ping period is ~2×
     interval — acceptable; do not add timer precision machinery.
2. Source `hello.payload.heartbeat_seconds` from the constant/config instead
   of the hardcoded `20` (`server.py:4068`).
3. Client (`ui/app.js`, the realtime client around line 7240-7400): on
   incoming frame `type === 'ping'`, respond `{v: 1, type: 'pong', payload: {}}`.
   Check the frame dispatch in `onmessage` — server `ping` must be handled
   before the event-dispatch switch so it isn't swallowed as an unknown type.
4. Client: treat server closes with code 1013 (`slow_consumer`) as ordinary
   reconnects — the existing jittered backoff + persisted-cursor resume
   already does the right thing; just make sure no code path special-cases
   1013 as fatal. Add a console.warn with the close code.
5. **Version bump (mandatory per project convention):** after editing
   `ui/app.js`, bump the version string in all 5 spots across `ui/sw.js` and
   `ui/index.html` or the PWA serves stale cache.

**Acceptance:** new test: a client that never sends anything gets a `ping`
within ~1.5× interval (patch the constants small in the test) and is closed
after the miss limit. Client change verified by the e2e heartbeat assertion
in step 5 (or manual: devtools shows pong replies).

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
- UI version strings bumped in all 5 spots (`ui/sw.js` + `ui/index.html`).
