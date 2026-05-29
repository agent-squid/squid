---
status: accepted
date: 2026-05-25
updated: 2026-05-28
---
# ADR-0001: Session Management — Resumable and Adhoc Modes

## Overview

Squid supports two session modes, selected per-message via input syntax:

- **Resumable** (`#topic@alias message`) — CLI owns the conversation state via
  `--resume <session_id>`. Token-efficient; native conversation format. Used for
  ongoing sessions where continuity matters.
- **Adhoc** (`#topic@alias! message` or `#topic@alias!N message`) — stateless
  oneshot invocation. History is injected as a synthetic text block via
  `_build_prompt`. Used for parallel, context-scoped turns where the user
  explicitly selects how much history to include.

## How Resumable Sessions Work

Each `(topic, alias)` pair maps to a stored `session_id` and locked `cwd` in
`topic_sessions`. On every message, Squid looks up the stored record:

- **No stored session** → fresh CLI invocation; session_id captured from the
  first response and stored
- **Stored session** → `--resume <session_id>` passed to the CLI; the CLI
  continues the conversation natively

The `cwd` is locked at session creation (see ADR-0003). Changing an alias's
`cwd` after a session starts has no effect until the session is cleared.

Session clearing — via `/clear` command or `DELETE /topics/{topic}/session` —
wipes both `session_id` and `cwd`, allowing the next message to start fresh.
`/clear` also kills any in-flight CLI subprocess for the topic.

## How Adhoc Turns Work

Adhoc turns (`!`) bypass session resumption entirely. Each turn is independent:

- `_build_prompt` injects the last N non-adhoc session turns as a `<conversation_history>` block
- No `session_id` is stored or used

Adhoc turns run in parallel on the same topic without queuing constraints.

## Backend Support

| Backend     | Resumable flag          | Session ID field  |
|-------------|-------------------------|-------------------|
| Claude      | `--resume SESSION_ID`   | `system.session_id` |
| Codex       | `exec resume SESSION_ID`| `thread.started.thread_id` |
| Cursor      | `--resume SESSION_ID`   | `system.session_id` |
| Copilot     | `--resume=SESSION_ID`   | `result.sessionId` |
| Antigravity | `--conversation ID`     | `system.session_id` |

All backends emit a session/thread identifier in their first event. Squid
captures this and stores it after every response, so a `/clear` followed by a
new message immediately establishes a fresh session_id for the next turn.

## Stale Session Recovery

When `--resume <session_id>` fails with "No conversation found" — most
commonly after a reboot changes the resolved `cwd` (e.g. `/tmp/squid` was
previously a symlink to a different path) — Squid recovers automatically:

1. A `_status` event is emitted with the stale session details (session_id,
   cwd, backend, model) so the user sees what was lost.
2. The prompt is retried immediately as a fresh invocation (no `--resume`).
3. The new `session_id` from the fresh run is stored via `set_topic_session`,
   replacing the stale record. Subsequent turns resume normally.

This recovery is handled in `topic_queue._process` and requires no user
action. It is semantically equivalent to an implicit `/clear` followed by a
replay of the original message.

## Consequences

- Good: resumable path is token-efficient; CLI owns context natively
- Good: adhoc path gives precise control over injected context
- Good: both modes coexist on the same topic via `!` syntax
- Bad: two code paths (`_build_prompt` + resume logic) to maintain
- Bad: Codex `exec resume` does not support `/clear` or `/compact` natively —
  session reset must be handled by Squid (see ADR-0013)
