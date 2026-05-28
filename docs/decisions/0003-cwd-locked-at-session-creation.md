---
status: accepted
date: 2026-05-25
---
# ADR-0003: `cwd` Locked at Session Creation

## Context and Problem Statement

Claude CLI stores session files under `~/.claude/projects/<cwd-hash>/`. The hash is derived from
the working directory the CLI was launched from. If `--resume <session_id>` is called from a
different `cwd`, the CLI cannot find the session files and the resume fails.

This means the `cwd` used at session creation must be used for all subsequent messages in that
session. It cannot be changed mid-session.

## Considered Options

1. Lock `cwd` at session creation; store it alongside `session_id`
2. Always use a single fixed `cwd` (e.g. `SQUID_HOME`) for all sessions
3. Re-detect `cwd` from alias config on every message

## Decision Outcome

**Option 1.** The `cwd` is stored in `topic_sessions` at first message and used for all
subsequent `--resume` calls for that `(topic, alias)` session.

Changing an alias's `cwd` after a session has started has no effect on that session. To use a
different `cwd` on the same topic, create a new alias — this naturally produces a separate
`(topic, alias)` lane that runs in parallel.

Clearing a session (`DELETE /topics/{topic}/session?alias=X`) wipes both `session_id` and
stored `cwd`, allowing the next message to start fresh with the current alias config.

## Consequences

- Good: `--resume` always succeeds; no cwd mismatch
- Good: alias cwd changes are safe; they only affect new sessions
- Bad: users cannot change cwd for an existing session without resetting it
- Bad: `cwd` must be stored in a new `topic_sessions` table (not just in the alias config)
