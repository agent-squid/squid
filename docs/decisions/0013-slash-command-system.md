---
status: accepted
date: 2026-05-27
---
# ADR-0013: Slash Command System

## Context and Problem Statement

Squid's chat input serves both as a message composer and a control surface for
session management. Previously, commands (`stop`, `restart`, etc.) were bare
words matched by exact regex. Two problems emerged:

1. Adding `/clear` and `/compact` required handling backend differences —
   Claude supports `/clear` natively (emits a new session_id); Codex ignores
   it entirely (tokens keep growing, same thread_id). Users had no way to know
   which commands their backend supported.

2. There was no discoverability mechanism. Users had to read the help panel to
   know what commands existed.

## Decision Outcome

**Squid owns all session-management commands.** No slash command is passed
through to the underlying CLI. Squid intercepts and handles them uniformly
regardless of backend.

Commands are triggered by a `/` prefix in the chat input. Typing `/` opens an
autocomplete popup listing all commands with descriptions and a "squid" badge
indicating they are always handled by Squid (not the agent). Commands also
continue to work as bare words (exact whole-message match) for backward
compatibility.

### Command registry (`SQUID_COMMANDS` in `app.js`)

| Command        | Action                                           | Backend behavior       |
|----------------|--------------------------------------------------|------------------------|
| `/clear`       | kill procs, `clear_topic_session`, next msg fresh | uniform (Squid-owned)  |
| `/compact`     | same as clear (native compaction unsupported)    | uniform (Squid-owned)  |
| `/stop`        | SIGTERM running process for topic                | uniform                |
| `/stopall`     | SIGTERM + drain queue                            | uniform                |
| `/deq [N]`     | drain queue or remove Nth item                   | uniform                |
| `/restart`     | kill all procs + restart server                  | uniform                |
| `/filter`      | filter history by topic/alias                    | client-only            |
| `/filter reset`| clear filter                                     | client-only            |
| `/help`        | open help panel                                  | client-only            |

### `/clear` and `/compact` implementation

`POST /cmd { command: "clear", topic, alias? }`:
- Resolves alias from request or falls back to topic's sticky alias
- Calls `kill_procs_by_topic(topic)` — stops any in-flight CLI process
- Calls `clear_topic_session(topic, alias)` — wipes session_id and cwd lock
- Next message starts a fresh CLI invocation from `SQUID_HOME` (`/tmp/squid`)

`compact` is implemented identically to `clear`. True context compaction
(summarize history → inject as pin → fresh session) is deferred; native
compaction via CLI exec mode is not supported by any backend.

### Why not per-backend passthrough?

- **Codex**: `/clear` and `/compact` are interactive-only REPL commands. In
  `exec resume` mode they are treated as plain user messages — the model
  responds "Done." / "Compacted." but tokens keep growing and the thread_id
  never changes. Confirmed by testing (15k → 30k → 45k → 61k → 76k → 91k
  input tokens across turns, regardless of `/clear` or `/compact`).
- **Claude**: `/clear` via `--print --resume` does work (emits a new
  session_id), but Squid's session-clear achieves the same result and is
  consistent with all other backends.
- Owning the command layer means behavior is predictable regardless of which
  backend is active.

## Consequences

- Good: consistent `/clear` and `/compact` across all backends
- Good: `/` popup makes the command surface self-documenting
- Good: bare-word commands still work (backward compatible)
- Bad: Claude's native `/clear` (which preserves some context awareness of the
  clear event) is bypassed — Squid's clear is a hard session reset
- Bad: `/compact` is a reset, not a true compaction — noted in UI description
- Note: if a backend later supports compaction via non-interactive mode, the
  `compact` handler can be updated without changing the command surface
