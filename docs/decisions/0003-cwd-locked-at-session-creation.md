---
status: accepted
date: 2026-05-25
updated: 2026-07-15
---
# ADR-0003: `cwd` Locked at Session Creation

## Context and Problem Statement

Claude CLI stores session files under `~/.claude/projects/<cwd-hash>/`. The hash is derived from
the working directory the CLI was launched from. If `--resume <session_id>` is called from a
different `cwd`, the CLI cannot find the session files and the resume fails.

This means the `cwd` used at session creation must be used for all subsequent messages in that
session. It cannot be changed mid-session without starting a new session.

## Considered Options

1. Lock `cwd` at session creation; store it alongside `session_id` in `topic_sessions`
2. Always use a single fixed `cwd` (e.g. `SQUID_HOME`) for all sessions
3. Always use the agent config's current `cwd` on every message, ignoring any stored value — this would cause `--resume` to fail whenever the agent config's `cwd` had changed since the session was created

## Decision Outcome

**Option 1.** The `cwd` is stored in `topic_sessions` at first message and used for all
subsequent `--resume` calls for that `(topic, agent)` session.

On each message, Squid uses `stored["cwd"]` from `topic_sessions` — not the agent config's
current `cwd`. This ensures `--resume` always points to the same project directory the CLI
used when the session was created.

## Why `topic_sessions` exists as a separate table

Three tables each serve a distinct purpose:

| Table | What it stores | Scope |
|---|---|---|
| `agents` | Config: harness, provider, model, cwd, timeout | Per agent name (global) |
| `topics` | Autocomplete: sticky_agent, last_prompt, last_at | Per topic (display/nav) |
| `topic_sessions` | Runtime state: session_id + locked cwd | Per (topic, agent) pair |

`agents` cannot hold session state because the same agent can have active sessions on
multiple topics simultaneously — it has the wrong granularity.

`topics` drives autocomplete (`GET /topics` reads exclusively from the `topics` table) and
is never touched by session operations. Mixing runtime state into the autocomplete table
would couple two unrelated concerns.

`topic_sessions` is the right scope: one row per `(topic, agent)` pair, holding exactly
what `--resume` needs — the session ID and the cwd it was created with.

## Changing `cwd` mid-flight — DELETE not NULL

Changing an agent's `cwd` (or `harness`/`provider`/`model`) via `POST /config/agents` is detected as
a key attribute change. Squid immediately **deletes** all `topic_sessions` rows for that
agent (`clear_agent_sessions` → `DELETE FROM topic_sessions WHERE agent = ?`).

**Why DELETE rather than setting `session_id = NULL`:**
- `session_id` and `cwd` are both `NOT NULL` in the schema — a null session_id is not valid
- The stored `cwd` is no longer needed after a key change: the next message reads the new
  `cwd` directly from the `agents` table, creating a fresh row from scratch
- Keeping a nulled row would require special-casing in `get_topic_session` with no benefit
- DELETE is cleaner: the session is truly gone; the next message starts fresh

**Autocomplete is unaffected** by deleting from `topic_sessions` — it reads from `topics`,
not `topic_sessions`.

The next message finds no stored session, reads the new `cwd` from the `agents` table, and
creates a fresh `topic_sessions` row. This is equivalent to a forced `/clear` across all
topics that agent is active in.

Clearing a single session manually (`/clear` or `DELETE /topics/{topic}/session?agent=X`)
deletes the `topic_sessions` row for that `(topic, agent)` pair — same mechanism, one topic.

## Consequences

- Good: `--resume` always succeeds within a session; no cwd mismatch
- Good: changing agent cwd is safe — sessions are auto-cleared, no stale resume attempts
- Good: autocomplete (`topics` table) is completely independent of session state
- Bad: changing any key agent attribute (harness/provider/model/cwd) clears all active sessions for that agent
- Bad: `cwd` must be stored in `topic_sessions` separately from agent config
