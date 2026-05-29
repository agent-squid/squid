---
status: superseded
date: 2026-05-25
superseded: 2026-05-29
---
# ADR-0007: Cross-session Context Injection

## Original Design (Superseded)

The original design used a server-side `session_context_log` table to track one-shot
cross-session injections. Pinning a message server-side queued it for injection into any
`(topic, agent)` session that hadn't absorbed it yet. `get_pending_injections` returned
outstanding messages on each dispatch, and `GET /context/{topic}?agent=X` exposed pending
vs absorbed state.

**This mechanism was removed.** `session_context_log` is explicitly dropped in `init_db()`
(`DROP TABLE IF EXISTS session_context_log`). The `pinned` column and `pin_count` were also
dropped from `chat_messages` and `session_stats` respectively. `get_pending_injections`
does not exist in the codebase.

## Current Design

Context injection is now **client-side** via the bookmark feature:

- Users click 🔖 on any assistant response to bookmark it. Bookmarks are stored in
  `localStorage` as `{ id, topic, agent, content }` and persist across page reloads.
- The client sends `pinned_ids: [id, ...]` in `POST /chat` for **both session and adhoc turns**,
  filtered to exclude bookmarks from the **same session_id** (not merely same `topic@agent` —
  the same topic can run different sessions, and a bookmark from a prior session under the
  same `topic@agent` is still cross-session context worth injecting).
  The server fetches those rows by ID from `chat_messages` via `get_messages_by_ids()`.
  - **Adhoc turns**: pinned content is prepended to `context_history` for `_build_prompt`,
    deduplicated against the lookback window.
  - **Session turns**: pinned content is prepended to the prompt as a
    `<referenced_context>` block (`effective_message`), giving the CLI the supplementary
    context that `--resume` does not provide.
- The client tracks which IDs have already been injected per `(topic, agent)` in an
  `injectedInto` localStorage map, preventing re-injection on subsequent turns.
- The UI shows injection status in the bookmark panel: `will inject`, `in session · skip`,
  or `already added · skip`.

**Key differences from the original design:**

| | Original | Current |
|---|---|---|
| Storage | Server DB (`session_context_log`) | Client `localStorage` |
| Scope | Session turns + adhoc | Session turns + adhoc |
| Tracking | Per-session DB rows | Per-`(topic, agent)` localStorage map |
| Trigger | Automatic on each dispatch | Explicit client-side selection |

`GET /context/{topic}` still exists but now returns the same data as
`GET /topics/{topic}/session` — just the active `session_id` and `cwd`. It no longer
shows pending/absorbed injection state.

**Contract tests**: `tests/e2e/pin.spec.js`

## Why the Server-side Approach Was Removed

The `session_context_log` table added schema complexity and required cleanup logic for
inactive sessions. The client-side approach is simpler: the user explicitly selects what
to inject, the tracking lives in localStorage where it belongs (it is UI state, not
conversation state), and there is no server cleanup burden.

The trade-off: bookmarked context is only available on the device where it was bookmarked.
Cross-device sync would require re-introducing server-side storage.

## Ephemerality of Pinned Context

Pinned context is intentionally ephemeral, and the design accepts this for two reasons:

**Session turns — injection is one-shot by nature.** Once a bookmark is injected into a
resumable session, that content becomes part of the session history and is carried forward
by `--resume` on every subsequent turn. The `injectedInto` localStorage map records this
so the client never sends the same `pinned_id` to the same `(topic, agent)` twice. After
the first injection the bookmark has done its job; re-injecting it would be redundant noise.

**Adhoc turns — cross-device sync has no practical payoff.** Adhoc turns are stateless by
design: each one runs independently with no persistent session. A bookmark injected into an
adhoc turn disappears with that turn's context window; there is no accumulated history for
a second device to build on. Syncing bookmarks across devices would cost server-side
storage and a sync protocol for a scenario where the value gained is marginal.

The localStorage-only approach therefore matches the actual lifecycle: bookmarks are
UI-state that bridges a gap until the first injection, then become inert (for session turns)
or remain available for repeated one-off enrichment on the same device (for adhoc turns).
