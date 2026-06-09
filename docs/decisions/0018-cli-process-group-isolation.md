---
status: accepted
date: 2026-06-09
---
# ADR-0018: CLI Process Group Isolation and Termination

## Context and Problem Statement

Squid runs agent backends as local CLI subprocesses and streams their stdout
back to the browser. Some CLIs can create child processes while handling a
turn:

- Claude Code can spawn sub-agents through its `Agent` tool.
- Codex can run command/tool subprocesses and may create additional child
  processes depending on the task and CLI implementation.
- Other backends may do the same.

The parent CLI is the only process Squid reads from directly. Child process
output is only visible to Squid when the parent CLI captures it and emits it on
the parent's stdout stream. Process groups do not change this I/O path; they
only define which processes receive signals.

The failure mode is premature or partial termination: a stop, timeout, terminal
hangup, or server shutdown that targets only the parent PID can leave child
processes behind or interrupt a CLI while it is still waiting for a child to
finish.

## Decision Outcome

Every CLI backend launched through `_stream_lines()` is started with
`start_new_session=True`.

This creates a new session and process group for the top-level CLI process:

```
squid server process group
    |
    `- cli parent process, new process group
          |
          `- cli-created children inherit the cli process group
```

Squid treats the CLI process group as the lifecycle unit. Stop and cleanup paths
send signals to the process group with `os.killpg(os.getpgid(pid), signal)`.
If the group no longer exists or cannot be signaled, Squid falls back to
signaling the parent PID.

This applies to Claude, Codex, Cursor, Antigravity, and Copilot because they all
share `_stream_lines()`. Claude's sub-agent behavior is the easiest case to
observe, but the rule is backend-agnostic.

## I/O Contract

Squid only reads the stdout pipe of the top-level CLI process. It does not read
from child processes directly.

```
Squid stdout pipe reader
    ^
    |
cli parent stdout
    ^
    |
cli child/sub-agent output captured and re-emitted by the parent CLI
```

Moving the CLI into a separate process group does not sever stdout/stderr pipes.
Pipes are file descriptors; process groups affect signal delivery, not data
flow.

## Termination Contract

All user-visible stop paths must target the process group:

| Path | Behavior |
|---|---|
| `#topic /stop` | Terminate every matching CLI process group under the topic. |
| `#topic@agent /stop` | Terminate matching session process groups for the agent. |
| `#topic@agent! /stop` | Terminate the most recent matching adhoc process group. |
| Thinking-bubble `x` | Terminate the exact message's process group by `msg_id`. |
| `stopall` | Terminate all registered CLI process groups. |
| Response timeout cleanup | Escalate to `SIGKILL` for the CLI process group after the grace period. |

The process registry stores the parent PID, topic, agent, mode, and message id.
The parent PID is used to discover the process group id at termination time.

## Wait Timeout

After stdout reaches EOF, Squid waits up to 30 seconds for the parent CLI to
exit before escalating to `SIGKILL` for the process group.

This grace period matters for CLIs that finish streaming output before all
internal child-process cleanup has completed. A shorter timeout can cut off
sub-agents or tool subprocesses mid-turn.

## Verification

Manual verification can watch PID, PPID, and PGID while a backend runs a task
that creates children:

```
watch -n1 "ps -eo pid,ppid,pgid,comm | grep -E 'claude|codex|python' | grep -v grep"
```

Expected shape:

- The top-level CLI has `PGID` equal to its own PID.
- CLI-created children have the same `PGID` as the top-level CLI.
- Squid remains in its original process group.

Automated coverage:

- `tests/test_runners.py` verifies topic stop, `msg_id` stop, and `stopall`
  signal process groups.
- The same tests verify fallback to parent-PID signaling when a process group
  cannot be found.

## Consequences

- Good: backend-created child processes are stopped with the parent CLI.
- Good: signals aimed at Squid's process group do not accidentally reach active
  CLI turns.
- Good: stdout streaming remains unchanged; Squid still consumes only the
  parent CLI stream.
- Neutral: Squid cannot directly recover output from children unless the parent
  CLI captures and emits that output.
- Required: future runner code must preserve `_stream_lines()` as the shared
  process-launch path or explicitly implement the same process-group contract.
