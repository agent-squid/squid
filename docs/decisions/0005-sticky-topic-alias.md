---
status: accepted
date: 2026-05-25
---
# ADR-0005: `#topic` Uses Last-used Alias (Sticky)

## Context and Problem Statement

Users will frequently send many consecutive messages to the same `(topic, alias)` lane. Requiring
them to type `#topic@alias` on every message is repetitive. The system needs a way to remember
which alias is active for a topic.

## Considered Options

1. No sticky — always require `#topic@alias`
2. Sticky: remember last-used alias per topic; `#topic` alone resolves to it
3. Sticky with deferred update: update sticky only after first successful response

## Decision Outcome

**Option 2.** The `topics` table already has an `alias` column. When a user sends
`#topic@alias`, `upsert_topic(topic, alias)` updates the sticky immediately — before the
response arrives — as long as the alias exists (validated in ADR-0004).

Immediate update is safe because alias existence is validated before any state changes. If the
alias doesn't exist, the request is rejected and the sticky is not changed.

A new topic with no sticky yet shows the chip in a "needs alias" state. The user must send at
least one `#topic@alias` message to establish the sticky.

## Consequences

- Good: no need to retype `@alias` on every message in a continuous session
- Good: switching to `#topic@other-alias` visually and immediately confirms the context switch
- Bad: accidental `#topic@wrong-alias` updates the sticky; user must retype to fix it
- Mitigation: the persistent input chip (ADR-0008) makes the active alias visible at all times
