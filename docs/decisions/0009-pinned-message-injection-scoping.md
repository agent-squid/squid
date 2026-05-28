---
status: accepted
date: 2026-05-26
---
# ADR-0009: Pinned Message Injection Scoping by Mode

## Context and Problem Statement

Pinned messages are the curated context primitive: a user marks a turn as worth carrying
forward into other sessions or as a reference for ad-hoc queries. Three distinct dispatch
modes exist — resumed session, first session turn, and adhoc (`!`) — and each has different
needs for how pinned messages should enter context.

The key tension: a resumed session already owns its full history via `--resume`. Injecting a
pinned message that originated in that same session would duplicate it in the context window.
But for adhoc turns (one-shot, no `--resume`) and for cross-session injection, the pin is
the only mechanism to bring that context in.

## Considered Options

**A. Inject all pinned messages unconditionally in all modes**
Simple, but causes duplicate content for resumed sessions (same message appears twice).

**B. Inject pinned messages only from other sessions**
Exclude pins whose `session_id` matches the current `resume_session_id`. Deduplication is
handled at the source using the existing `session_id` column on `chat_messages`.

**C. Inject all pinned messages and rely on the model to de-duplicate**
No extra logic, but wasteful and potentially confusing for the model.

## Decision Outcome

**Option B** — scoped injection using existing `session_id`.

Each mode behaves as follows:

| Mode | Context source | Same-session pins |
|---|---|---|
| Resumed session | `--resume` + new pending injections | Excluded — already in `--resume` context |
| First session turn (no resume) | All pending pinned via `inject_history` | Included — no active session yet |
| Adhoc (`!`) | All pinned for topic via `context_history` | Included — adhoc has no session of its own |

Implementation: `get_pending_injections(topic, alias, exclude_session_id=resume_session_id)`.
The `exclude_session_id` filter uses the existing `chat_messages.session_id` column — no new
column or table needed.

Adhoc uses `get_context_history(topic, limit=0)` (no alias filter, no session exclusion),
meaning it can receive curated context from any alias or session in the topic. The `!` modifier
disables automatic history (N-turn lookback), not curated context.

If a user explicitly pins a message from the active session, it is silently skipped for that
session's own resumed turns — it is already present via `--resume`. The pin remains visible
to other sessions and adhoc turns in the same topic.

## Consequences

- Good: no duplicate content in resumed sessions
- Good: uses `chat_messages.session_id` already on every row — zero schema changes
- Good: adhoc gets the broadest curated context (any source in the topic)
- Neutral: same-session pins are "silently excluded" for the owning session, but the content
  is always in context via `--resume` — the effect is identical from the model's perspective
- Bad: slightly surprising if a user pins from the current session and expects a visible
  injection event; the pin takes effect only for other sessions and adhoc turns
