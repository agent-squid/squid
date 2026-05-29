---
status: accepted
date: 2026-05-25
updated: 2026-05-28
---
# ADR-0003: `cwd` Locked at Session Creation

## Context and Problem Statement

Claude CLI stores session files under `~/.claude/projects/<cwd-hash>/`. The hash is derived from
the working directory the CLI was launched from. If `--resume <session_id>` is called from a
different `cwd`, the CLI cannot find the session files and the resume fails.

This means the `cwd` used at session creation must be used for all subsequent messages in that
session. It cannot be changed mid-session without starting a new session.

## Considered Options

1. Lock `cwd` at session creation; store it alongside `session_id`
2. Always use a single fixed `cwd` (e.g. `SQUID_HOME`) for all sessions
3. Re-detect `cwd` from agent config on every message

## Decision Outcome

**Option 1.** The `cwd` is stored in `topic_sessions` at first message and used for all
subsequent `--resume` calls for that `(topic, agent)` session.

On each message, Squid uses `stored["cwd"]` from `topic_sessions` — not the agent config's
current `cwd`. This ensures `--resume` always points to the same project directory the CLI
used when the session was created.

## Changing `cwd` mid-flight

Changing an agent's `cwd` (or `backend`/`model`) via `POST /config/agents` is detected as
a key attribute change. Squid immediately deletes all `topic_sessions` rows for that agent
(`clear_agent_sessions` → `DELETE FROM topic_sessions WHERE agent = ?`). The stored `cwd`
is not updated in-place — the row is gone.

The next message finds no stored session, reads the new `cwd` from the `agents` table, and
creates a fresh `topic_sessions` row with the new value. This is equivalent to a forced
`/clear` across all topics that agent is active in.

Clearing a single session manually (`/clear` or `DELETE /topics/{topic}/session?agent=X`)
deletes the `topic_sessions` row for that `(topic, agent)` pair — the same outcome scoped
to one topic.

## Consequences

- Good: `--resume` always succeeds within a session; no cwd mismatch
- Good: changing agent cwd is safe — sessions are auto-cleared, no stale resume attempts
- Bad: changing any key agent attribute (backend/model/cwd) clears all active sessions for that agent
- Bad: `cwd` must be stored in `topic_sessions` (not derivable from agent config alone)
