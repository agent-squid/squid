---
status: accepted
date: 2026-05-25
updated: 2026-05-29
---
# ADR-0005: `#topic` Uses Last-used Agent (Sticky)

## Context and Problem Statement

Users will frequently send many consecutive messages to the same `(topic, agent)` lane. Requiring
them to type `#topic@agent` on every message is repetitive. The system needs a way to remember
which agent is active for a topic.

## Considered Options

1. No sticky — always require `#topic@agent`
2. Sticky: remember last-used agent per topic; `#topic` alone resolves to it
3. Sticky with deferred update: update sticky only after first successful response

## Decision Outcome

**Option 2.** The `topics` table has a `sticky_agent` column. When a user sends
`#topic@agent`, `upsert_topic(topic, agent)` updates the sticky immediately — before the
response arrives — as long as the agent exists (validated in ADR-0004).

Immediate update is safe because agent existence is validated before any state changes. If the
agent doesn't exist, the request is rejected and the sticky is not changed.

A new topic with no sticky yet shows the chip in a "needs agent" state. The user must send at
least one `#topic@agent` message to establish the sticky.

## Consequences

- Good: no need to retype `@agent` on every message in a continuous session
- Good: switching to `#topic@other-agent` visually and immediately confirms the context switch
- Bad: accidental `#topic@wrong-agent` updates the sticky; user must retype to fix it
- Mitigation: the persistent input chip (ADR-0008) makes the active agent visible at all times
