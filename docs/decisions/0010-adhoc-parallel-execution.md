---
status: accepted
date: 2026-05-26
---
# ADR-0010: Adhoc (`!`) Messages Bypass the Topic Queue and Run in Parallel

## Context and Problem Statement

Session messages to the same `topic@alias` lane are serialized through a `TopicWorker` FIFO
queue. This is correct for session mode: each turn must complete before the next so the CLI
can resume the correct session state.

Adhoc (`!`) turns are stateless one-shots — they carry their own context snapshot and never
resume a session. Queuing them behind session turns (or behind each other) on the same
`topic@alias` key adds unnecessary latency with no correctness benefit.

## Considered Options

**A. Share the session queue** (`topic@alias` key)
Simple — no code change. Adhoc turns wait behind session turns and each other. Defeats the
purpose of firing a quick parallel lookup while a session is running.

**B. Separate queue per mode** (`topic@alias:adhoc` vs `topic@alias:session`)
Adhoc turns queue among themselves, session turns queue separately. Still serializes adhoc
messages to the same alias.

**C. Unique worker per adhoc message**
Each adhoc dispatch gets a key like `__adhoc_N` (monotonic counter), creating a fresh
ephemeral `TopicWorker`. The worker starts immediately — no queue, pure parallel execution.

## Decision Outcome

**Option C** — unique ephemeral worker per adhoc message.

`TopicDispatcher.dispatch()` accepts `adhoc: bool`. When `True`, a counter-keyed worker is
created (`__adhoc_N`) rather than reusing the `topic@alias` worker. The worker starts
immediately and the item runs without delay.

Ephemeral workers accumulate in `_workers` but remain idle after their single item completes.
For a local single-user tool the overhead is negligible.

## Consequences

- Good: adhoc turns are truly parallel — multiple `#topic@alias!` prompts run concurrently
- Good: a long-running session turn on `topic@alias` does not block adhoc queries to the same alias
- Good: no change to session queue behavior
- Neutral: idle `TopicWorker` tasks accumulate per session; acceptable for local use
- Neutral: `stop` / `stopall` commands target session workers; adhoc workers are ephemeral and not cancelable by topic key
