---
status: accepted
date: 2026-05-25
updated: 2026-05-28
---
# ADR-0001: Session Management — Resumable and Adhoc Modes

## Overview

Squid supports two session modes, selected per-message via input syntax:

- **Resumable** (`#topic@alias message`) — CLI owns the conversation state via
  `--resume <session_id>`. Native conversation format; context grows with every
  turn. Used for ongoing sessions where continuity matters.
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

The `cwd` is locked at session creation (see ADR-0003). Changing an agent's
`cwd` (or `backend`/`model`) via `POST /config/agents` forces an immediate
session reset — all `topic_sessions` rows for that agent are deleted and the
next message starts fresh with the new config.

Session clearing — via `/clear` command or `DELETE /topics/{topic}/session` —
wipes both `session_id` and `cwd`, allowing the next message to start fresh.
`/clear` also kills any in-flight CLI subprocess for the topic.

## How Adhoc Turns Work

Adhoc turns (`!`) bypass session resumption entirely. Each turn is independent:

- `_build_prompt` injects the last N non-adhoc session turns as a `<conversation_history>` block
- Bookmarked responses (`pinned_ids` from the client) are prepended to the context, deduplicated against the lookback window
- No `session_id` is stored or used

Adhoc turns run in parallel on the same topic without queuing constraints.

The UI shows which messages will be included before sending:
- Typing `!N` pre-highlights the last N messages in the bookmark panel and lights up their bookmark icons
- Persistent bookmarks (from clicking 🔖 on a response) are listed separately with status labels
- The `ctx:` label on sent messages shows both lookback count and bookmark count (e.g. `ctx: 3 backs · 2 bookmarked`); clicking it shows a detail popup

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

## Token Cost of Resumable Sessions

Resumable sessions are **not token-efficient for long conversations** — just
like a regular long session on a local CLI agent. The CLI re-sends the full
conversation history on every `--resume` call. Context grows unboundedly until
the user manually runs `/compact` or `/clear`.

Claude Code has a native `/compact` that summarises the conversation and
resets the context window, but it requires explicit user invocation. There is
no automatic compaction for resumed sessions.

**Open: Squid-side auto-compaction** — Squid should track turn count per
`(topic, agent)` session and automatically trigger compaction every N turns
(e.g. every 20 session turns). This would be Squid's own compaction policy,
independent of the CLI's native `/compact`. Design questions to resolve:

- What triggers the count: user messages, assistant messages, or pairs?
- Should N be configurable per agent?
- Compaction for Claude: issue `--resume SESSION_ID` with a compact prompt; for
  Codex the session must be reset since `exec resume` has no compact equivalent.
- Should the user be notified via a `status` event when auto-compaction fires?

Until auto-compaction is implemented, long resumed sessions accumulate context
silently and become increasingly expensive per turn.

## Consequences

- Good: CLI owns context natively; no synthetic history injection overhead
- Good: adhoc path gives precise control over injected context
- Good: both modes coexist on the same topic via `!` syntax
- Bad: context grows with every turn — no auto-compaction yet
- Bad: two code paths (`_build_prompt` + resume logic) to maintain
- Bad: Codex `exec resume` does not support `/clear` or `/compact` natively —
  session reset must be handled by Squid (see ADR-0013)
