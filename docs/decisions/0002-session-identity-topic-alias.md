---
status: accepted
date: 2026-05-25
---
# ADR-0002: Session Identity is `(topic, alias)`

## Context and Problem Statement

With resumable sessions, a session ID must be stored and keyed to something stable. The question
is what constitutes the identity of a conversation lane — topic alone, topic + model name, or
topic + alias.

## Considered Options

1. Key by `(topic, model_name)` — direct model addressing, auto-detect backend
2. Key by `(topic, alias)` — named alias required, alias locks backend + model + cwd
3. Key by `(topic, backend, model)` — explicit triple

## Decision Outcome

**Option 2.** Session identity is `(topic, alias)`.

An alias locks three things together: `(backend, model, cwd)`. Two aliases with the same model
but different `cwd` are different agents — they operate in different project contexts and their
session files live in different locations on disk (see ADR-0003). Using the alias name as the
identity key makes this explicit.

Queue lanes in `TopicDispatcher` are also keyed by `(topic, alias)`, so different aliases on
the same topic run in parallel automatically.

## Consequences

- Good: identity is explicit and named; no invisible defaults
- Good: parallel lanes per alias fall out naturally from the queue key
- Bad: direct `@model-name` addressing without a named alias is not supported (see ADR-0004)
