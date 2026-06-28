---
status: accepted
date: 2026-06-18
---
# ADR-0022: Multi-Protocol Agent Execution

## Context and Problem Statement

Squid currently drives coding-agent CLIs mostly through a one-shot CLI
protocol: each turn spawns a subprocess, reads and parses stdout, captures
stats and session IDs when available, then exits. This gives deterministic
turn boundaries and good observability, but it does not cover every useful CLI
shape.

Some agents expose their best behavior only in a long-lived interactive
session: warm context, native slash commands, live tool display, built-in
compaction, and optional raw terminal access. That interactive session might be
available as a structured stdin/stdout protocol, or it might only be available
through a terminal UI.

Squid therefore needs a vocabulary that separates the configured agent from the
communication protocol used to run it.

## Decision Outcome

Squid supports multiple driver protocols. A **protocol** defines process
lifetime, input shape, output shape, streaming behavior, turn boundary
detection, session/resume behavior, terminal attach support, and stats strategy.

Supported protocol names:

| Protocol | Lifetime | Transport | Boundary | Primary use |
|---|---|---|---|---|
| `oneshot-cli` | Per turn | CLI stdin/stdout/stderr | Process exit or structured done event | Current default for session and adhoc turns |
| `interactive-cli` | Long-lived | Structured CLI stdin/stdout/stderr | Structured done event | Preferred interactive protocol when a CLI exposes it |
| `interactive-pty` | Long-lived | Terminal bytes / ANSI | Terminal heuristic | Fallback for interactive CLIs that only expose terminal UI |

Drivers declare supported protocols. Backends may provide a default protocol,
and agents may override it when Squid exposes that setting. Protocol selection
is never inferred from model name.

## Selection Policy

Default selection order for normal session turns:

1. Agent protocol override, if present.
2. Backend default protocol, if present.
3. Driver default protocol.
4. `oneshot-cli` where available.

Preferred protocols by workload:

- **Sustained topic work**: `interactive-cli` first, `interactive-pty`
  second, `oneshot-cli` fallback.
- **Parallel adhoc work**: `oneshot-cli`.
- **Auditable automation**: `oneshot-cli`, because process exit and
  structured stats are deterministic.

Adhoc turns continue to prefer one-shot protocols because `#topic@agent!`
means parallel, outside the durable session queue, with explicit lookback
context. Interactive protocols may be used for adhoc only if a future design
gives each adhoc turn an isolated process/session.

## Protocol Responsibilities

Every driver protocol implementation must define:

- how the process starts, stops, and resumes
- how prompts and slash commands are sent
- how response text, status, tools, errors, stats, and done signals are emitted
- how a session ID is created or discovered
- whether idle-kill-resume is supported
- whether terminal attach is available
- how `/clear`, `/compact`, and backend-native usage commands are handled

The server still exposes one SSE surface to the browser. Protocol-specific
events are normalized to Squid's event stream: `meta`, text chunks, `status`,
`tool`, `stats`, `done`, and `error`.

## Interactive CLI

`interactive-cli` is the ideal protocol when a coding-agent CLI supports it:
Squid keeps a process alive across turns but exchanges structured events over
stdin/stdout or another non-terminal channel.

Benefits over PTY:

- exact or explicit turn boundary via structured `done`
- native structured tool, status, error, and stats events
- no ANSI parsing or virtual terminal reconstruction
- easier deterministic tests and replay
- warm sessions and native slash commands if the CLI protocol accepts them

Squid should prefer `interactive-cli` over PTY whenever a driver can
implement it without losing native interactive behavior.

## Interactive PTY

`interactive-pty` is accepted as a fallback protocol for CLIs whose real
interactive behavior only exists through a terminal. Squid spawns the CLI in a
direct PTY, not tmux, and parses the terminal output into chat events.

PTY lifecycle:

```
IDLE (no process)
  -> STARTING
  -> ACTIVE (turn running)
  -> WAITING (PTY alive, idle timer running)
  -> IDLE (idle-kill or clear)
```

State is tracked per `(topic, agent)` alongside `topic_sessions`. The PTY PID is
registered in the normal process registry so existing stop/timeout controls
still target the agent process group.

Turn boundary detection is heuristic and driver-specific. The default layered
strategy is:

1. terminal cursor-show or equivalent completion sequence
2. recognizable interactive prompt pattern
3. output quiescence after a short idle interval

Raw PTY output contains ANSI escape sequences, cursor movement, and formatting
codes. A PTY protocol implementation may use a virtual terminal emulator such
as `pyte` or a lighter ANSI-stripping path if the driver's output is simple
enough.

Idle-kill-resume remains part of the PTY design. When a PTY session is waiting
and exceeds its idle timeout, Squid kills the process group but keeps the
session ID in `topic_sessions`. The next message respawns the driver in the
locked cwd and resumes the native session.

## Stats and Usage

Stats are protocol-specific:

- `oneshot-cli`: use structured result events when the CLI emits them.
- `interactive-cli`: use structured stats or usage events when available.
- `interactive-pty`: send backend-native local commands such as `/cost` or
  `/usage`, run a sidecar local stats command, or omit stats if no reliable
  local source exists.

Drivers must not invent token or cost values. Missing stats are acceptable for
protocols that cannot expose them reliably.

## Comparison

| Property | `oneshot-cli` | `interactive-cli` | `interactive-pty` |
|---|---|---|---|
| Process per turn | Yes | No | No |
| Transport | CLI stdin/stdout/stderr | Structured CLI stdin/stdout/stderr | Terminal bytes / ANSI |
| Turn boundary | Process exit / done event | Structured done event | Heuristic |
| Stats/tool events | Native when CLI emits them | Native when CLI emits them | Sidecar or parsed command output |
| Slash commands | Synthetic prompt if supported | Native if protocol accepts commands | Native terminal input |
| Web terminal toggle | No | Usually no | Yes |
| Parallel adhoc fit | Good | Poor unless isolated | Poor unless isolated |
| Token cache warmth | Cold per process | Warm within process | Warm within process |
| Idle cleanup | Process exits every turn | Configured idle timeout, then resume | Configured idle timeout, then resume |
| Implementation risk | Low | Medium, CLI-dependent | High, due to terminal parsing |

## Consequences

- Good: Squid can support more than one execution protocol per driver/backend.
- Good: structured interactive CLI protocols are preferred when available, avoiding
  PTY parsing while keeping warm sessions.
- Good: one-shot CLI execution remains available for deterministic,
  parallel, and adhoc work.
- Good: PTY still covers CLIs whose real interactive behavior only exists in a
  terminal and enables a web terminal toggle.
- Good: interactive processes are treated as a warm cache; Squid can stop them
  after a backend-configured idle timeout while keeping the durable session ID
  for native resume.
- Neutral: token stats are protocol-specific; PTY may need command scraping or
  sidecars.
- Neutral: PTY turn boundary detection is heuristic.
- Bad: interactive protocols are sequential per `(topic, agent)` unless Squid
  deliberately starts isolated parallel sessions.
- Bad: ANSI parsing adds implementation complexity for `interactive-pty`.
