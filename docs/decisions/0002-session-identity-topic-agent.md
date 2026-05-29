---
status: accepted
date: 2026-05-25
---
# ADR-0002: Session Identity is `(topic, agent)`

## Context and Problem Statement

With resumable sessions, a session ID must be stored and keyed to something stable. The question
is what constitutes the identity of a conversation lane — topic alone, topic + model name, or
topic + agent.

## Considered Options

1. Key by `(topic, model_name)` — direct model addressing, auto-detect backend
2. Key by `(topic, agent)` — named agent required, agent locks backend + model + cwd
3. Key by `(topic, backend, model)` — explicit triple

## Decision Outcome

**Option 2.** Session identity is `(topic, agent)`.

An agent locks three things together: `(backend, model, cwd)`. Two agentes with the same model
but different `cwd` are different agents — they operate in different project contexts and their
session files live in different locations on disk (see ADR-0003). Using the agent name as the
identity key makes this explicit.

Queue lanes in `TopicDispatcher` are also keyed by `(topic, agent)`, so different agentes on
the same topic run in parallel automatically.

## Consequences

- Good: identity is explicit and named; no invisible defaults
- Good: parallel lanes per agent fall out naturally from the queue key
- Bad: direct `@model-name` addressing without a named agent is not supported (see ADR-0004)
