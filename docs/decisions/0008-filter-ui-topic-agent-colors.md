---
status: accepted
date: 2026-05-25
---
# ADR-0008: `#topic` and `@alias` as Separately Colored Clickable Filters

## Context and Problem Statement

Messages in history come from multiple `(topic, alias)` combinations running in parallel. Users
need to browse and filter this history by topic, by model, or both. The filter state should be
visible and directly tied to the input context.

## Considered Options

1. A separate filter sidebar with dropdowns for topic and alias
2. Clickable tags in message bubbles, with topic and alias as separate spans
3. A single combined `#topic@alias` tag per bubble, click-to-filter whole combination

## Decision Outcome

**Option 2.** In every message bubble, `#topic` and `@alias` are rendered as separately colored,
independently clickable spans:

- `#topic` in one color (e.g. blue) — clicking filters history to `topic = X`
- `@alias` in a second color (e.g. teal) — clicking adds `alias = Y` to the active filter

**Filter behavior is hierarchical:** clicking `#work` shows all aliases in that topic; clicking
`@claude-opus` within that view narrows to `topic = "work" AND alias = "claude-opus"`.

**Filter state = input chip state.** Clicking `#work` sets the input chip to `#work`; clicking
`@claude-opus` sets it to `#work@claude-opus`. The active filter and the active input context
are the same object — no separate filter UI is needed.

## Consequences

- Good: no additional filter UI to build; chip already exists
- Good: intuitive drill-down — topic first, then model within topic
- Good: clicking a response naturally "enters" that lane for the next message
- Bad: clicking `@alias` without a `#topic` context active needs a defined behavior
  (resolved as: clicking `@alias` alone sets `topic = <message's topic> AND alias = Y`)
