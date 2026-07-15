---
status: proposed
date: 2026-07-15
---
# ADR-0032: Route Chains with CWD-Profile Agents

## Context and Problem Statement

Squid should support workflows where an agent's behavior comes from the CLI's
native cwd-based profile loading, not from a Squid-specific role system.

Example:

```text
#squid@codex<2>@review!
```

The intended reading is:

1. Send the prompt to `#squid@codex`.
2. Run two follow-up review passes through fresh `@review` sessions.
3. Let `@review` get its role/profile from its configured `cwd`.
4. Preserve route autocomplete support for the full expression.

This keeps Squid responsible for routing only. The agent CLI remains responsible
for reading `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.cursor/rules`, or any other
native profile files from its process cwd.

## Decision Drivers

- Avoid introducing Squid-owned roles, role registries, or prompt templates.
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
#topic@agent<2>@next
#topic@agent<2>@next!
```

`>` means pass the previous response forward to the next route step.

`<N>` means repeat the next step `N` times. In:

```text
#squid@codex<2>@review!
```

`@review!` runs twice after `@codex`, with each review pass using a fresh native
session.

Bare `@review` inside a chain resumes the same persistent session as
`#squid@review`.

`!` on a chained route step means fresh native workflow session for that step.
It is a boolean suffix and does not accept a numeric argument.

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
- Complete `@agent` after `#topic`, `>`, and `<N>`.
- Preserve typed chain operators when accepting a completion.
- Complete and accept `!` as a fresh-session suffix on chained route steps.
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
| `>@review` chained step | Use the existing resumable `(topic, review)` session |
| `>@review!` chained step | Start a fresh native session for that step |
| `>@review!2` chained step | Unsupported: `!` does not take `N` |

The chain itself is the transaction. Each downstream step receives the previous
step's output as explicit chain input and does not consult global recent-message
lookback.

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
`#squid@codex<2>@review!` is the supported form for two fresh review passes.

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
- Supports both conversational review lanes and fresh review passes.
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
  `#topic@agent` resumes; `#topic@agent!` remains ordinary adhoc if used as a
  standalone prompt.
- Every bare chained `>@agent` step uses the persistent `(topic, agent)` session.
- Every chained `>@agent!` step starts a fresh native session for that chain run.
- Numbers after chained `!` are unsupported.
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
- Good: `>@agent!` gives a fresh review pass without global recent-message
  lookback.
- Bad: chained execution needs fresh-session handling that is separate from
  both resumed sessions and adhoc lookback.
- Constraint: `!N` remains valid for standalone adhoc turns only. In chained
  route steps, `!` never takes a number.
