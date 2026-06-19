---
status: proposed
date: 2026-06-18
---
# ADR-0022: Invisible PTY with Chat Surface and Idle-Kill-Resume

## Context and Problem Statement

Squid currently drives agent CLIs in one-shot batch mode: each turn spawns a
fresh subprocess (`claude --print --output-format stream-json --resume <id>`),
streams structured JSON, and exits cleanly. Session continuity comes from
storing the session_id and passing `--resume` on the next turn (ADR-0001).

This model works well for discrete turns but cannot support long-running
interactive sessions where the user expects:

- Responses that stream continuously across multiple exchanges without
  re-incurring cold-start overhead
- Native Claude Code features that only activate in interactive mode (real-time
  tool call display, `/compact` invoked mid-session, compaction-on-context-limit)
- A web terminal escape hatch that mirrors the live PTY if the user wants raw
  access
- Chat-like message bubbles in the UI (not raw ANSI noise) without losing the
  terminal option

The challenge is that interactive PTY sessions are long-running processes that
hold memory and a file descriptor while idle. Keeping them alive indefinitely
wastes resources. Killing them naively loses the session. The solution is
**idle-kill-resume**: kill the PTY after a configurable idle period, persist the
session_id, and spawn a new PTY with `--resume` when the user next sends a
message.

## Decision Outcome

Introduce an invisible PTY execution mode alongside the existing batch mode.
When a topic's agent is configured with `mode: pty` (or a future UI toggle),
Squid manages a long-running interactive PTY subprocess, parses its output as
chat-like messages, and applies idle-kill-resume lifecycle management. The
existing batch mode (`mode: oneshot`, current default) is unchanged.

## Architecture

### PTY Lifecycle States

```
IDLE (no process)
  │  user sends message
  ▼
STARTING
  │  process spawned; session_id extracted (see below)
  ▼
ACTIVE  ◄──── user sends message ────┐
  │  turn boundary detected           │
  ▼                                   │
WAITING (PTY alive, idle timer running)
  │  idle timeout OR /clear
  ▼
IDLE
```

State is tracked per `(topic, agent)` alongside the existing `topic_sessions`
record. The PTY process PID is registered in `_proc_registry` as today;
deregistration on exit transitions the state to IDLE automatically.

### Session ID Extraction

Interactive PTY mode does not emit structured JSON, so the current approach of
reading `system.session_id` from the first JSON event does not apply.

Instead, Squid extracts the session_id from Claude's on-disk project files
after the first prompt is sent:

1. Compute the project directory: `~/.claude/projects/<cwd-hash>/` where
   `<cwd-hash>` is the absolute `cwd` with every `/` replaced by `-`
   (e.g., `/Users/haebin/Work/squid` → `-Users-haebin-Work-squid`).
2. After sending the first prompt, poll the project directory for the most
   recently modified `.jsonl` file (modified within the last N seconds,
   default 30s).
3. Read the first line of the candidate file; it contains
   `{"type":"mode","sessionId":"<id>"}`.
4. Optionally verify: scan the file for the first `"role":"user"` entry and
   confirm it contains the prompt text sent.
5. If matched: the file stem (filename without `.jsonl`) is the session_id.
   Store it in `topic_sessions` exactly as the batch mode does.

This approach requires no probe prompt and no modification to the CLI invocation.
It relies on Claude CLI's stable on-disk format: the `.jsonl` filename equals
the session_id and the first line always contains `{"type":"mode","sessionId":"..."}`.

**Fallback**: if no matching file is found within the polling window, log a
warning and continue without a session_id. The next user message will re-attempt
extraction. Resumption is unavailable until a session_id is captured.

### Text Extraction

Raw PTY output contains ANSI escape sequences, cursor movement, and formatting
codes. Squid feeds the byte stream through a headless VT100 emulator (`pyte`)
to produce a clean virtual screen, then extracts text by querying the screen
buffer when a turn boundary is detected.

Alternatively, a lighter approach strips ANSI codes with a regex
(`re.sub(r'\x1b\[[0-9;]*[mABCDEFGHJKLMPSTf]', '', raw)`) and post-processes
the result. The pyte path is more correct for complex output; the regex path is
simpler and sufficient for most Claude Code responses.

Output is buffered per turn and emitted as a single `text` event (for the chat
surface) when the turn ends. Streaming partial content is possible by emitting
incremental `text_delta` events as lines arrive.

### Turn Boundary Detection

Detecting when Claude has finished a response is the primary heuristic challenge
of PTY mode. Squid uses a layered strategy:

1. **Cursor-show sequence**: Claude Code emits `ESC[?25h` (show cursor) after
   completing a response. This is the most reliable signal when present.
2. **Prompt pattern**: The interactive shell prompt (`>` or `❯` at the start of
   a line after a blank line) indicates Claude is waiting for input.
3. **Output quiescence**: No new bytes for 600ms after at least one byte of
   output implies the response has settled. Used as a final fallback.

The three checks run in priority order; the first to fire wins. A `/clear`
command issued by the user in the PTY resets the boundary detector.

### Idle Detection and Kill

Squid tracks two timestamps per PTY session:

- `last_input_at`: when the user last sent a prompt via the Squid UI
- `last_output_at`: when the PTY last emitted bytes

When `now - last_input_at > IDLE_TIMEOUT` and the session is in WAITING state
(no active turn), Squid kills the PTY:

1. Send SIGTERM to the process group (existing `kill_procs_by_topic()`)
2. Wait up to 5s for clean exit; escalate to SIGKILL if needed
3. Transition state to IDLE; session_id remains in `topic_sessions`

`IDLE_TIMEOUT` defaults to 3600s (1 hour). It is configurable per agent in
`squid.yaml` under `pty_idle_timeout_sec`.

No kill is issued while a turn is ACTIVE, even if `last_input_at` exceeds the
threshold.

### Resume on Next Message

When a user sends a message and the session is IDLE:

1. Look up `session_id` and locked `cwd` from `topic_sessions` (same as batch
   mode resumption).
2. Spawn interactive PTY: `claude --resume <session_id>` in the locked `cwd`.
3. Claude reloads the full conversation from `~/.claude/projects/<cwd-hash>/`.
4. Send the user's prompt once the PTY is ready (detected by the prompt pattern
   or a brief startup delay).
5. Transition to ACTIVE.

The user experience is seamless: the conversation continues as if the PTY had
never been killed. Claude's context, tool state, and compaction history all
survive because they live in Claude's on-disk session files, not in the PTY
process.

### Web Terminal Toggle

The PTY file descriptor is connected to both the chat parser and a hidden
xterm.js WebSocket channel. A "View Terminal" button in the UI shows the xterm.js
pane, giving the user a live view of the raw terminal exactly as it exists.

When the session is IDLE, the terminal pane shows a placeholder ("Session
paused. Send a message to resume."). No reconnection is needed because the PTY
is respawned on the next user message, and xterm.js re-attaches at that point.

### Compaction

Native Claude Code `/compact` works unchanged in PTY mode: the user (or Squid
auto-compaction, once implemented per ADR-0001) sends `/compact` as a prompt,
Claude summarises and resets its context window, and the conversation continues
in the same session. The session_id does not change after compaction.

This is an advantage over batch mode, where `/compact` must be issued as a
synthetic prompt and the structured output verified. In PTY mode, `/compact`
is typed naturally into the interactive session.

### `/clear` in PTY Mode

`/clear` kills the active PTY and wipes `topic_sessions` for the
`(topic, agent)` pair, identical to batch mode behaviour. The next message
starts a fresh PTY with no `--resume`. The session_id extracted from the new
session replaces the old record.

## Comparison with Batch Mode

| Property | Batch (current) | PTY (this ADR) |
|----------|----------------|----------------|
| Process per turn | Yes (fresh subprocess) | No (shared PTY) |
| Session ID source | First JSON event | JSONL filesystem scan |
| Structured events (stats, tool) | Yes | No — requires separate scraping |
| Turn boundary | Process exit (exact) | Heuristic (layered) |
| Idle resource usage | Zero (no process) | Zero (killed on idle) |
| `/compact` | Synthetic prompt | Native interactive |
| Web terminal toggle | No (no live PTY) | Yes |
| Parallelism | Full (adhoc ADR-0010) | PTY mode is sequential per topic |
| Token cache warmth | Cold per turn | Warm within session |

Stats (token counts, cost) are not available from raw PTY output. A companion
approach — running a lightweight one-shot turn to fetch stats after each PTY
turn — is deferred. Alternatively, the stats panel can be omitted in PTY mode
or populated from Claude's own `/cost` command output.

## Consequences

- Good: long-running sessions stay warm between turns; no per-turn cold-start
- Good: native `/compact`, `/clear`, and other Claude Code slash commands work
  as-is
- Good: web terminal toggle is a natural byproduct of the PTY approach
- Good: idle-kill-resume reclaims resources without losing session state
- Good: session_id extraction requires no probe prompt or CLI modification
- Neutral: token stats unavailable in PTY mode until a stats-scraping companion
  is added
- Neutral: turn boundary detection is heuristic (three-layer fallback reduces
  false positives)
- Bad: PTY mode is sequential per topic; parallel adhoc turns (ADR-0010) are
  not supported in this mode
- Bad: ANSI parsing adds implementation complexity not present in batch mode
