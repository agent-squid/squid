---
status: proposed
date: 2026-09-01
---
# ADR-0046: Layered Runtime and User Context Injection

## Context and Problem Statement

Squid augments an agent's request with several kinds of context that have
different owners and lifetimes:

- execution invariants imposed by Squid, such as one-shot process behavior;
- facts computed for the current turn, such as isolated worktree and backing
  repository paths;
- user preferences shared across topics;
- durable topic memory;
- request-local pins, attachments, and lookback context.

Today some runtime wording is assembled in Python and prepended to the user
prompt. Topic memory is also prepended when selected. The resulting prompt is
correctly self-contained for a stateless CLI invocation, but its sources,
injection cadence, and editability are not explicit to users. It is also easy
for two assembly layers to inject the same runtime guidance twice.

Squid needs one context model that works across harnesses. Some CLIs expose a
system-prompt extension while others expose only a per-turn prompt or stdin,
so the model cannot depend on a provider-specific message role.

## Decision Drivers

- Keep mandatory execution invariants effective and consistent across agents.
- Let users inspect everything Squid adds to their request.
- Let users customize preferences without allowing accidental removal of
  worktree or process-lifecycle constraints.
- Make it clear whether a block is injected every turn, at session start, or
  only for one request.
- Keep durable templates separate from rendered per-turn values.
- Make prompt assembly deterministic, auditable, and testable.
- Avoid relying on an agent-specific `@file` import implementation for
  required context delivery.

## Considered Options

### Option A: Put all shared instructions in topic memory

Each topic memory links to or copies a global instruction file.

This makes inheritance visible and user-editable, but conflates Squid-owned
runtime invariants with topic-owned knowledge. A removed, stale, or unsupported
file link can silently break isolation behavior, and every topic needs memory
solely to carry platform plumbing.

### Option B: Keep all runtime wording embedded in prompt-assembly code

This guarantees delivery and makes conditions easy to express, but hides the
actual wording from users and makes policy text harder to review, version, and
test independently from code.

### Option C: Layered Markdown templates with explicit metadata and one
authoritative assembler

Squid owns inspectable Markdown templates for required runtime blocks. Users
own separate global and topic context. A single assembler selects, renders,
orders, and records the blocks for each request.

## Decision Outcome

Adopt **Option C**.

### Context layers

Squid recognizes these layers, in this precedence and delivery order:

1. **Runtime context** — Squid-owned, required, and inspectable.
2. **Global user context** — user-owned, optional, and editable.
3. **Topic memory** — user-owned, optional, and topic-specific.
4. **Request context** — pins, attachments, lookback, and other selections for
   the current send.
5. **User request** — the message that initiated the turn.

Later user-authored layers may refine preferences from earlier user-authored
layers. They cannot disable or redefine runtime facts and constraints. Squid
must not place mandatory runtime policy behind an unlinkable-looking but
actually removable `@file` directive.

### Runtime templates

Runtime templates are split by applicability rather than collected into one
miscellaneous per-turn file:

```text
runtime/
  global.md
  oneshot.md
  worktree.md
```

- `global.md` contains only invariants applicable to every supported Squid
  agent invocation.
- `oneshot.md` applies only to the `oneshot-cli` protocol.
- `worktree.md` applies only when per-turn worktree isolation is active and
  renders the isolated and backing repository paths.

These files are the durable wording source of truth. Squid renders them in
memory for the current request; it does not need to create a generated
Markdown file in each worktree. An optional rendered snapshot is an audit or
debug artifact, never a second source of truth.

### Declarative metadata

Each runtime template has validated frontmatter. For example:

```yaml
---
id: runtime.worktree
owner: squid
inject: every-turn
required: true
when:
  worktree_isolated: true
variables:
  - code_roots
  - backing_repos
  - isolated_roots
---
```

The supported `inject` values are:

- `every-turn` — render and inject on every applicable turn;
- `session-start` — inject when creating a session and again when its source
  revision changes;
- `this-request` — include only when explicitly selected for this request.

Applicability and cadence are separate: `when` decides whether a block applies
to the current execution state, while `inject` decides when an applicable block
is delivered.

All entries in `when` are ANDed. Squid supports only an allowlisted set of
condition keys and literal values, including protocol, worktree-isolation, and
native-shell state. Unknown keys, unknown cadence values, undeclared template
variables, and unresolved required variables are configuration errors rather
than reasons to silently omit a runtime constraint. The template language has
named `{{variable}}` substitution only: no expressions, scripts, environment
lookups, conditionals, or nested includes.

The execution state remains authoritative. Comments in a template may explain
applicability but do not govern it.

### User context and linking

Global user preferences live separately from required runtime templates, for
example at `~/.squid/context/user-global.md`. Topic memory may visibly link to
that file using Squid's file-link syntax. The user may remove or customize this
link because it controls preferences, not execution invariants.

Topic creation does not require a memory file solely to establish runtime
behavior. Default global-user-context inheritance, if offered, is stored as
explicit topic metadata; a link in an existing `memory.md` is a visible user
control, not the mechanism on which required delivery depends.

Squid resolves user-context links itself and passes their contents explicitly.
It does not rely on Codex, Claude, or another harness interpreting `@path`
consistently.

### Prompt assembly and harness delivery

One server-side assembler is the sole authority for context selection and
ordering. It receives resolved execution state only after protocol selection
and, when applicable, isolated-worktree setup. It emits typed blocks containing
at least:

```text
id, owner, source, required, inject, template revision, rendered content
```

The harness adapter then delivers the assembled content through the strongest
portable per-turn interface it supports. Squid does not assign different
semantics merely because one provider exposes a system-prompt flag and another
accepts only a prompt argument or stdin. Required runtime blocks must be
present in the effective request for every harness.

Session-resumed agents still receive `every-turn` blocks. This is necessary
for current execution facts such as a newly isolated worktree and for one-shot
process lifecycle guidance. `session-start` blocks rely on session continuity
and are reinjected when their revision changes.

### Visibility and audit

The composer shows optional user context and topic memory as removable context.
Required runtime blocks are not presented as removable chips. Turn details
provide a **Runtime context** disclosure listing each applicable block with a
plain cadence label such as `required · every turn`.

For each turn, Squid records enough information to establish what was actually
injected, including block IDs, source/template revisions, cadence, and rendered
hashes. Exact rendered text may also be retained where replay or debugging
requires it. Eligibility alone is not an audit record; the record reflects the
blocks actually delivered.

## Consequences

- Good: users can inspect Squid-added instructions without being able to
  accidentally unlink mandatory runtime behavior.
- Good: user customization remains first-class through global user context and
  topic memory.
- Good: applicability and injection cadence are explicit in templates and UI.
- Good: runtime wording is reviewable Markdown rather than scattered string
  literals.
- Good: one authoritative assembler prevents duplicate injection.
- Good: behavior remains consistent across harnesses with different native
  prompt roles and file-import features.
- Bad: Squid needs a small validated template loader, renderer, and condition
  registry.
- Bad: changes to runtime template text require versioning and regression tests
  because they affect every applicable agent turn.
- Neutral: the final CLI input may still be one combined string even though
  Squid stores, displays, and audits its constituent blocks separately.

## Related Decisions

- ADR-0009 defines request-context injection and session-level deduplication
  for pinned messages and topic memory.
- ADR-0019 selects direct one-shot CLI subprocess execution.
- ADR-0020 stores topic-specific code roots in topic memory frontmatter while
  keeping runtime diff tracking independent from memory injection.
- ADR-0022 defines the supported execution protocols.
- ADR-0025 defines per-turn worktree isolation.
