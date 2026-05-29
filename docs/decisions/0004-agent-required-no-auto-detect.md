---
status: accepted
date: 2026-05-25
updated: 2026-05-28
---
# ADR-0004: Agent Must Exist — No Auto-detect Fallback

## Context and Problem Statement

When a user types `#topic@name`, `name` must resolve to a known agent. The question is what to
do when it doesn't — silently auto-detect the backend from the model name string, or reject and
prompt the user to create an agent.

## Considered Options

1. Auto-detect backend from model name (e.g. `@claude-opus-4-7` → claude backend, SQUID_HOME cwd)
2. Reject with a prompt to create the agent first
3. Auto-create a minimal agent with inferred defaults

## Decision Outcome

**Option 2.** If `@name` does not match a known agent, the request is rejected. The UI prompts
the user to create the agent with the name pre-filled, requiring explicit backend, model, and cwd.

Auto-detection undermines ADR-0002 and ADR-0003: it would create an invisible implicit agent
with an implicit `cwd`, hiding the identity and cwd that get locked at session creation. Model
name → backend mapping is also fragile (ambiguous names, future model releases).

## Consequences

- Good: session identity and cwd are always explicit and user-confirmed before a session starts
- Good: consistent with the agent identity model
- Bad: one extra step for casual/quick use (type agent name, fill in form, then message)
- Mitigation: the agent creation form is inline and pre-filled from the `@name` the user typed
