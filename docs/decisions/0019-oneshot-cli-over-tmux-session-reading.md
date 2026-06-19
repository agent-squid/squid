---
status: accepted
date: 2026-06-10
---
# ADR-0019: One-Shot CLI Subprocess Over Tmux Session Reading

## Context and Problem Statement

Squid drives agent CLIs as subprocesses, parsing their structured output
event-by-event. An alternative considered was to wrap each CLI invocation inside
a tmux pane and read output via `capture-pane`, relying on the terminal as the
common interface instead of per-backend output parsers.

The tmux model is appealing on the surface:

- `capture-pane` works the same regardless of CLI — no per-backend parsing
- Reset and resume map naturally to pane lifecycle (`send-keys`, `kill-pane`)
- The view mirrors exactly what the user would see running the CLI themselves

## Decision Outcome

Squid continues to invoke agent CLIs as direct subprocesses and parse their
structured output. Tmux-based session reading is not adopted.

## Reasons

**Turn completion requires heuristics instead of a signal.**
A subprocess exits cleanly. Tmux `capture-pane` gives a snapshot of the
terminal; detecting end-of-turn requires pattern-matching the CLI's shell
prompt, polling for an idle cursor, or imposing an inactivity timeout. All
three are fragile — prompt format varies across shells and backends, idle
detection has race conditions, and timeout-based completion introduces
unnecessary latency.

**Structured output is lost.**
Every supported backend emits structured events over stdout (token counts,
costs, tool call records, session IDs, error codes). This data drives the stats
panel, token tracking (ADR-0017), tool event display, and session resumption
(ADR-0001). `capture-pane` returns ANSI escape sequences and painted terminal
cells — none of that structure survives.

**Process lifecycle control breaks.**
ADR-0018 establishes an explicit process-group contract: Squid registers the
CLI's PID and PGID on launch and signals the group for stop, timeout, and
restart. Tmux inserts its own session between Squid and the CLI process, making
direct signal delivery to the agent process group non-trivial and breaking
exact message-level cancellation.

**Parallelism requires N panes.**
Adhoc turns on the same topic run concurrently (ADR-0010). With tmux, each
concurrent turn would need its own pane, adding multiplexing overhead and a
pane-management layer with no corresponding benefit.

**Per-backend adapters already provide model-agnosticism.**
The argument for tmux is that it avoids per-CLI parsing work. In practice, each
backend emits a small, stable JSON schema. Thin adapters normalize to a shared
event stream (`text`, `tool`, `status`, `stats`, `done`, `error`). Adding a new
backend means writing one adapter, not learning tmux pane lifecycle. The tmux
approach replaces a small, self-contained parsing problem with a larger terminal
scraping problem.

## Tmux Remains Valid for Interactive Use

Nothing prevents a user from running a CLI inside tmux for their own
interactive session. Squid and tmux address different audiences: Squid drives
CLIs programmatically from a server process; tmux is for human-facing terminal
multiplexing. They are complementary, not competing.

## Alternative: Invisible PTY with Chat Surface (ADR-0022)

A third approach not considered in the original tmux analysis: spawn the CLI in
a **direct PTY** (no tmux intermediary), parse its ANSI output with a headless
VT100 emulator, and present the result as chat-like message bubbles. This
preserves process group control (no tmux between Squid and the CLI), enables a
web terminal toggle by connecting xterm.js to the same PTY, and supports
long-running sessions with idle-kill-resume lifecycle management.

This differs from the tmux model in three key ways:

- **Session ID**: extracted from Claude's on-disk JSONL project files after the
  first prompt (filename = session_id), not inferred from terminal content.
- **Process control**: Squid holds the PTY fd directly; `kill_procs_by_topic()`
  still works without routing through a tmux intermediary.
- **Turn boundary**: still heuristic (cursor-show sequence + prompt pattern +
  quiescence fallback), which was one of the reasons tmux was rejected. This is
  an acknowledged limitation in PTY mode.

The invisible PTY approach is suitable when users want both a chat UI and
occasional raw terminal access, and when long-running session warmth matters
more than exact structured event delivery. It is not a replacement for the
batch mode — both coexist. See ADR-0022 for full design.

## Consequences

- Good: turn completion, process lifecycle, and parallelism remain deterministic
- Good: structured output (stats, tool events, session IDs) is preserved end-to-end
- Good: per-backend adapters are small and independently testable
- Neutral: each new CLI backend requires a thin adapter
- Neutral: tmux session reading can be revisited if a backend stops emitting
  structured output and no other parsing path is feasible
