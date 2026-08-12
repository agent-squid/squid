---
status: accepted
date: 2026-08-10
updated: 2026-08-10
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
#ops@codex! investigate this     unchanged: adhoc LLM turn
```

The leading `!` is removed before execution and never reaches the LLM. A
message without it follows normal agent dispatch; it is not implicitly
transformed into `echo`. Literal output remains explicit (`! echo hello`). The
marker makes the transition from chat text to local code execution intentional.

The selector's trailing `!` remains exclusively an adhoc-agent marker and must
not be overloaded as shell syntax. Native commands use the resolved
topic/agent lane and working directory and are queued sequentially, but each
receives a fresh shell. Neither `cd` nor other shell state persists across
messages.

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
