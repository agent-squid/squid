---
status: accepted
date: 2026-05-26
updated: 2026-06-12
---
# ADR-0009: Pinned Message Injection Scoping by Mode

## Original Design (Superseded 2026-05-29)

The original design used a server-side `get_pending_injections` / `inject_history` mechanism
to push pinned messages into sessions automatically. Exclusion was performed server-side using
`exclude_session_id=resume_session_id` against `chat_messages.session_id`. This was removed
along with the `session_context_log` table (see [[0007-cross-session-injection-tracking]]).

## Current Design (Updated 2026-06-12)

Injection is **explicitly client-driven**. The client selects which pins to send and the
server injects them.

### Scoping by mode

| Mode | How pinned content enters | Same-session exclusion |
|---|---|---|
| Session turn (`--resume`) | Prepended as `<referenced_context>` block in `effective_message` | Client skips pins whose `session_id` matches `_sessionIds[topic@agent]` |
| Adhoc turn (`!`) | Prepended to `context_history` for `_build_prompt`, deduped against lookback window | Client skips same-session pins |

### Client-side exclusion at send time

Pins store `{ id, topic, agent, session_id, content }` in localStorage. At send time
the client filters the pin list through three checks in order:

1. **Same-session skip**: if `item.session_id === currentSid && !adhoc` → skip.
   The session already carries this context via `--resume`.
2. **Fresh adhoc pass-through**: if `adhoc && lookback === 0` → always include.
   Each fresh adhoc turn has an empty context window.
3. **Already-injected skip**: if `injectedInto[currentSid]` contains `item.id` → skip.
   This pin was sent in a prior turn of this session.

### Server-side injection

The server receives `pinned_ids: [id, ...]` in `POST /chat` and:

1. Fetches rows via `get_messages_by_ids(filtered)` — assistant messages, `status='done'`
2. Deduplicates against the adhoc lookback window to avoid double-injecting context
   already in `context_history`
3. For **session turns**: builds `effective_message` with a `<referenced_context>` prefix
4. For **adhoc turns**: prepends fetched pairs to `context_history`

### DB record at send time

`chat_messages.context` on the user message row stores:

```json
{"pins": [47, 52], "mem": true}
```

This records exactly what was injected (pin IDs + whether topic memory was included).
It serves as the ground truth for cross-device injection tracking (see below).

### Re-injection prevention

`injectedInto` in localStorage is keyed by **`session_id`** (not `topic@agent`).

- **On session load**: `GET /topics/{topic}/session` returns `injected_ids` — the union of
  all pin IDs in `chat_messages.context` for that session. The client seeds
  `injectedInto[session_id]` from this response, giving any device resuming the session
  immediate knowledge of what was already injected.
- **After a successful turn**: the client adds `_pinnedIds` to `injectedInto[lastSessionId]`.
- **On session delete**: `injectedInto[session_id]` is cleared.

Keying by `session_id` rather than `topic@agent` ensures stale tracking from a prior session
never suppresses injection in a new one.

### UI status labels (pin panel)

| Status | Class | Meaning |
|---|---|---|
| `will inject` | `pin-status-inject` | Will be sent in `pinned_ids` on next turn |
| `in session · skip` | `pin-status-session` | `session_id` matches current — already in `--resume` context |
| `injected · skip` | `pin-status-done` | In `injectedInto[session_id]` — sent in a prior turn |

The distinction between `in session` and `injected` matters: `in session` means the model
has it via session continuity (you can't avoid it), while `injected` means it was explicitly
sent as supplementary context in a prior turn.

**Contract tests**: `tests/e2e/pin.spec.js`

## Consequences

- Good: no server-side pending queue or cleanup logic
- Good: same-session exclusion is session-id–precise — a prior session under the same
  `topic@agent` is correctly treated as cross-session and injected
- Good: cross-device correctness for `injectedInto` state — DB is ground truth, seeded
  on session load
- Good: DB record (`chat_messages.context`) provides an audit trail of what was injected
- Neutral: pins themselves (the basket) remain device-local in localStorage — cross-device
  pin basket sync is not implemented
- Neutral: if `_sessionIds` has no entry for the current `topic@agent` (first turn of a new
  browser session before the session API responds), same-`topic@agent` pins are not excluded
  — they inject, which may be redundant but is not harmful
