---
status: accepted
date: 2026-06-09
updated: 2026-06-10
---
# ADR-0018: CLI Subprocess Lifecycle, Queues, and Run Persistence

## Context and Problem Statement

Squid runs local agent CLIs and streams their output to the browser. The server
does not host the model runtime; Claude Code, Codex, Cursor Agent, Copilot CLI,
and Antigravity run as subprocesses on the user's machine.

Those CLIs can also create their own children while handling a turn:

- Claude Code can spawn sub-agents through its `Agent` tool.
- Codex can run shell/tool subprocesses.
- Cursor, Copilot, and Antigravity may do the same depending on their CLI
  implementation.

Squid needs three contracts to hold at the same time:

1. Process lifecycle: stop, restart, timeout, and cleanup should not leave
   backend-created children behind.
2. Queue lifecycle: topic-scoped session turns must remain FIFO, while adhoc
   turns run independently.
3. Persistence lifecycle: streamed text, tool events, stats, session ids, and
   final message state must survive refreshes and client disconnects.

## Decision Outcome

Squid separates those responsibilities across three layers:

| Layer | Owner | Responsibility |
|---|---|---|
| Runner | `agent/runners.py` | Launch one CLI subprocess, register it, stream stdout, drain stderr, and signal process groups for explicit stops. |
| Topic worker | `agent/topic_queue.py` | Own queue execution, consume runner chunks, persist run events/stats/session ids, update the assistant message, and forward chunks to the SSE queue. |
| Server stream | `agent/server.py` | Resolve agent/topic state, create chat rows, dispatch work, translate worker chunks to SSE, save pending partials, and drain after disconnect. |

The topic worker is the persistence authority for a completed turn. The server
stream is a transport surface and fallback partial saver.

## Process Launch Contract

All CLI backends go through `_stream_lines()` in `agent/runners.py`.

`_stream_lines()` starts each CLI with `start_new_session=True`, creating a new
session and process group for the top-level CLI:

```
squid server process group
    |
    `- cli parent process, new process group
          |
          `- cli-created children inherit the cli process group
```

Squid registers the CLI parent PID with:

- backend
- topic
- agent
- adhoc/session mode
- message id
- prompt preview
- start time

The registry is used for `/processes`, status UI, stop commands, and exact
message-level cancellation.

## I/O Contract

Squid only reads stdout from the top-level CLI process. It does not read child
process stdout directly.

```
Squid stdout pipe reader
    ^
    |
cli parent stdout
    ^
    |
cli child/sub-agent output captured and re-emitted by the parent CLI
```

Process groups do not alter the stdout/stderr pipe relationship. They only
control signal delivery.

`_stream_lines()` also drains stderr concurrently. This prevents a CLI that
writes a large amount of diagnostics from blocking on a full stderr pipe before
it can finish writing stdout.

## Queue and Worker Contract

`TopicDispatcher.dispatch()` creates a `QueueItem` containing:

- topic, agent, backend, model, cwd
- prompt and context history
- resume session id
- adhoc flag and lookback
- assistant message id

Session turns are queued by `topic@agent`. Adhoc turns use ephemeral worker keys
so they run in parallel and do not block the session lane.

`TopicWorker._process()` is responsible for consuming the runner stream. For
each chunk, it:

- appends text chunks to the accumulated response
- records status chunks
- records tool events
- enriches stats with `adhoc` and `lookback`
- saves session stats
- updates `topic_sessions` for resumable non-adhoc turns
- inserts ordered `run_events`
- forwards the chunk to the server stream queue

At the end of a turn, the worker updates the assistant message to `done` or
`error`, stores tool context, records a final `done` or `error` run event, and
always sends a `None` sentinel to close the stream queue.

## Run Event Contract

`run_events` stores an ordered replay log per assistant message:

| Field | Meaning |
|---|---|
| `msg_id` | Assistant message id. |
| `seq` | Monotonic event sequence within the run. |
| `event_type` | `text`, `tool`, `status`, `stats`, `error`, or `done`. |
| `payload` | Event payload as text or JSON, depending on event type. |

The unique key is `(msg_id, seq)`. Inserts use `INSERT OR IGNORE` so repeated
drain/replay attempts do not duplicate events.

## Server Streaming Contract

`stream_response()` dispatches work to the topic worker and then reads from the
worker's `out_q`.

The server stream:

- emits `meta`, `queued`, `status`, `tool`, `stats`, content chunks, `done`, and
  `error` SSE events
- saves pending partial content every few seconds while the client remains
  connected
- uses `only_if_pending=True` for final updates so it does not overwrite the
  worker's completed message
- starts `_drain_to_completion()` if the client disconnects before completion

The drain path keeps consuming the same worker queue for a bounded period and
saves a final pending-only update if the worker has not already completed the
message.

## Termination Contract

Explicit user-visible stop paths target registered process groups:

| Path | Behavior |
|---|---|
| `#topic /stop` | Terminate every matching running process group under the topic. |
| `#topic@agent /stop` | Terminate matching process groups for that agent lane. |
| `#topic@agent! /stop` | Terminate the most recent matching adhoc process group. |
| Thinking-bubble `x` | Terminate the exact process group registered for the message id. |
| `#topic /stopall` | Terminate matching running process groups and drain pending queue items for the topic. |
| Server restart | Terminate all registered process groups before exec/reload. |

The parent PID is stored in the registry. Stop paths call
`os.killpg(os.getpgid(pid), signal)`. If the process group cannot be found or
cannot be signaled, Squid falls back to signaling the parent PID.

Queued but not-yet-started items do not have subprocesses. Queue drain paths
cancel those items by emitting an error chunk and a `None` sentinel to their
waiting stream queue.

## Timeout and Cleanup Contract

`_stream_lines()` enforces:

- a first-byte timeout while waiting for initial CLI output
- a full response timeout for the overall run
- a 30 second post-EOF wait for the parent CLI to exit

Current implementation detail: read timeouts call `proc.kill()` on the parent
process. The post-EOF wait escalation signals the process group with `SIGKILL`
and falls back to `proc.kill()` if group signaling fails.

The 30 second post-EOF grace period matters for CLIs that finish writing stdout
before internal child cleanup has completed.

## Verification

Manual verification can watch PID, PPID, and PGID while a backend runs a task
that creates children:

```
watch -n1 "ps -eo pid,ppid,pgid,comm | grep -E 'claude|codex|cursor|agy|copilot|python' | grep -v grep"
```

Expected shape:

- the top-level CLI has `PGID` equal to its own PID
- CLI-created children share the top-level CLI's `PGID`
- Squid remains in its original process group

Automated coverage:

- `tests/test_runners.py` verifies topic stop, message-id stop, stopall, and
  parent-PID fallback behavior.
- `tests/test_topic_queue.py` verifies lookback propagation, worker stats
  persistence, and worker error/sentinel behavior.

## Consequences

- Good: backend-created child processes are stopped by explicit stop/restart
  paths when they remain in the CLI process group.
- Good: stderr draining prevents subprocess deadlock on large diagnostic output.
- Good: the worker owns final persistence, so refreshes and connected streams do
  not race to write final assistant state.
- Good: `run_events` provides an ordered per-message event log for future replay
  or recovery paths.
- Neutral: Squid still cannot read child-process output unless the parent CLI
  captures and re-emits it.
- Neutral: backends without stable stats still emit best-effort stats rows with
  zero or null values.
- Required: future runner code must preserve `_stream_lines()` as the shared
  process-launch path or explicitly implement the same registry and signal
  contract.
- Required: future persistence changes must keep the worker as the authority for
  completed-turn state, with server-side final writes guarded by pending-only
  updates.
