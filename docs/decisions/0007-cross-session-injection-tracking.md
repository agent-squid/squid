---
status: accepted
date: 2026-05-25
---
# ADR-0007: One-shot Cross-session Injection via `session_context_log`

## Context and Problem Statement

With resumable sessions, each `(topic, alias)` lane owns its own CLI context. To share a useful
turn from one lane into another (e.g. a pinned claude response shared into a codex session on
the same topic), we need to inject it exactly once — at the moment of the next message to the
target session. Without tracking, every subsequent message would re-inject the same content.

The same problem applies to adhoc (`!`) turns: they run outside the session and are not in
context by default, but a user may want to promote one into a session.

## Considered Options

1. Track injected messages per session in a dedicated log table
2. Use a timestamp watermark per session (inject everything pinned before timestamp X)
3. Re-inject all pinned messages on every message (idempotent but wasteful)

## Decision Outcome

**Option 1.** A `session_context_log` table records which `msg_id` values have been injected
into each `(topic, alias)` session. On each message dispatch, `get_pending_injections` returns
pinned messages not yet in the log. They are prepended to the current message as a one-time
injection, then recorded.

**The pin mechanic is the cross-session sharing primitive.** Pinning a message (from any source:
adhoc turn, other model, other topic) queues it for one-time injection into any `(topic, alias)`
session that hasn't absorbed it yet.

Clearing a session also clears its `session_context_log` rows — a fresh session re-absorbs all
currently-pinned messages on its first message.

## Consequences

- Good: no duplicate injection; efficient for long-running sessions
- Good: pin mechanic is already in place; no new user-facing concept needed
- Good: injection is transparent — `GET /context/{topic}?alias=X` shows pending and absorbed
- Bad: log table grows over time; needs periodic cleanup for inactive sessions
- Bad: injected messages may conflict with what the CLI session already knows (edge case)
