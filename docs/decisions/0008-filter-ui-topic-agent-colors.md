---
status: accepted
date: 2026-05-25
---
# ADR-0008: `#topic` and `@agent` as Separately Colored Clickable Filters

## Context and Problem Statement

Messages in history come from multiple `(topic, agent)` combinations running in parallel. Users
need to browse and filter this history by topic, by model, or both. The filter state should be
visible and directly tied to the input context.

## Considered Options

1. A separate filter sidebar with dropdowns for topic and agent
2. Clickable tags in message bubbles, with topic and agent as separate spans
3. A single combined `#topic@agent` tag per bubble, click-to-filter whole combination

## Decision Outcome

**Option 2.** In every message bubble, `#topic` and `@agent` are rendered as separately colored,
independently clickable spans:

- `#topic` in one color (e.g. blue) — clicking filters history to `topic = X`
- `@agent` in a second color (e.g. teal) — clicking adds `agent = Y` to the active filter

**Filter behavior is hierarchical:** clicking `#work` shows all agentes in that topic; clicking
`@claude-opus` within that view narrows to `topic = "work" AND agent = "claude-opus"`.

**Filter state = input chip state.** Clicking `#work` sets the input chip to `#work`; clicking
`@claude-opus` sets it to `#work@claude-opus`. The active filter and the active input context
are the same object — no separate filter UI is needed.

**Slash command surface.** The same filter can be applied via commands typed into the input:

| Command | Behavior |
|---|---|
| `/filter` | Apply the current input's `#topic` / `@agent` as the history filter |
| `/filter reset` | Clear the active filter and reload full history |

`/filter` reads whatever is explicitly in the input — no agent auto-resolution. The `!`
adhoc flag is part of the filter tuple; the lookback count `N` in `!N` is ignored (only
`!` matters for filtering). This means:

| Input | History filter |
|---|---|
| `#work /filter` | `topic=work` — all agents, all turn types |
| `#work@claude-opus /filter` | `topic=work&agent=claude-opus&adhoc=false` — session turns only |
| `#work@claude-opus! /filter` | `topic=work&agent=claude-opus&adhoc=true` — adhoc turns only |
| `/filter reset` | no params — full history |

For sent messages the server resolves a null agent from the topic's sticky agent or the
default agent, and the chip updates post-response. But `/filter` is processed before any
server round-trip, so `#topic`-only remains topic-only.

**Contract tests**: `tests/e2e/filter.spec.js`

## History display: responses only

The history panel shows **only assistant responses** — user prompts are not rendered as
separate bubbles. `GET /history` (backed by `get_messages_flat`) filters to
`role = 'assistant'` before returning rows. Each assistant bubble includes a 55-character
prompt snippet in its header (from the `reply_to` join), giving enough context without
a separate user turn row.

**Rationale:** History is a cross-topic, cross-agent feed — not a single conversation
thread. User prompts interleaved from different sessions have no coherent order and would
add noise. The prompt snippet in the response header is sufficient to anchor each response
to its question. Showing full pairs only makes sense in a single-thread chat view.

**Live turns are unaffected.** Real-time messages (the current send) render both the user
bubble and the streaming assistant response as they arrive, since they come through the SSE
path, not `GET /history`. Only the scroll-back history feed is responses-only.

## Consequences

- Good: no additional filter UI to build; chip already exists
- Good: intuitive drill-down — topic first, then model within topic
- Good: clicking a response naturally "enters" that lane for the next message
- Good: keyboard-driven filtering via `/filter` for users who prefer not to click
- Good: history panel is a clean response feed — prompt snippets give enough context
- Bad: clicking `@agent` without a `#topic` context active needs a defined behavior
  (resolved as: clicking `@agent` alone sets `topic = <message's topic> AND agent = Y`)
