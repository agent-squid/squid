---
status: superseded
date: 2026-05-25
superseded: 2026-05-29
updated: 2026-06-12
---
# ADR-0007: Cross-session Context Injection

## Original Design (Superseded 2026-05-29)

The original design used a server-side `session_context_log` table to track one-shot
cross-session injections. Pinning a message server-side queued it for injection into any
`(topic, agent)` session that hadn't absorbed it yet. `get_pending_injections` returned
outstanding messages on each dispatch, and `GET /context/{topic}?agent=X` exposed pending
vs absorbed state.

**This mechanism was removed.** `session_context_log` is explicitly dropped in `init_db()`
(`DROP TABLE IF EXISTS session_context_log`). The `pinned` column and `pin_count` were also
dropped from `chat_messages` and `session_stats` respectively. `get_pending_injections`
does not exist in the codebase.

## Current Design (Updated 2026-06-12)

Context injection is **client-driven** via the pins feature, with injection state backed by
the server DB for cross-device correctness.

### Storage

- **Pins**: stored in `localStorage` as `{ id, topic, agent, session_id, content }`.
  Persist across page reloads and survive refreshes.
- **Injection tracking**: `injectedInto` localStorage map keyed by **`session_id`**
  (previously keyed by `topic@agent` — see below). Records which pin IDs have been sent
  to a given session. Seeded from the server on session load.
- **DB record**: `chat_messages.context` on user rows stores `{"pins": [...], "mem": bool}` —
  the pin IDs and topic-memory flag that were active at send time. This is the ground truth
  for cross-device seeding.

### Cross-device Correctness

On session load, `GET /topics/{topic}/session` returns `injected_ids` — the union of all
pin IDs from `chat_messages.context` for that session. The client seeds
`injectedInto[session_id]` from this, so any device resuming the session immediately knows
what has already been injected, without needing to have sent those turns itself.

The flow:
1. `GET /topics/{topic}/session` → `{ session_id, cwd, injected_ids }`
2. Client: `injectedInto[session_id] = union(local, injected_ids)`
3. At send time: skip pins in `injectedInto[_currentSid]`
4. After completion: `injectedInto[lastSessionId] |= _pinnedIds` (written locally,
   durable via the DB on the next load)

### Why `session_id` as Key (Changed from `topic@agent`)

The old `topic@agent` key never expired. If session A for `squid@claude` injected pin #47,
`injectedInto['squid@claude']` persisted indefinitely. When session B started, pin #47 still
showed `injected · skip` even though session B had never seen it — silently dropping context.

Keying by `session_id` is correct: each session is independent, tracking resets naturally
when a new session starts, and the key is cleared explicitly when the session is deleted
(`clearCachedSessionId`).

### Key Differences from Prior Designs

| | Original (removed) | 2026-05-29 design | Current (2026-06-12) |
|---|---|---|---|
| Storage | Server DB (`session_context_log`) | Client `localStorage` only | `localStorage` + DB record |
| Tracking key | Per-session DB rows | `topic@agent` in localStorage | `session_id` in localStorage |
| Cross-device | Full server sync | Not supported | Seeded from DB on session load |
| Trigger | Automatic on dispatch | Explicit client selection | Explicit client selection |

`GET /context/{topic}` still exists but now returns the same data as
`GET /topics/{topic}/session` — just the active `session_id` and `cwd`.

**Contract tests**: `tests/e2e/pin.spec.js`

## Why the Original Server-side Approach Was Removed

The `session_context_log` table added schema complexity and required cleanup logic for
inactive sessions. The client-side approach is simpler: the user explicitly selects what
to inject, and there is no server cleanup burden.
