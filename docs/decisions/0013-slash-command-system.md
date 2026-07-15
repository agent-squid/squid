---
status: accepted
date: 2026-05-27
updated: 2026-07-15
---
# ADR-0013: Slash Command System

> **Update (2026-07-15):** `/compact` — documented below as a live command at
> the time this ADR was written — has since been removed. It was implemented
> identically to `/clear` from day one (see "Why not per-harness passthrough"),
> and was dropped as a redundant alias rather than ever gaining true
> compaction behavior. The rest of this ADR's decision (Squid owns all
> session-management commands uniformly, regardless of harness) still holds.
> Terminology updated from "backend" to "harness" per
> [ADR-0028](0028-harness-provider-separation.md).

## Context and Problem Statement

Squid's chat input serves both as a message composer and a control surface for
session management. Previously, commands (`stop`, `restart`, etc.) were bare
words matched by exact regex. Two problems emerged:

1. Adding `/clear` (and, originally, `/compact`) required handling harness
   differences — Claude Code supports `/clear` natively (emits a new
   session_id); Codex ignores it entirely (tokens keep growing, same
   thread_id). Users had no way to know which commands their harness
   supported.

2. There was no discoverability mechanism. Users had to read the help panel to
   know what commands existed.

## Decision Outcome

**Squid owns all session-management commands.** No slash command is passed
through to the underlying CLI. Squid intercepts and handles them uniformly
regardless of harness.

Commands are triggered by a `/` prefix in the chat input. Typing `/` opens an
autocomplete popup listing all commands with descriptions and a "squid" badge
indicating they are always handled by Squid (not the agent). Commands also
continue to work as bare words (exact whole-message match) for backward
compatibility.

### Command registry (`SQUID_COMMANDS` in `app.js`)

| Command             | Action                                            | Harness behavior       |
|----------------------|---------------------------------------------------|------------------------|
| `/clear`             | kill procs, `clear_topic_session`, next msg fresh | uniform (Squid-owned)  |
| `/stop`              | SIGTERM running process for topic                 | uniform                |
| `/stopall`           | SIGTERM + drain queue                             | uniform                |
| `/deq [N]`           | drain queue or remove Nth item                    | uniform                |
| `/restart`           | kill all procs + restart server                   | uniform                |
| `/refresh`           | hard-refresh the browser tab; server untouched     | client-only            |
| `/f`, `/filter`      | filter history by topic/agent; `/f reset` clears   | client-only            |
| `/s`, `/search`      | keyword search history                            | client-only            |
| `/bookmarks`, `/bm`  | toggle bookmarked-responses-only view              | client-only            |
| `/prompts`           | toggle user-prompts-only view                      | client-only            |
| `/status`            | show active processes panel                        | client-only            |
| `/help`              | open help panel                                     | client-only            |
| `/remote`            | show QR code for mobile/tablet access               | client-only            |

`/deq`, `/stop`, `/stopall`, `/restart`, and `/clear` are also exposed
server-side via `POST /cmd`; the rest are handled entirely client-side.

### `/clear` implementation

`POST /cmd { command: "clear", topic, agent? }`:
- Resolves agent from request or falls back to topic's sticky agent
- Calls `kill_procs_by_topic(topic)` — stops any in-flight CLI process
- Calls `clear_topic_session(topic, agent)` — wipes session_id and cwd lock
- Next message starts a fresh CLI invocation from `SQUID_HOME` (`/tmp/<user>/squid`)

### Why not per-harness passthrough?

- **Codex**: `/clear` is an interactive-only REPL command. In `exec resume`
  mode it's treated as a plain user message — the model responds "Done." but
  tokens keep growing and the thread_id never changes. Confirmed by testing
  (15k → 30k → 45k → 61k → 76k → 91k input tokens across turns, regardless of
  sending `/clear`).
- **Claude Code**: `/clear` via `--print --resume` does work (emits a new
  session_id), but Squid's session-clear achieves the same result and is
  consistent with all other harnesses.
- Owning the command layer means behavior is predictable regardless of which
  harness is active.

## Consequences

- Good: consistent `/clear` across all harnesses
- Good: `/` popup makes the command surface self-documenting
- Good: bare-word commands still work (backward compatible)
- Bad: Claude Code's native `/clear` (which preserves some context awareness
  of the clear event) is bypassed — Squid's clear is a hard session reset
- Note: `/compact` was removed (see update note above) rather than evolved
  into true compaction; see ADR-0001's "Token Cost of Resumable Sessions" for
  the still-open auto-compaction question.
