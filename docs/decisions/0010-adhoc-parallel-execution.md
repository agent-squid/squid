---
status: accepted
date: 2026-05-26
---
# ADR-0010: Adhoc (`!`) Messages Bypass the Topic Queue and Run in Parallel

## Context and Problem Statement

Session messages to the same `topic@agent` lane are serialized through a `TopicWorker` FIFO
queue. This is correct for session mode: each turn must complete before the next so the CLI
can resume the correct session state.

Adhoc (`!`) turns are stateless one-shots — they carry their own context snapshot and never
resume a session. Queuing them behind session turns (or behind each other) on the same
`topic@agent` key adds unnecessary latency with no correctness benefit.

## Considered Options

**A. Share the session queue** (`topic@agent` key)
Simple — no code change. Adhoc turns wait behind session turns and each other. Defeats the
purpose of firing a quick parallel lookup while a session is running.

**B. Separate queue per mode** (`topic@agent:adhoc` vs `topic@agent:session`)
Adhoc turns queue among themselves, session turns queue separately. Still serializes adhoc
messages to the same agent.

**C. Unique worker per adhoc message**
Each adhoc dispatch gets a key like `__adhoc_N` (monotonic counter), creating a fresh
ephemeral `TopicWorker`. The worker starts immediately — no queue, pure parallel execution.

## Decision Outcome

**Option C** — unique ephemeral worker per adhoc message.

`TopicDispatcher.dispatch()` accepts `adhoc: bool`. When `True`, a counter-keyed worker is
created (`__adhoc_N`) rather than reusing the `topic@agent` worker. The worker starts
immediately and the item runs without delay.

Ephemeral workers accumulate in `_workers` but remain idle after their single item completes.
For a local single-user tool the overhead is negligible.

## Stop scoping

`_proc_registry` stores `topic`, `agent`, `adhoc`, and `msg_id` per process.
`kill_procs_by_topic` accepts optional `agent` and `adhoc` filters. The client sends
`agent` and `adhoc` in the `/cmd` body, giving three scopes:

| Input | Kills |
|---|---|
| `#topic /stop` | All processes under topic — session and adhoc, all agents |
| `#topic@agent /stop` | Session processes for that agent only (`adhoc=false`) |
| `#topic@agent! /stop` | Adhoc processes for that agent only (`adhoc=true`) |

`stopall` follows the same scoping and also drains the session queue.

## Why not LIFO stop for adhoc

An alternative considered: `/stop` on `#topic@agent!` kills only the most-recently-started
adhoc process (LIFO), repeated presses walk back through older ones. Rejected because:

- **Blind** — the user has no visible ordering to reason about; which turn is "most recent"
  depends on timing, not on what's shown on screen.
- **Wrong surface** — the thinking bubble IS the visual handle for each running process.
  Click-to-kill on the bubble is zero cognitive overhead; LIFO requires mental bookkeeping.

`#topic@agent! /stop` therefore kills **all** in-flight adhoc processes for that agent at
once (nuclear for adhoc). Individual cancel is via the `×` button.

## Click-to-kill for individual adhoc processes

Each thinking bubble shows a `×` button once the `msg_id` arrives from the `meta` SSE
event. Clicking it:

1. Aborts the client-side SSE fetch (`controller.abort()`)
2. Posts `POST /cmd { command: "stop_msg", msg_id }` to the server
3. Server calls `kill_proc_by_msg_id(msg_id)` → SIGTERM on the exact process

This gives per-process precision for parallel adhoc turns without requiring the user to know
which agent or topic key to target.

**Contract tests**: `tests/e2e/stop.spec.js`

## Consequences

- Good: adhoc turns are truly parallel — multiple `#topic@agent!` prompts run concurrently
- Good: a long-running session turn on `topic@agent` does not block adhoc queries to the same agent
- Good: no change to session queue behavior
- Good: scoped stop — `#topic@agent /stop` kills session only, `#topic@agent! /stop` kills adhoc only
- Good: click-to-kill `×` on each thinking bubble for surgical per-process cancel
- Neutral: idle `TopicWorker` tasks accumulate per session; acceptable for local use
