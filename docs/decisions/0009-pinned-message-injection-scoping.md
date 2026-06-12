---
status: accepted
date: 2026-05-26
superseded-section: 2026-05-29
---
# ADR-0009: Pinned Message Injection Scoping by Mode

## Original Design (Superseded)

The original design used a server-side `get_pending_injections` / `inject_history` mechanism
to push pinned messages into sessions automatically. Exclusion was performed server-side using
`exclude_session_id=resume_session_id` against `chat_messages.session_id`. This was removed
along with the `session_context_log` table (see [[0007-cross-session-injection-tracking]]).

## Current Design

Injection is now **explicitly client-driven**. The client selects which pins to send and
the server injects them. Same-session exclusion happens on the client before the request is made.

### Scoping by mode

| Mode | How pinned content enters | Same-session exclusion |
|---|---|---|
| Session turn (`--resume`) | Prepended as `<referenced_context>` block in `effective_message` | Client skips pins whose `session_id` matches `_sessionIds[topic@agent]` |
| Adhoc turn (`!`) | Prepended to `context_history` for `_build_prompt`, deduped against lookback window | Client skips same-session pins |

### Client-side exclusion

Pins store `{ id, topic, agent, session_id, content }` in localStorage. The client
tracks the most recent `session_id` per `topic@agent` in a `_sessionIds` map (populated from
the `stats` SSE event on each response). At send time, a pin is skipped if:

```
item.session_id && currentSessionId && item.session_id === currentSessionId
```

This means same-`topic@agent` pins from a **prior** session are still eligible for
injection — only the exact current session is excluded. Same-`topic@agent` ≠ same session.

### Server-side injection

The server receives `pinned_ids: [id, ...]` in `POST /chat` and:

1. Fetches the rows via `get_messages_by_ids(filtered)` — assistant messages only, `status='done'`
2. Deduplicates against the adhoc lookback window (`context_ids`) to avoid double-injecting
   content already in `context_history`
3. For **session turns**: builds `effective_message` with a `<referenced_context>` prefix so
   the CLI receives the supplementary context that `--resume` does not carry
4. For **adhoc turns**: prepends the fetched pairs to `context_history`, which `_build_prompt`
   incorporates into the prompt

### Re-injection prevention

After a successful injection, the client records the injected IDs in an `injectedInto`
localStorage map keyed by `topic@agent`. Subsequent turns skip those IDs (`already added · skip`).
This prevents the same pin from being sent on every turn.

### UI status labels (pin panel)

| Status | Meaning |
|---|---|
| `will inject` | Will be sent in `pinned_ids` on next turn |
| `in session · skip` | `session_id` matches current session — already in `--resume` context |
| `already added · skip` | Previously injected into this `topic@agent`; in `injectedInto` map |

**Contract tests**: `tests/e2e/pin.spec.js`

## Consequences

- Good: no server-side pending queue or cleanup logic
- Good: same-session exclusion is session-id–precise — a prior session under the same
  `topic@agent` is correctly treated as cross-session and injected
- Good: session turns get supplementary context via `<referenced_context>` without
  disrupting `--resume` history
- Neutral: pins are device-local (localStorage); cross-device sync not supported
  (see [[0007-cross-session-injection-tracking]] — ephemerality section)
- Neutral: if `_sessionIds` has no entry for the current `topic@agent` (first turn of a new
  browser session), same-`topic@agent` pins are not excluded — they inject, which may
  be redundant but is not harmful
