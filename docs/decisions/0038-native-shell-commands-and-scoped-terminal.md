---
status: accepted
date: 2026-08-10
updated: 2026-08-12
---
# ADR-0038: Native Shell Commands with a Scoped Terminal Escape Hatch

## Context and Problem Statement

Squid users sometimes need to run a local command and receive its exact output
in chat without opening a separate terminal. Examples include inspecting disk
usage, running tests or cleanup scripts, checking a service, and invoking a
non-interactive CLI.

Today a leading `!` can reach the selected coding-agent CLI, which may run a
shell tool and then have the LLM summarize or interpret the result. That spends
model tokens, can alter or omit output, and is unnecessary when the user only
wants command output. Users already understand leading `!` as a deliberately
limited, direct operation, so native execution is the more useful default.

A dedicated `shell` harness was considered first. It is the wrong abstraction:
the user's message is program input, stdout and stderr are the response, and
there is no model, provider, token accounting, conversation history, or native
agent session to resume.

Two process models were also considered:

1. A long-lived shell attached to a PTY, preserving `cd`, exported variables,
   jobs, virtualenv activation, and other state across messages.
2. A fresh shell subprocess for every message, with ordering supplied by
   Squid's existing topic queue.

A persistent PTY resembles a terminal, but makes command completion,
message/output boundaries, restart recovery, prompts, terminal applications,
and background-job ownership heuristic. These are the same reliability costs
that led ADR-0019 to prefer direct one-shot subprocesses over terminal
scraping. Most useful state can instead be scoped to one compound or multiline
command.

There are two visually similar uses of `!` that remain distinct:

- A leading `!` in message content requests native shell execution.
- Squid's existing trailing `!` on an agent selector marks an adhoc LLM turn
  that bypasses the durable `(topic, agent)` queue (ADR-0010).

## Decision Outcome

Intercept a leading `!` as a native Squid operation before dispatch to any
coding-agent harness. Do not add a `shell` harness. The initial/default path is
a stateless one-shot subprocess; a command-scoped terminal is the explicit
escape hatch for interactive work. A long-lived shell spanning chat messages
is not part of this feature.

The input contract is:

```text
#ops ! make clean                queued, sequential native command
#ops@codex ! git status          native command using the resolved lane/cwd
#ops@codex! ! git status         adhoc native command, runs in parallel
#ops@codex! investigate this     unchanged: adhoc LLM turn
```

The leading `!` is removed before execution and never reaches the LLM. A
message without it follows normal agent dispatch; it is not implicitly
transformed into `echo`. Literal output remains explicit (`! echo hello`). The
marker makes the transition from chat text to local code execution intentional.

The selector's trailing `!` remains the adhoc-turn marker and is not shell
syntax. It applies consistently to both LLM and native turns: a native command
without it uses the resolved topic/agent lane and is queued sequentially,
while an explicitly adhoc native command receives its own ephemeral worker and
runs in parallel. Both forms use the resolved working directory and receive a
fresh shell. Neither `cd` nor other shell state persists across messages.

This deliberately changes leading-`!` behavior: agent CLIs no longer receive
those messages or interpret their results. UI and help text must state that a
leading `!` means exact native execution with no LLM involvement. Users who
want an agent to decide which tools to run send a normal prompt instead.

When state is needed, dependent operations belong in one message:

```sh
! cd project &&
  source .venv/bin/activate &&
  pytest
```

### Process and output contract

Each turn starts the configured user shell as a new process group with a
non-interactive command invocation equivalent to `$SHELL -lc <command>`.
Squid streams stdout and stderr to the originating chat message and reports
the final exit status, elapsed time, and working directory, including when the
command emits no output. The result is visibly typed as raw shell output, not
an assistant response.

The path does not invoke an LLM and consumes no LLM tokens. If the command
itself starts an LLM-backed program, that nested program can of course consume
its own tokens.

The process is registered through Squid's existing process registry
(ADR-0018). Message stop, topic stop, timeout, and server shutdown signal the
entire process group rather than only the shell parent. A configurable timeout
may bound commands that do not terminate.

The command runs directly in the resolved agent working directory: the same
cwd in which that lane's model starts. Topic code roots describe the model's
scope and may contain multiple entries; their ordering does not implicitly
select a shell cwd. Native execution does not create or synchronize a per-turn
worktree. Worktree isolation is an implementation boundary for LLM-driven
edits; applying it to an explicit user-authored command would make `pwd`, Git
inspection, and file mutations operate on surprising internal paths. A
command that explicitly changes its directory remains responsible for that
choice, and the change does not persist to the next message because each
command receives a fresh shell. Native execution does not create a model
session ID, inject conversation history, or emit token or cost statistics.

The shell result uses the header's normal right-side context-badge position,
labeled **ctx: shell · no LLM**, but it does not display the orange in-context
indicator. Its metadata popup shows the message ID, starting working directory,
exit status, and elapsed time. It does not display a session/adhoc
classification or a thoughts/trace affordance because no model session or
model trace exists. Pinning, bookmarking, and cancellation remain available.

### Passing output to an LLM

Native shell results are recorded as typed shell-result messages in chat, but
are not automatically injected into an agent session. Command output can be
large, noisy, or secret-bearing; automatic injection would erase the token and
context benefits of native execution.

When interpretation is wanted, the user pins the shell-result message and
sends a normal prompt. Squid then includes the pinned result through its
existing explicit-context path, and only that later agent turn consumes model
tokens. Large results remain expandable or downloadable in chat. Pinning must
show the amount of context being added and allow truncation or selection of an
excerpt rather than silently sending an unbounded log.

### Passing selected context to a shell command

Selected topic memory, pinned responses, and attached files are available to every native
shell command through indexed environment variables. Squid does not append
context to the command text or write its contents directly into environment
variables. A command that does not reference the variables simply ignores the
available context.

The naming contract is:

```sh
SQUID_PINNED_COUNT=2
SQUID_PINNED_1=/private/tmp/.../pinned-1.txt
SQUID_PINNED_2=/private/tmp/.../pinned-2.txt
SQUID_TOPIC_MEMORY=/private/tmp/.../topic-memory.md
SQUID_ATTACHED_COUNT=1
SQUID_ATTACHED_1=/path/to/report.csv
```

Indexes are one-based and contiguous within each category, following the
context cart's displayed order. `SQUID_PINNED_N` always names a UTF-8 temporary
file containing the selected message's content. This includes pinned native
shell results: shell output is stored as a normal typed message, can be pinned,
and is exposed to a later shell command by the same `SQUID_PINNED_N` contract.
The original message remains the durable source; the temporary text file is
only a command-scoped representation.

When topic memory is selected, `SQUID_TOPIC_MEMORY` names a UTF-8 temporary
file containing it. The variable is unset when memory is not selected. Like
pinned-message files, it is command-scoped and does not imply that memory was
delivered or consumed.

`SQUID_ATTACHED_N` names the existing resolved attachment path. Squid does not
copy, modify, or delete the attached file. Filenames and message contents are
never interpolated into shell source, so whitespace, newlines, and shell
metacharacters do not alter the submitted command. Consumers must quote path
variables normally:

```sh
sed -n '1,120p' "$SQUID_PINNED_1" "$SQUID_TOPIC_MEMORY"
wc -c -- "$SQUID_ATTACHED_1"
```

Squid creates pinned-message files in a private command-specific temporary
directory with restrictive permissions. The directory and its generated files
are removed when the tracked command finishes, times out, or is cancelled.
Detached descendants are unsupported and must not rely on those paths after
the tracked command ends. Count variables are always present with `0` when a
category is empty; indexed variables beyond the count are unset.

Making context available to a shell is not context delivery. Squid cannot know
whether the command read a variable or file, so it does not prune pins, mark
them or memory injected, or mark attachments delivered. They remain selected for a later
shell or LLM turn. The shell-result context badge may indicate that selected
context was available, but must not claim it was consumed.

No JSON manifest is part of the initial interface. Numbered path variables
cover the required memory, ordered pins, and attachments with ordinary shell tools and
avoid requiring `jq` or another parser. If future consumers require richer
per-item metadata, Squid may add a versioned `SQUID_CONTEXT_FILE` manifest
without changing the indexed-variable contract.

### Background and detached processes

One-shot completion means the invoked process tree is expected to terminate.
Commands such as `server &`, `nohup`, `disown`, or programs that daemonize can
outlive the shell, escape its process group, retain output descriptors, or keep
an isolated working directory in use after the chat turn appears complete.
Squid cannot reliably turn arbitrary shell daemonization into managed service
lifecycle.

The initial native command path therefore rejects obvious background or
detachment syntax with guidance to run the process in the foreground or use a
future explicit managed-process feature. Detection is a usability guard, not a
security boundary: shell syntax and programs can evade static detection. While
a process remains in the registered group, normal stop and timeout behavior
applies. A process that deliberately creates a new session is outside Squid's
lifecycle guarantee and must not be presented as managed by Squid.

A future durable process facility should use explicit start, logs, status, and
stop operations rather than assigning durable semantics to shell `&`.

### Scoped terminal for interactive commands

A one-shot process cannot reliably answer login prompts, model selectors,
password requests, or arbitrary interactive applications. Squid must not infer
interactivity from silence or automatically open a terminal after a timeout,
because a quiet command may simply be computing.

Instead, the shell-result UI offers a themed **Run interactively** action for a
clear TTY-required failure, and known interactive commands may offer it before
launch. The action opens Squid's embedded terminal around a PTY scoped to that
command. Output and input remain attached to the originating message; exit,
cancel, timeout, or page teardown closes the PTY and terminates its registered
process group. This is not a persistent login shell and preserves no state for
the next message.

Known harness login flows continue using the narrower allowlisted auth PTY from
ADR-0035. The general scoped terminal is the escape hatch for other interactive
commands, not a replacement for that safer path. It uses Squid's common themed
panel or modal rather than a system modal.

### Nested agent CLIs

Native execution supports commands with a defined non-interactive exit:

```sh
! codex exec "review this repository"
! claude -p "summarize the tests"
! codex --version
```

Their output remains raw shell output. Squid does not parse it into tool
events, adopt or resume the nested agent session, or attribute its token usage
to Squid's selected agent.

Bare interactive invocations such as `! codex` and `! claude` are unsupported
in the one-shot path. They may run through the explicit command-scoped
terminal, but Squid's native Codex or Claude harness remains preferable for
conversational sessions with structured events, resumption, cancellation, and
statistics.

### Security boundary

Native commands run arbitrary local code with the permissions of the Squid
server's OS user. The leading `!` is an intent marker, not a sandbox. UI and
documentation must state this clearly.

Obvious high-risk commands may receive an in-app themed confirmation, but
pattern matching cannot establish that a shell command is safe. No system
modal is used. Authorization ultimately follows the same local-user boundary
as agent shell tools described in ADR-0016.

## Alternatives Considered

### Long-lived PTY shell

Rejected for the initial implementation. It preserves state naturally, but
turn completion and output attribution become heuristic; one blocked prompt or
interactive program stalls the lane; restart recovery is ambiguous; and job
control substantially expands process-management scope.

### Dedicated `shell` harness

Rejected. Shell execution has no model, provider, native session, or token
semantics and does not benefit from agent selection. Making it a native
operation provides exact output without fake harness configuration.

### Always open a terminal

Rejected. Most commands need no input. A terminal requires continued user
attention, is awkward on mobile, and does not naturally produce a clean
message-scoped result. It remains an explicit escape hatch.

### Preserve leading `!` as agent input and add `/sh`

Rejected. It preserves compatibility, but continues making the most natural
shell syntax spend tokens and produce interpreted output. Users understand the
direct-execution limit of leading `!`; help and UI labeling make the behavior
explicit.

### Treat non-command text as `echo`

Rejected. It silently changes user input and makes it less clear which messages
execute local code. Users can run `! echo ...` explicitly.

### Automatically redirect Codex or Claude commands to native harnesses

Rejected. Non-interactive subcommands, version checks, and auth/status commands
are legitimate shell operations. Interactive sessions are instead offered the
scoped terminal without silently changing execution targets.

## Prior Art

Added 2026-08-11 while scoping follow-up work on the timeout, output-retention,
and interactive-command gaps left open by the initial implementation. Reviewed
the shell/bash tool implementations of two open-source local coding agents
installed on this machine: `@earendil-works/pi-coding-agent` (source in its
published npm package, `dist/core/tools/bash.js`) and `sst/opencode`
(`packages/opencode/src/tool/shell.ts`, fetched from GitHub — the local
`opencode-ai` npm package only ships a compiled binary).

Neither tool attempts static classification of "known interactive commands"
(`vi`, `tail -f`, REPLs, etc.). Both spawn with no TTY (stdin ignored or
piped, never a pty) and instead bound the interactive/hanging case with a
timeout plus cancellation, not pre-emptive pattern matching:

- **Pi** ships with *no default timeout* on its bash tool — the schema
  documents "no default timeout" and leaves an unbounded hang to be stopped
  by the interactive UI's cancel/abort affordance (`AbortSignal` →
  `killProcessTree`). It assumes a human is present to notice and cancel.
- **OpenCode** defaults to a 2-minute timeout
  (`flags.bashDefaultTimeoutMs ?? 2 * 60 * 1000`) that the model can override
  per call via a `timeout` parameter. On expiry it does not emit a bare
  limit notice; it tells the model *why* and what to do: "shell tool
  terminated command after exceeding timeout ... If this command is expected
  to take longer and is not waiting for interactive input, retry with a
  larger timeout value." This lets the caller self-diagnose an interactive
  hang and either raise the timeout or stop, rather than Squid's current
  generic `[command stopped: {timeout}s runtime limit]`.

Both tools also converge independently on the same output-retention pattern:
truncate what's shown live (a sliding-window tail kept in memory) but keep
streaming full output to a scratch file once the display cap is hit, then
report that file's path in the result (pi: `Full output: <path>`; OpenCode:
`Full output saved to: <path>`). Neither one terminates the process just
because the output cap was hit — only the display truncates, not execution.
This differs from Squid's current behavior, where `run_native_shell` kills
the process outright once `NATIVE_SHELL_MAX_OUTPUT_LINES` /
`NATIVE_SHELL_MAX_OUTPUT_BYTES` is exceeded (`agent/runners.py:251-277`).

OpenCode additionally parses each command with tree-sitter (bash and
PowerShell grammars) to identify which files/directories it touches, and
gates access to directories outside the resolved project root behind an
explicit permission prompt. This is a materially more sophisticated safety
layer than anything described in this ADR's Security Boundary section, but
it addresses a different problem (unexpected filesystem blast radius) than
the interactive-command/output-retention gaps above, and is a much larger
lift — noted here as a future option, not folded into the near-term plan
below.

Implication for this ADR: a static interactive-command denylist (considered
as a possible interim fix) is weaker than what either prior-art tool does,
and is exactly the kind of pattern-matching this ADR's Security Boundary
section already says "cannot establish that a shell command is safe." The
validated alternative — configurable timeout with an actionable message,
plus spooling truncated output to disk instead of killing the process — is
adopted as the near-term plan for the "Truly configurable timeout" and
"Large-output viewing/download" gaps instead. The interactive-hang case
itself (`vi`, `tail -f`) remains unsolved by all three tools structurally;
all three bound it with a timeout and rely on cancellation/retry rather than
pre-detecting it, so Squid's existing process-registry stop/cancel path
(ADR-0018) is the operative mitigation until the scoped terminal ships.

## Consequences

- Good: every one-shot turn has an explicit subprocess exit as its completion
  signal.
- Good: direct commands spend no LLM tokens and preserve exact output.
- Good: shell results enter LLM context only through explicit pinning.
- Good: native commands are sequential without requiring persistent state.
- Good: existing queueing, streaming, cancellation, timeout, and process
  registry machinery can be reused.
- Good: commands start in the resolved lane cwd without creating or
  synchronizing an internal per-turn worktree.
- Good: interactive commands have an explicit command-scoped terminal path.
- Good: compound and multiline commands cover dependent command sequences
  within one reliable lifecycle boundary.
- Neutral: `cd`, exports, aliases, functions, and activated environments do not
  persist across messages.
- Neutral: nested non-interactive agent CLIs work, but only as opaque shell
  processes without Squid session or stats integration.
- Bad: leading `!` no longer invokes an agent CLI's own shell-command feature.
- Bad: durable background services are unsupported; interactive commands
  require the separate scoped-terminal action.
- Bad: arbitrary shell execution expands the consequences of account or UI
  compromise and must be presented as a high-trust local capability.
