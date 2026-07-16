---
status: proposed
date: 2026-07-15
---
# ADR-0032: Route Chains and Flow Graphs with CWD-Profile Agents

## Context and Problem Statement

Squid should support workflows where an agent's behavior comes from the CLI's
native cwd-based profile loading, not from a Squid-specific role system.

Example:

```text
#squid@codex<2>@review!
```

The intended reading is:

1. Send the prompt to `#squid@codex`.
2. Run two request/response rounds through fresh `@review` sessions.
3. Let `@review` get its role/profile from its configured `cwd`.
4. Preserve route autocomplete support for the full expression.

This keeps Squid responsible for routing only. The agent CLI remains responsible
for reading `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/rules`, or any other
native profile files from its process cwd.

## Decision Drivers

- Avoid introducing Squid-owned roles, role registries, or role-specific prompt
  templates. The generic chain envelope is part of the routing protocol.
- Keep route chains as routing syntax only; the route must not make Squid infer
  agent-specific roles such as reviewer, summarizer, tester, or implementer.
- Keep the existing agent config model: an agent may have a `cwd`.
- Make route composition easy to type and discover through autocomplete.
- Preserve the existing meaning of a bare `@agent`: it names the persistent
  `(topic, agent)` session lane, including inside chains.
- Keep session semantics explicit enough that review agents do not accidentally
  reuse stale conversational context when a fresh review is expected.
- Make chained execution deterministic even while unrelated adhoc prompts are
  running in parallel.

## Proposed Route Syntax

Route autocomplete should understand chained route expressions:

```text
#topic@agent
#topic@agent>@next
#topic@agent<>@next!
#topic@agent<2>@next
#topic@agent<2>@next!
#topic@agent!<>@next!
```

`>` means pass the previous response forward to the next route step. `=>` is
accepted as the same one-way handoff operator.

`<>` means one request/response round: pass the previous response forward to
the next route step, then pass that step's response back to the original route
step. It is equivalent to `<1>`. In:

```text
#squid@codex<>@review!
```

Squid runs `@codex`, sends the result to fresh `@review!`, then sends the
review output back to `@codex`. This is shorthand for writing the return step
explicitly:

```text
#squid@codex>@review!>@codex
```

`<N>` means run `N` request/response rounds with the next route step. In:

```text
#squid@codex<2>@review!
```

Squid runs `@codex`, sends the result to fresh `@review!`, sends the review
output back to `@codex`, then repeats that request/response round once more
using the latest returned output.

Bare `@review` inside a chain resumes the same persistent session as
`#squid@review`.

`!` on a chained route step means fresh native workflow session for that step.
It is a boolean suffix and does not accept a numeric argument.

`<>` and `<N>` are the same request/response operator family: `<>` means one
round, and `<N>` means `N` rounds.

When a repeated step uses `!`, each execution of that step starts its own fresh
native session. Therefore `#squid@codex<2>@review!` uses two independent fresh
`@review!` sessions; review state passes between rounds only through the chain
envelope.

## Flow Graphs

Route chains are the first supported subset of a flow graph. A linear chain is
a graph with one active path and no joins, cycles, or scheduled edges. The same
execution model should handle both simple chains and fuller graph expressions.

Flow graphs list directed lineage clauses:

```text
#topic@origin>@review!,@origin>@test!,@review!+@test!>@origin
```

The intended reading would be:

```text
#topic@origin > @review!
@origin > @test!
@review! + @test! > @origin
```

The first full route establishes the topic for the graph expression. Later bare
`@agent` steps inherit that topic, so `@review!` means `#topic@review!`.

Commas separate lineage clauses. `>` and `=>` remain directed handoffs. `+` is
an explicit join operator: the downstream step runs after every listed upstream
step has completed, and Squid synthesizes one combined chain envelope from the
joined outputs.

Implicit joins are not part of the flow syntax. For example:

```text
@review!>@origin,@test!>@origin
```

would mean two independent executions of `@origin`, not one joined execution.
A true join must be written explicitly:

```text
@review!+@test!>@origin
```

The same operator family supports bounded request/response loops and scheduled
one-way handoffs:

```text
@a<5>@b
@a<5:1d>@b
@c=1d>@a
@c=5:1d>@a
@c=5:1d/1400>@a
```

`<N:T>` means run `N` request/response rounds with the next route step, waiting
duration `T` between rounds. `=>` is equivalent to `>`. `=T>` means one delayed
one-way handoff. `=N:T>` means `N` delayed one-way handoffs spaced by duration
`T`. A `/HHMM` suffix anchors the schedule to a wall-clock time in the user's
timezone, so `=5:1d/1400>` means five one-way handoffs, once per day at 14:00.
Without `/HHMM`, the duration is relative to the triggering completion time.

Unbounded graph cycles are invalid. A cyclic flow must include an explicit
bound or schedule, such as `@c=5:1d>@a`. Delayed edges create future flow runs
with lineage metadata; Squid must persist the pending handoff rather than keep
an in-memory chain suspended for the delay.

The first implementation can support only linear chains while using the same
flow representation internally. Joins, scheduled edges, and cyclic flows can
land incrementally without redefining the route syntax.

Complex graph expressions should also have a named alias form so the composer
does not need to display or edit the full lineage list for common workflows.
The preferred alias suffix is `.flow`:

```text
#topic@review.flow
```

The suffix keeps autocomplete name-first: users can type the meaningful flow
name, such as `review`, before choosing the `.flow` type. A future config might
define the alias as:

```yaml
flows:
  review:
    route: "@codex>@review!,@codex>@test!,@review!+@test!>@codex"
```

In this form, `#topic@review.flow` would expand to the configured flow graph
with all bare `@agent` steps inheriting `#topic`.

## CWD-Profile Agents

An agent entry may specify a cwd:

```yaml
agents:
  codex:
    cli: codex
  review:
    cli: codex
    cwd: ~/agents/review
```

Squid does not inspect or validate `~/agents/review`. It only spawns the CLI
from that cwd. The CLI loads its own profile files using its normal behavior.

This is deliberately not a role feature. `@review` is just an agent name.

## Autocomplete Requirements

Route autocomplete should support this workflow as a first-class input form:

- Complete `#topic` at the start of the expression.
- Complete `@agent` after `#topic`, `>`, `<>`, and `<N>`.
- Preserve typed chain operators when accepting a completion.
- Complete and accept `!` as a fresh-session suffix on chained route steps.
- Complete and accept `<>` as a request/response operator before a chained
  route step.
- Prefer `!` on autocompleted request/response chain targets, e.g.
  `#topic@origin<>@next!`, because fresh collaborators are the safer default.
  Bare `@next` remains valid when the user intentionally wants the persistent
  `(topic, next)` lane involved.
- Do not complete or accept any number after `!` on chained route steps.
- Show existing agents such as `@review` even when their behavior is entirely
  defined by cwd-profile files outside Squid.

Autocomplete must not need to read profile directories.

## Session Type

Existing Squid semantics are:

- `#topic@agent message` uses the existing resumable `(topic, agent)` session.
- `#topic@agent! message` runs adhoc and does not store or resume a session.

Chained route steps use different semantics:

| Form | Possible meaning |
|---|---|
| `#topic@agent` at chain start | Use the existing resumable `(topic, agent)` session |
| `#topic@agent!` at chain start | Start a fresh native session for the origin step |
| `#topic@agent!2` at chain start | Unsupported in chains: `!N` remains standalone adhoc lookback only |
| `>@review` chained step | Use the existing resumable `(topic, review)` session |
| `>@review!` chained step | Start a fresh native session for that step |
| `<>@review!` request/response step | Send output to fresh `@review!`, then send the review output back to the originating route step |
| `>@review!2` chained step | Unsupported: `!` does not take `N` |

A chain carries explicit step-to-step input. Each downstream step receives the
previous step's output as explicit chain input and does not consult global
recent-message lookback.

Persistent chained steps intentionally use and extend the target
`(topic, agent)` native session. This is useful when the user wants an
established lane involved, but it means the chain is not an end-to-end
transaction: other work queued for that persistent lane may run before a later
chain step returns to it, and the native session may contain intervening turns.
It can also silently accumulate generated chain turns inside agents such as
`@review` if the UI does not make the mode obvious. Therefore autocomplete
should prefer `@agent!`, history should label generated chain prompts, and bare
`@agent` in a chain should be treated as an explicit choice to involve and grow
the persistent lane.

End-to-end chain isolation is only guaranteed when every step in the chain uses
fresh/adhoc session semantics. In mixed chains, the envelope still provides
explicit handoff context, but persistent native session state remains persistent.

## Chain Envelope

Encapsulation comes from the route chain, not from agent-specific behavior.
Squid knows a turn is chained because the route contains `>`, `<>`, or `<N>`,
so Squid wraps each step's raw final output in a structured chain envelope
before sending it to the next step.

For a request/response chain:

```text
a <> b
```

the data flow is:

```text
a -> chain(a output) -> b -> chain(b output) -> a
```

Agents do not need to emit a special machine-readable response for chaining.
Squid captures the normal final assistant output and creates the next automated
user prompt with chain metadata such as the original user prompt, source route,
target route, round number, and previous step output.

## Downstream Prompt Synthesis

The user prompt starts the chain. Downstream route steps receive automated user
prompts synthesized by Squid. This is a generic chain protocol, not a
Squid-owned role registry.

For:

```text
#squid@codex>@review! implement the feature
```

Squid sends `implement the feature` to `#squid@codex`. After that step
completes, Squid sends an automated user prompt to `#squid@review!` containing:

- The original user prompt.
- The previous step's route.
- The previous step's final output.
- Any explicitly scoped chain context that Squid can attach deterministically,
  such as the chain's workspace diff/status when available.
- Squid-observed changed files, when included, are hints for the downstream
  agent. If worktree isolation is disabled, they may include unrelated parallel
  edits and must not be treated as an authoritative scope.
- A generic collaboration contract: follow the downstream agent's configured
  instructions when they define a specific role; otherwise act as an independent
  collaborator on the original request using the previous output as explicit
  context.

Example shape:

```text
You are a downstream agent in a Squid request/response chain.

Follow your configured agent instructions. If they define a specific role, use
it. Otherwise, act as an independent collaborator on the original user request,
using the previous step as explicit context.

<chain_input>
Original user prompt:
implement the feature

Previous step:
Route: #squid@codex
Final output:
...
</chain_input>

Return a response useful to the originating agent. Do not assume hidden context.
```

The previous step's output is therefore delivered as a new user prompt to the
downstream agent. It is not inserted as assistant history and Squid does not
replay another agent's conversation transcript into the downstream agent's
native session.

Return steps created by `<>` use the same automated prompt mechanism, with the
other half of the generic contract: the originating agent reconciles the
responding agent's output against the original user request. Squid still does
not infer an agent-specific role. The returned-to agent should follow its
configured instructions, incorporate valid points, ignore incorrect ones, and
produce the next best answer or work result for the original request.

Example return shape:

```text
You are the originating agent in a Squid request/response chain.

A downstream agent responded to your prior output. Follow your configured agent
instructions. Use the response as external feedback: incorporate valid points,
ignore incorrect ones, and produce the next best answer or work result for the
original user request.

<chain_input>
Original user prompt:
implement the feature

Your prior output:
...

Downstream route:
#squid@review!

Downstream response:
...
</chain_input>

Continue the original task using this feedback.
```

This lets generic agents participate usefully without per-agent customization,
while specialized cwd-profile agents can still interpret the same chain input
through their configured role.

## Chain Failure Handling

If any chain step errors, times out, is cancelled, or produces no final output,
Squid stops the chain by default. It records the failed step with its normal
message status and does not run later downstream or return steps.

The failed step should be visible as a generated chain turn so the user can
inspect it and decide whether to retry that step or rerun the chain. Squid does
not silently skip a failed step, synthesize a successful response, or continue
the chain with an empty output.

Scheduled flow edges use the same fail-stop default. If the source step fails,
no scheduled handoff is created. If a scheduled target run fails, Squid marks
that scheduled edge failed and does not enqueue remaining repetitions. For
example, if `@c=5:1d/1400>@a` fails on the third `@a` run, the fourth and fifth
runs are not scheduled unless the user explicitly retries or resumes the flow.

## Relationship to Parallel Execution

This is closer to parallel workflow isolation than to adhoc history injection.

ADR-0025 isolates filesystem changes so a turn can run without bleeding writes
into other turns. Chained `>@agent!` applies the same idea to conversation
state for concurrent workflows: the route step gets explicit input from the
chain, runs in a fresh native session, and does not inherit hidden prior context
from the persistent `(topic, agent)` session.

This matters because Squid may have direct prompts, adhoc prompts, and chained
workflow steps all running at the same time. A route step cannot safely define
its context by asking for "the recent N messages" from a shared topic list while
other work is appending to that list. The chain must carry its own explicit
inputs.

That makes `!` in a chain a parallel-workflow isolation marker, not a lookback
marker.

## Why `!` Does Not Take `N` in Chains

Adhoc lookback is relative to the topic's message list. For example,
`>@review!2` would mean "review with the recent two messages", but unrelated
adhoc prompts can complete while the chain is running. That makes "recent two"
non-transactional and potentially nondeterministic.

Even if `!2` were redefined to look only inside the current route, it would no
longer match accepted adhoc semantics from ADR-0001 and ADR-0006. Rather than
make `!N` context-sensitive, chained steps should always use explicit chain
input.

Plain `!` is still useful in chains because it asks for a fresh native session,
not relative history lookback. Repetition already belongs to `<N>`, so
`#squid@codex<2>@review!` is the supported form for two request/response rounds
with fresh review sessions.

At the chain start, `#topic@agent!<>@review!` is valid and means the originating
step is also fresh. `#topic@agent!2<>@review!` is not valid for the first
implementation because `!N` is global recent-message lookback; chained execution
must use explicit chain input rather than relative topic history.

## Considered Options

### Option A: Bare Chain Steps Start Fresh Native Sessions

`>@review` starts a fresh native session for that route step. Chained `!` and
`!N` are rejected.

Good:

- Deterministic by default.
- Avoids stale review context.

Bad:

- Breaks the existing mental model that bare `@agent` refers to the persistent
  `(topic, agent)` session.
- Makes `>@review` behave differently from `#topic@review`.

### Option B: Bare Chain Steps Resume; `!` Starts Fresh Native Session

`>@review` resumes `(topic, review)`. `>@review!` starts a fresh native session
for that route step. Numbers after `!` are rejected.

Good:

- Preserves the existing meaning of bare `@agent`.
- Supports both conversational review lanes and fresh request/response rounds.
- Lets the CLI own context within each review run.
- Avoids nondeterministic relative lookback by rejecting numbers after `!`.

Bad:

- Requires a distinct execution path from ordinary resumed session turns.
- Fresh chain sessions must not overwrite the stored `(topic, agent)` session.

### Option C: Make `!` Always Mean Adhoc in Chains

Allow `>@review!` and `>@review!N` with ordinary adhoc semantics.

Good:

- Fully preserves ADR-0001 and ADR-0006 semantics.

Bad:

- `!N` depends on global recent-message lookback, which is not transactional.
- `>@review!` may not exercise native session behavior inside the review agent.

## Proposed Outcome

Adopt Option B.

For the first implementation:

- The first route step uses normal explicit input semantics:
  `#topic@agent` resumes; `#topic@agent!` starts a fresh origin step in a chain;
  `#topic@agent!N` remains ordinary adhoc lookback only for standalone prompts.
- Every bare chained `>@agent` step uses the persistent `(topic, agent)` session.
- Every chained `>@agent!` step starts a fresh native session for that chain run.
- Every repeated chained `>@agent!` execution gets a separate fresh native
  session.
- Numbers after chained `!` are unsupported.
- If any step fails, the chain stops and later steps are not run.
- Route autocomplete supports `#squid@codex<2>@review!`.
- No Squid-owned roles or profile registry are added.

Fresh chain sessions are ephemeral workflow sessions. They should not update or
replace the persistent `(topic, agent)` session row used by normal direct
messages.

## Consequences

- Good: enables cwd-profile review agents without adding Squid role concepts.
- Good: autocomplete can expose the full workflow immediately.
- Good: chained reviews are deterministic because they consume explicit chain
  input.
- Good: bare `@agent` consistently means the persistent `(topic, agent)` lane.
- Good: `<>@agent!` gives a fresh request/response round without global
  recent-message lookback.
- Bad: chained execution needs fresh-session handling that is separate from
  both resumed sessions and adhoc lookback.
- Constraint: `!N` remains valid for standalone adhoc turns only. In chained
  route steps, `!` never takes a number.
