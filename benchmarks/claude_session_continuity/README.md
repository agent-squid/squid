# Claude session-continuity benchmark

This manual benchmark compares two native Claude Code execution styles while
preserving one conversation across every prompt:

- `persistent`: one `claude` process receives all prompts over stream-json.
- `resumed`: one `claude` process per prompt; prompts after the first use
  `--resume` with the same session ID.

Both arms use the same installed binary, stream-json protocol, model, settings,
environment, prompt order, and a detached git worktree created from the same
baseline. The measured value is the claude.ai five-hour quota gauge delta, not
the token counts reported in Claude's JSON output.

## Prerequisites

- Authenticate the local `claude` CLI.
- Configure Claude gauge credentials in Squid (`~/.squid/squid-creds.json`).
- Ensure no other process or browser session uses the same Claude account while
  the benchmark is measuring.
- Do not begin close to the five-hour gauge reset time.

## Run

Copy and edit `prompts.example.yaml`, then run from the repository root:

```bash
.venv/bin/python -m benchmarks.claude_session_continuity.benchmark \
  --config benchmarks/claude_session_continuity/prompts.example.yaml
```

The default protocol waits ten idle minutes before each arm, then polls the
quota gauge until it is stable before and after execution. Results are written
under `results/`, which is ignored by git. The report includes prompt outputs,
raw stream events, quota snapshots, git status, and the final binary diff.

Run a second trial with `execution.order: [resumed, persistent]`. Do not use
`--skip-cooldown` for a real measurement; it exists only for debugging.

## Background runs and status

Long runs can detach from the terminal:

```bash
.venv/bin/python -m benchmarks.claude_session_continuity.benchmark \
  --config benchmarks/claude_session_continuity/prompts.example.yaml \
  --background
```

The command immediately prints the child PID plus the result, state, and log
paths. Poll the run using either the result path or the state path:

```bash
.venv/bin/python -m benchmarks.claude_session_continuity.benchmark \
  --status benchmarks/claude_session_continuity/results/run-123.json
```

State is updated atomically during cooldown countdowns, gauge stabilization,
prompt transitions, completion, and failure. The detached process continues if
the launching terminal or Squid response ends.
