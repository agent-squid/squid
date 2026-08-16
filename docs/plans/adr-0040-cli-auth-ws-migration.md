# Plan: CLI-auth (ADR-0035) → WebSocket migration (ADR-0040 step 1)

Execute the steps in order. Each step is self-contained with objective, files,
actions, and an acceptance check. Backend steps (2–5) are verifiable by the
server tests in step 7 before any client work. Line numbers reference the tree
at authoring time and may drift as you edit — anchor on function names.

## Design decisions (already settled — do not revisit)

- **Transport only.** Move CLI-auth PTY interaction onto `/ws/v1`, mirroring
  `chat.*`. The PTY/security model is untouched. SSE stays as fallback.
- **Decision A — output is TRANSIENT.** Do NOT route `auth.output` through
  `insert_realtime_event` / the durable `realtime_events` log, and do NOT add
  any `auth.*` type to `_REALTIME_V1_REPLAY_TYPES`. Reuse the existing in-memory
  ring buffer + per-listener `asyncio.Queue`. Rationale: login output contains
  device codes / OAuth URLs / echoed tokens, and TUI logins emit high-volume
  ANSI redraws — neither belongs in durable storage.
- **Decision B — implicit socket listener, no new scope.** Do NOT add an
  `auth_session_id` scope to `_authorize_realtime_scopes` / `_realtime_scope_sql`
  / `_realtime_snapshot`. `auth.start` registers the *calling socket* as the
  session's output listener; output pushes directly to that socket. No subscribe
  step, no new scope authorization surface, no start→subscribe race.
- **Timer stays as-is.** Keep the idle reaper (`AUTH_SESSION_IDLE_TIMEOUT_SECONDS
  = 1200`, activity-reset). Do NOT add a hard total-duration/wall-clock cap.

## Invariants — do NOT touch

In `agent/auth_sessions.py`: the command allowlist (`_login_argv` /
`_install_argv` / `_model_argv`), `_spawn_pty`, `_register_proc` registration
(`_AUTH_SESSION_TOPIC`, `agent=target_id`), the idle reaper and
`AUTH_SESSION_IDLE_TIMEOUT_SECONDS`, `_POST_EXIT_RETENTION_SECONDS`, and the 64KB
ring (`AuthSession.buffer` / `broadcast` / `_REPLAY_BUFFER_CAP`).

Keep the existing HTTP/SSE routes (`auth_session_create` / `_events` / `_input`
/ `_resize` / `_cancel`, `server.py:1485-1561`) and the client's HTTP path fully
intact — WS is added *alongside*, per ADR-0040's staged-migration rule. SSE
removal is a separate future decision.

Both transports must drive the same `AuthSession` via `broadcast` / `listeners`
with no shared per-transport mutable state, so toggling config can never
half-wire a session.

---

## Step 1 — Protocol doc

**File:** `docs/realtime-protocol-v1.md`

Add the `auth.*` family:

- `auth.start` — idempotent mutation via `request_id` (like `chat.start`).
  Payload `{harness, mode, model?, cols, rows}`. Result `{ok, session_id}` or
  `{ok:false, error, detail}`.
- `auth.input` — fire-and-forget, NOT persisted. Payload `{session_id, data}`.
- `auth.resize` — fire-and-forget, NOT persisted. Payload `{session_id, cols, rows}`.
- `auth.cancel` — idempotent mutation via `request_id` (like `chat.cancel`).
  Payload `{session_id}`.
- `auth.output` (server→client) — `{v:1, type:"auth.output", payload:{session_id, data}}`.
  `data` is a base64-encoded PTY chunk. **Transient — not replayable.**
- `auth.done` (server→client) — `{v:1, type:"auth.done", payload:{session_id, returncode}}`.
  **Transient.**

**Acceptance:** doc describes all six message types and explicitly marks
`auth.output` / `auth.done` as transient (not in the replay log).

---

## Step 2 — Listener helper in auth_sessions.py

**File:** `agent/auth_sessions.py`

Add a small helper (no logic change to existing machinery):

```python
def attach_listener(session: AuthSession) -> asyncio.Queue:
    """Register a fresh output queue and prime it with the ring buffer.

    Appends the queue to session.listeners, then snapshots the buffer with NO
    await in between (broadcast runs synchronously on the loop from the reader
    callback, so nothing can interleave) — same ordering guarantee as
    stream_events. If the session already exited, also enqueue the done
    sentinel so the caller emits auth.done immediately.
    """
    q: asyncio.Queue = asyncio.Queue()
    session.listeners.append(q)
    snapshot = bytes(session.buffer)
    if snapshot:
        q.put_nowait(snapshot)
    if session.state == "exited":
        q.put_nowait(None)
    return q
```

`mark_exited` already does `put_nowait(None)` to every listener (the done
sentinel) — reuse it, don't add a parallel signal.

**Acceptance:** helper compiles; existing `stream_events`/SSE path unchanged.

---

## Step 3 — auth.start / auth.cancel dispatch (server.py)

**File:** `agent/server.py`

1. Extend `_handle_realtime_mutation` (`server.py:3787`): change its allowed set
   from `{"chat.start","chat.cancel"}` to also include `{"auth.start","auth.cancel"}`.
   Add branches:
   - `auth.start` → `result = await _realtime_auth_start(payload, websocket)`,
     then `save_realtime_request`.
   - `auth.cancel` → look up `session_id` from payload; `await cancel_session(session_id)`;
     `result = {"ok": True, "cancelled": <bool>, "session_id": session_id}`;
     `save_realtime_request`.
2. Add `_realtime_auth_start(payload, websocket)`:
   - Validate payload → call `create_session(harness, cols, rows, mode=mode, model=model)`.
   - Catch `NoLoginCommand` / `AuthSessionError` → return `{ok:false, error, detail}`.
   - On success, register this socket as the listener: `q = attach_listener(session)`,
     stash `q` + `session.id` on the connection so the pump (step 5) drains it.
   - Return `{ok:true, session_id: session.id}`.
   - **Idempotent replay:** when `_handle_realtime_mutation` finds a stored
     result, re-attach the listener to the still-live session (`get_session(stored_id)`)
     and replay its ring via `attach_listener`; if the session is gone, return
     the stored result unchanged. (Handle this in the mutation path so a WS
     reconnect that resends `auth.start` re-wires output.)
3. In the `realtime_v1` main loop dispatch chain (`server.py:~3900`), add
   `auth.start` / `auth.cancel` alongside `chat.start` / `chat.cancel` — same
   `if not principal: client_identity_required` guard, same route into
   `_handle_realtime_mutation`.

**Acceptance:** covered by step 7 tests — `auth.start` returns a `session_id`,
resend with same `request_id` does not spawn a second PTY, `auth.cancel` is
idempotent.

---

## Step 4 — auth.input / auth.resize (server.py)

**File:** `agent/server.py`

In the `realtime_v1` dispatch chain, handle these **inline** (not via
`_handle_realtime_mutation`, no `save_realtime_request` — fire-and-forget):

- `auth.input`: `session = get_session(payload["session_id"])`; if missing →
  `command.result` with `{ok:false, error:"unknown_session"}`. Else
  `write_input(session, payload["data"].encode())` (base64-decode if you chose
  base64 for input; keep input encoding consistent with the client) and ack
  `{ok:true}`. **Must go through `write_input`** so `touch()` keeps the idle
  timer alive — do not write to the fd directly.
- `auth.resize`: `resize(session, cols, rows)`; ack `{ok:true}`. (`resize` does
  not `touch()` — that is intended existing behavior; leave it.)

**Acceptance:** step 7 asserts input reaches the session and neither type writes
a `realtime_requests` row.

---

## Step 5 — Transient output pump (server.py)

**File:** `agent/server.py`, inside `realtime_v1` (`server.py:3817`)

Critical: Starlette `WebSocket.send_*` is NOT safe for concurrent writers. Do
NOT spawn a background task that sends while the main loop also sends. Integrate
the auth output queue as a **third awaitable** in the existing
`asyncio.wait({receive_task, notify_task})` at `server.py:3837`.

- Track connection-local `auth_output_q: Optional[asyncio.Queue]` and
  `auth_session_id: Optional[str]`, set by step 3 when `auth.start` succeeds.
- When `auth_output_q` is set, add `output_task = asyncio.create_task(auth_output_q.get())`
  to the `wait` set. On completion:
  - bytes chunk → `await websocket.send_json({"v":1,"type":"auth.output","payload":{"session_id":auth_session_id,"data":<base64>}})`, then re-arm `output_task`.
  - `None` sentinel → `await websocket.send_json({"v":1,"type":"auth.done","payload":{"session_id":auth_session_id,"returncode":get_session(auth_session_id).returncode if still present else None}})`, then clear `auth_output_q` / `auth_session_id`.
- Follow the existing pending-task cancel/gather cleanup pattern (`server.py:3841-3845`)
  so `output_task` is cancelled cleanly on each loop turn alongside the others.
- On `WebSocketDisconnect` / loop exit, remove `q` from `session.listeners`
  (guard with try/except ValueError, like `stream_events` does).

All `send_json` stays on the single loop coroutine — no concurrent writes.

**Acceptance:** step 7 asserts a live PTY chunk arrives as an `auth.output`
frame after `auth.start`, and `auth.done` with the returncode on exit; ring
buffer emitted before any live chunk on attach.

---

## Step 6 — Client (ui/app.js)

**File:** `ui/app.js`

Respect `realtime.transport: auto|websocket|sse` exactly as chat does:

- `sse` → never enter the WS auth path; use today's `openAuthPanel` HTTP/SSE
  path unchanged.
- `websocket` → WS only.
- `auto` → try WS; fall back to the HTTP/SSE path only if the WS command *send
  itself* fails (not silently on every error).

For the WS path, reuse the existing DOM / xterm / retry UI — swap transport only:
- `auth.start` over WS → read `session_id` from the ack.
- Feed `auth.output` payloads into the existing `term.write` (base64-decode first).
- Wire xterm `onData` → `auth.input`; resize handler → `auth.resize`.
- Cancel/Close → `auth.cancel`.
- On `auth.done`, render exit as the SSE path does today.

**Bump the PWA version marker** (project convention — stale cached assets
otherwise; applies to `ui/app.js`, and `style.css` / `index.html` if touched).

**Acceptance:** with `transport: sse` the WS auth code is never entered and the
panel behaves byte-identically to today; with `websocket`/`auto` the panel runs
over WS. Verified in steps 8–9.

---

## Step 7 — Server tests

**File:** `tests/test_realtime.py`

Mirror `test_websocket_subscribe_snapshot_live_event_and_idempotent_cancel`. Use
a stub/fake PTY session so no real login CLI spawns. Assert:
- `auth.start` returns a `session_id`.
- Ring buffer replays on attach (buffered bytes arrive before live output).
- A live PTY chunk arrives as `auth.output`; exit yields `auth.done` with returncode.
- `auth.cancel` is idempotent; resent `auth.start` with same `request_id` does
  not spawn a second session.
- `auth.input` / `auth.resize` do NOT write `realtime_requests` rows and input
  reaches the session (touch() updates activity).

**Acceptance:** `pytest tests/test_realtime.py` passes.

---

## Step 8 — E2E test

**File:** `tests/e2e/chat.spec.js` pattern (new spec or additions), using the
`MockWebSocket` harness.

Cover the auth panel over WS: start → receive output → send input → cancel; and
that `transport: sse` still drives the HTTP path.

**Acceptance:** the e2e suite passes.

---

## Step 9 — Verification gate

- Run the full server + e2e suites; all green.
- Visually confirm the auth panel renders correctly (screenshot, zoomed in) in
  both `websocket` and `sse` modes — the xterm/auth panel has regressed before
  specifically when visual checks were skipped. Do not mark the migration done
  on tests alone.

---

## Step 10 — ADR update

**File:** `docs/decisions/0040-versioned-realtime-protocol-over-websocket.md`

Mark CLI-auth migration done in the status table and "Remaining implementation
sequence", noting SSE remains until the separate parity/removal decision.

**Acceptance:** ADR status reflects step 1 complete; remaining steps
(backpressure/framing, delivery acks + prior-version support, SSE removal,
ADR-0039 Shore relay) still listed as pending.

---

## Out of scope

Backpressure/framing, delivery acknowledgements + prior-protocol-version
support, SSE removal, and ADR-0039 Shore relay — later ADR-0040 steps.
