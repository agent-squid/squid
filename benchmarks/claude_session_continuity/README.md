# Claude session-continuity benchmark

This manual benchmark compares two native Claude Code execution styles while
preserving one conversation across every prompt:

- `persistent`: one `claude` process receives all prompts over stream-json.
- `resumed`: one `claude` process per prompt; prompts after the first use
  `--resume` with the same session ID.

Both arms use the same installed binary, stream-json protocol, model, settings,
environment, prompt order, and a detached git worktree created from the same
baseline. Native Claude authentication variables are removed from the child
environment so Claude Code owns its OAuth credentials and refresh lifecycle.
The benchmark requires Sonnet and verifies the actual model reported by every
Claude process during initialization; it kills the process on any mismatch.
The measured value is the claude.ai five-hour quota gauge delta, not the token
counts reported in Claude's JSON output.

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
quota gauge until it is stable before and after execution. Each run receives a
unique timestamped directory under `results/`, which is ignored by git:

```text
results/run-<timestamp>/
  report.json
  state.json
  run.log
  persistent/benchmark_outputs/...
  resumed/benchmark_outputs/...
```

The report includes prompt outputs, raw stream events, quota snapshots, git
status, normalized per-prompt and per-arm model usage, Claude debug logs, and
the final binary diff, including untracked submission files. Persistent-process
usage counters are cumulative, so the report subtracts the prior snapshot;
resumed-process counters are summed because every prompt starts a fresh process.
Controller-owned correctness checks run after each arm so quality can be
compared independently of tests written by the model. The example uses seven
bounded Python tasks and writes each submission under a separate
`benchmark_outputs/` directory so implementations can be reviewed or scored
directly from either arm's saved files.

Run a second trial with `execution.order: [persistent, resumed]`. Do not use
`--skip-cooldown` for a real measurement; it exists only for debugging.

## Cache investigation notes

The resumed arm originally looked materially worse than the persistent arm in
short protocol captures. The important miss was not caused by `--resume`
itself, MCP tool schema drift, or copied transcript reloads. With this
controlled Claude Code setup:

```yaml
execution:
  extra_args:
    - --exclude-dynamic-system-prompt-sections
    - --strict-mcp-config
    - --mcp-config
    - '{"mcpServers":{}}'
```

the Claude init events reported `mcp_servers: []` and the same built-in tool
list for persistent and resumed processes. A raw mitm comparison of the
after-fix resumed prompt-2 request showed:

```text
tools equal: true
mcp tool names present: []
prior messages equal ignoring cache_control movement: true
prompt-1 status: Status: (clean)
prompt-2 status: Status: (clean)
```

The only byte-level system difference was Claude Code's billing header
`cch=...`; the cached system blocks were identical. The after-fix resumed arm
had no `messages_changed` or `system_changed` cache-miss diagnostic. The single
remaining miss diagnostic in that quick run was `unavailable` in the persistent
arm, not a resumed-session prefix break.

Before the fix, resumed prompt 2 regenerated the first user message with a
different git status:

```text
Status:
?? benchmark_outputs/
```

That changed the cached message prefix and Anthropic reported
`messages_changed`. The benchmark was measuring a dirtier resumed worktree
rather than an inherent resumed-session cache penalty.

The fix is to keep generated benchmark submissions out of Claude Code's dynamic
git-status reminder while still preserving them for scoring. Each temporary
benchmark worktree now adds `benchmark_outputs/` to its local `.git/info/exclude`.
`git_worktree_diff()` then explicitly includes files from that directory in the
report diff, because `git ls-files --others --exclude-standard` no longer sees
them.

The quick captured repro changed from:

```text
before fix:
  persistent prompt 2  cache_read 75889  cache_creation 1782
  resumed prompt 2     cache_read 70779  cache_creation 6699  messages_changed

after fix:
  persistent prompt 2  cache_read 75993  cache_creation 1786
  resumed prompt 2     cache_read 75748  cache_creation 1725
```

Conclusion: under the controlled no-dynamic-system/no-MCP setup, resumed is on
par with persistent at the request/cache level for this short repro. Any real
quota conclusion still needs a full two-order run with cooldowns, because the
claude.ai quota gauge is coarse for tiny samples.

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
  --status benchmarks/claude_session_continuity/results/run-<timestamp>
```

State is updated atomically during cooldown countdowns, gauge stabilization,
prompt transitions, completion, and failure. The detached process continues if
the launching terminal or Squid response ends.
