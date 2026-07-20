---
status: accepted
date: 2026-07-15
updated: 2026-07-20
---
# ADR-0032: Route Chains and Squid Flow with CWD-Profile Agents

> For the technical implementation reference (grammar, resolution semantics,
> branch expansion, canonical forms, and the server-side execution model) see
> the [Squid Flow v0.1 Whitepaper](../squid-flow-whitepaper.md). This ADR is
> the decision record — scope boundary and rationale; the whitepaper is the
> implementation reference.

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

## Current Implementation

As of 2026-07-20, Squid implements the v0.1 single-operator live subset. The
grammar is symmetric between the two edge types (see "Edge types" below) —
the only structural difference is the leading character:

```text
#topic@origin[!]>@target[!]
#topic@origin[!]<[N][:T]>@target[!]
#topic@origin[!]=[N][:T]>@target[!]
```

`>` is shorthand for `=>`, itself shorthand for `=1>` — parsed and rendered
identically to `=[N][:T]>` with `N` and `:T` both omitted, not a separate
form (`ui/flow-lang.js`'s `SCHEDULED_RE`/`_parse_operator_token` in
`agent/flow.py` both make `N` optional with an `N ? int(N) : 1` default, the
same way `<[N][:T]>` already did). Canonical rendering always emits the
shortest spelling for a given `(count, wait)` — `>` for `count=1, wait=None`,
`=:T>` for `count=1, wait=T`, `<>` for `rounds=1, wait=None` — regardless of
which equivalent spelling was typed, so `#a>@b`, `#a=>@b`, and `#a=1>@b` all
resolve to the identical stored `flow_route`.

The broader Squid Flow syntax in this ADR remains the accepted direction, but
multi-hop chains, `;` DAG clauses, cycles, and unbounded `=*` loops are outside
v0.1. Delayed `:T` forms use an in-memory async delay in the current
implementation; if the server restarts while a delayed handoff is sleeping,
that pending delayed handoff is lost.

A chain target may be a full `#topic@agent` route, a bare `@agent`, or a
bare `#topic` that inherits the source agent. Target fan-out, origin-side
joins, and — under `<>`/`<N>` only, where the round-trip return leg is a real
consumer for it (see the join-principle bullet under "v0.1 Scope") —
target-side joins are all implemented for the same single operator.

The client only ever sends the origin turn. Every step after that is
dispatched by the server (`agent/flow.py`), not the browser: `TopicWorker`
(`agent/topic_queue.py`) already runs each turn to completion independent of
any connected client, persisting through to `chat_messages`/`run_events`, so
the moment a step's message is marked done, that same completion hook decides
whether a Squid Flow chain has a next step and — if so — dispatches it
in-process, with no HTTP round trip. This means a chain keeps running (and
completing) even if the browser tab that started it is refreshed or closed
mid-chain; earlier, continuation was a client-side JS closure tied to that
tab's SSE stream, so a refresh mid-chain silently stranded the chain with no
later step ever sent. A boot-time sweep also resumes any chain whose last step
finished but had no next step dispatched (e.g. the server itself restarted
mid-chain, or a chain stranded by the old client-driven behavior before this
fix). The browser polls `GET /chat/flow/{flow_run_id}/steps` to discover
server-dispatched steps and render them live while a tab is open, purely for
UX — the chain's correctness never depends on a client being connected.

For `>`, the origin prompt is sent first, then Squid synthesizes and sends the
target prompt after the origin response completes. For `<>` and `<N>`, Squid
sends alternating target/origin handoffs until the requested round count is
complete. For `=N>`, Squid sends the same one-way target handoff `N` times
from the same previous output. `:T` adds an in-memory delay before each
scheduled repeat. Both the forward and return handoff prompts reuse the same generic
template described under "Downstream Prompt Synthesis" below (Route / Previous
step / Current step / Original prompt); the differentiated reconciliation-style
return prompt shown later in this ADR is the target direction, not yet
implemented. The backend still rejects route-chain expressions sent directly
to `/chat` via the legacy `route` field; this keeps the chain visible as normal
message rows rather than a hidden backend-only transaction.

### v0.1 Scope

v0.1 covers a single clause with **at most one hop operator** — no chained
multi-hop sequences (chaining more operators together). This subsection is
the authoritative boundary; every other section in this ADR should be read
against it, and any example elsewhere that implies something outside it is
describing the broader Squid Flow direction, not v0.1.

**In scope:**

- An origin group that contains at least one fully explicit anchor:
  `#topic@origin[!]`. A single origin atom must therefore be fully explicit,
  but an Origin Broadcast or join may use omitted-half atoms once some atom
  in that origin group establishes the root that later inheritance rolls
  from: `#topic@a,@b` and `#topic@a+@b>@c` are valid.
- At most one hop operator after the origin: `>`, `=>`, `<>`, `<N>`, `<N:T>`,
  `=N>`, `=N:T>`. A clause has either zero operators (a bare origin or
  broadcast, nothing downstream) or exactly one — never two or more chained
  together.
- One or more target atoms for that one hop, each independently
  `[#topic]@target[!]`, `#topic[!]` (bare topic — inherits the origin's
  agent), or `@target[!]` (bare agent — inherits the origin's topic).
  Multiple targets after one operator (`#topic@origin>@a,@b`, target
  fan-out) is in scope — it's still one hop, one operator, just multiple
  independent targets fed by it, structurally the same decomposition Origin
  Broadcast already does (each target atom resolves independently against
  the one shared parent state, then dispatches as an independent handoff
  carrying the same previous-step output). For example,
  `#topic@origin>#other` is valid: the origin is fully explicit, and the
  target switches topic while inheriting `origin`'s agent. `#topic>#other@target`
  is **not** valid syntax at any point in Squid Flow: the origin atom here is
  only `#topic` — no `@origin` — so nothing establishes a root at all.
  Concretely, origin resolution (`resolveGroupAgainstState`, `isOrigin=true`)
  requires *some* atom in the origin group to already be fully explicit
  before anything can act as a root (`firstAnchor = atoms.find(a => a.topic
  != null && a.agent != null)`); with a single bare-topic origin atom and no
  sibling to borrow from, there is no candidate and resolution fails outright
  ("nothing to inherit yet"). Target atoms never donate backward into the
  origin either — inheritance only flows origin → target, never the reverse
  — so `@target`'s agent is never a candidate source for the origin's missing
  agent, even in principle.
- **Principle of join: a join always requires a downstream consumer.** `+`
  pins every joined branch's final message into one later step's turn (see
  "Squid Flow" below) — that pinning is the join's entire reason to exist.
  A join with nothing to pin *into* is meaningless, full stop; this is the
  one rule that decides whether any given `+` is legal, in any position.
  Unlike `,` (Origin Broadcast), which is a complete, meaningful clause on
  its own (each listed origin just runs independently — see "zero operators"
  above), a bare `#topic@a+@b` with nothing after it has no consumer and is
  **not** valid syntax. `ui/flow-lang.js`'s `parseClause` rejects this case
  explicitly.
- The **origin** always satisfies the join principle, feeding the clause's
  one hop: `#topic@a+@b>@c` and `#t1@a+#t2@b>#t3@c` are both valid. See
  "Squid Flow" below for what happens when the joined origins disagree on a
  field the target needs to inherit.
- A hop's **target** can also satisfy it, but only under `<>`/`<N>`:
  `#t1@a1<>#t2+#t3` dispatches `#t2` and `#t3` in parallel from `t1`'s
  forwarded output, waits for *both* to complete, then fires one return turn
  to `t1` pinning both — the round-trip's return leg is what makes this a
  real consumer, already built into one operator token, unlike a plain `>`
  target (nothing can follow it in a single-operator clause, so `+` there
  is never valid — `#t1@a1>#t2+#t3` still rejects). The join-gate this
  needs — dispatch every joined target, wait for all of them, pin all of
  them into the one return step — lives in
  `agent/flow.py`'s `next_chain_steps` round-trip loop (`leg_targets =
  branch["targets"] if leg % 2 == 0 else [origin]`, gated by a `pending`
  list mirroring the origin-side join gate in the same function's `ready`
  loop) and in `ui/flow-lang.js`'s `expand()` (`hop.target.kind === 'plus'`
  branch of the roundtrip case). Every other operator (`>`, `=>`, `=N>`) is
  terminal — no hop can follow it in v0.1 — so target-side `+` stays illegal
  there regardless of anything else in the clause.

**Out of scope:**

- Multi-hop chains — more than one operator in a clause (`@a>@b>@c`). Once a
  hop's target (or targets) is reached, that clause is done.
- Multiple `;`-separated clauses (a full Squid Flow DAG). v0.1 is one clause.
- `;` multi-clause DAGs, cycles — not part of v0.1 or its near-term
  extension; `ui/flow-lang.js`'s reference grammar rejects `;` outright.

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

## Accepted Route Syntax

Route autocomplete should understand chained route expressions:

```text
#topic@agent
#topic@agent>@next
#topic@agent<>@next!
#topic@agent<2>@next
#topic@agent<2>@next!
#topic@agent!<>@next!
```

The current implementation accepts only:

```text
#topic@agent>@next
#topic@agent>@next!
#topic@agent!>@next
#topic@agent!>@next!
#topic@agent<>@next
#topic@agent<>@next!
#topic@agent!<>@next
#topic@agent!<>@next!
```

`=>` and `<N>` (for any N, including `<1>`) are not recognized yet; only the
literal `<>` token parses as the request/response operator.

`>` means pass the previous response forward to the next route step. `=>` is
meant to be accepted as the same one-way handoff operator, but is not
implemented yet (see "current implementation accepts only" above).

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

A chained route step may instead be a full `#topic@agent`, switching the
active topic for that step (and any later bare `@agent` steps, which then
inherit the new topic):

```text
#squid@codex>#hive@review!
```

This hands off from `#squid@codex` to a fresh `@review!` session on `#hive`,
not `#squid`. This explicit form (`#topic@agent`, both halves shown) is
implemented. The bare-topic form below it (`#hive` alone, agent inherited) is
not yet implemented — see "Current Implementation" and "v0.1 Scope" above.

A graph clause step is its own parse context, separate from an ordinary
standalone message, so ADR-0005's sticky-agent rule for a bare `#topic` does
not apply here. Within a graph clause, whichever half of `#topic@agent` a step
omits is inherited from the root, symmetrically in both directions: a bare
`@agent` inherits the root's topic (as above), and a bare `#topic` inherits the
root's agent. So `#squid@codex>#hive` means `#hive@codex` — same agent,
switched to `#hive` — not `#hive`'s own sticky agent. Writing `#hive` as a
complete standalone message elsewhere still resolves via ADR-0005 as always;
only its meaning as an omitted-agent graph-clause step is different, and that
difference is disambiguated by parse position, not by the text itself.

Canonicalization reuses this inheritance rule rather than inventing a
grouping notation. Given `#t1@a1,#t2@a1>#t3@a1`, the reduced form is
`#t1@a1,#t2>#t3` — `#t2` and `#t3` both inherit `a1` from the root, so
repeating `@a1` is redundant. The reduced form is never a bracket/group
shorthand like `#t1,t2@a1>#t3` (one shared `@agent` applying backward over a
list of bare topics) — that would be a second way to spell the same meaning
via a new grammar shape, and canonicalization must produce exactly one shape
per meaning, using rules that already exist.

Within an origin list, at least one atom must be a full `#topic@agent`.
Inheritance rolls left to right by *nearest fully-explicit ancestor*, not by
a single anchor fixed for the whole list: the first fully-explicit atom seeds
the initial root (so a bare atom with nothing yet resolved to its left still
borrows from the first complete atom in the text), but every atom that is
itself fully explicit supersedes the prior root for everything after it. A
root can donate its topic to one atom and its agent to a different atom at
once: `#t3,#t2@a1` is valid (`#t2@a1` is the only complete atom, so `#t3`
borrows `a1` from it), and `#t3,@a2,#t1@a1` resolves to `#t3@a1,#t1@a2,#t1@a1`
(`#t1@a1` is the only complete atom, so it donates both halves: `#t3` becomes
`#t3@a1`, `@a2` becomes `#t1@a2`).

Once a later atom is itself fully explicit, it becomes the new root for
everything after it — this is a rolling handoff, not a single global anchor:

```text
#t1@a1,#t2,@a2,#t4@a4,#a1
```

resolves left to right: `#t1@a1` seeds the root; `#t2` borrows `@a1` (root's
agent); `@a2` borrows `#t1` (root's topic); `#t4@a4` is itself fully explicit
and becomes the new root; `#a1` — a *topic* that happens to be spelled the
same as the first agent's name, a different namespace — borrows `@a4` from
that newer root, not `@a1` from the first one:

```text
#t1@a1,#t2@a1,#t1@a2,#t4@a4,#a1@a4
```

This is why order matters: `#t1@a1,#t3,#t2@a2` and `#t1@a1,#t2@a2,#t3` are
different graphs even though they contain the same three atoms — in the
first, `#t3` sits before any second fully-explicit atom appears, so its
nearest ancestor is still the root `#t1@a1` and it resolves to `#t3@a1`; in
the second, `#t3` sits after `#t2@a2`, which has already superseded the root,
so it resolves to `#t3@a2` instead. Reordering only changes the meaning when
it changes which fully-explicit atom is nearest to a given bare atom.

Rolling anchor is specific to origins, and only origins: an origin atom has
no parent to inherit from, which is the entire reason it borrows from a
sibling instead. A hop-target list is different — every target atom already
has a parent, the edge's source — so each target atom resolves independently
against that parent and never against another atom in the same target list,
no matter how that sibling happens to be written. `#squid@codex>@b,#t2@c,@d`:
`@d` inherits topic `squid` from the edge's source (`#squid@codex`), not `t2`
from its sibling `#t2@c`, even though `#t2@c` is fully explicit and sits
immediately to `@d`'s left. A target atom that happens to be fully explicit
is explicit only for itself; it does not become a root for the atoms next to
it the way a fully-explicit origin atom does for later origins.

A comma-list source always decomposes into independent branches before any
inheritance is resolved (unchanged from Origin Broadcast) — so each branch
keeps its own single root and inheritance is never ambiguous here, even when
the origins disagree. `#t1@a1,#t2@a2>#t3` decomposes into two independent
edges first, `#t1@a1>#t3` and `#t2@a2>#t3`, and only then does each `#t3`
inherit its own branch's agent: `#t1@a1>#t3@a1, #t2@a2>#t3@a2`.

`+` is different: a join does not decompose — that is the entire point of an
explicit join, every joined branch's output reaching one downstream step as
pins (see "Squid Flow" below — never a synthesized envelope). So an omitted-half
step downstream of a join has no single root to inherit from unless the
joined origins agree. `#t1@a+#t2@a>#t3` is unambiguous (both agree the agent
is `a`, so `#t3` resolves to `#t3@a`). `#t1@a1+#t2@a2>#t3` is **not** valid:
the joined origins disagree, there is no single root value for `#t3` to
inherit, and Squid must reject it rather than guess — the user must resolve
the target's topic/agent explicitly: `#t1@a1+#t2@a2>#t3@a1` (or whichever
agent the joined step should actually run as).

`!` on a chained route step means fresh native workflow session for that step.
It is a boolean suffix and does not accept a numeric argument.

`<>` and `<N>` are the same request/response operator family: `<>` means one
round, and `<N>` means `N` rounds.

When a repeated step uses `!`, each execution of that step starts its own fresh
native session. Therefore `#squid@codex<2>@review!` uses two independent fresh
`@review!` sessions; review state passes between rounds only through the chain
envelope.

## Squid Flow

The workflow graph feature is named **Squid Flow**. User-facing docs and UI
should use "Squid Flow" rather than "workflow graph" or "flow graph". Route
chains are the first supported subset of Squid Flow. A linear chain is a graph
with one active path and no joins, cycles, or scheduled edges. The same
execution model should handle both simple chains and fuller Squid Flow
expressions.

In the UI, a Squid Flow linear-chain start marker is shown above the originating
user message in live history and restored history. The marker displays the route
itself, without a `Squid Flow:` text prefix, and colors route pieces using the
same route tag styling as other route chips. When persistent lanes are involved,
the marker can show per-agent turn-count snapshots; the composer route chip does
not show a single aggregate count because multiple agents may be involved.

Squid Flow expressions list directed lineage clauses:

```text
#topic@origin>@review!;@origin>@test!;@review!+@test!>@origin
```

The intended reading would be:

```text
#topic@origin > @review!
@origin > @test!
@review! + @test! > @origin
```

The first full route establishes the topic for the graph expression. Later bare
`@agent` steps inherit that topic, so `@review!` means `#topic@review!`. A later
step may instead write its own full `#topic@agent`, which switches the active
topic from that step onward — bare `@agent` steps after it inherit the new
topic, not the original one. This is the same escape hatch as chain targets
(see "Accepted Route Syntax"), extended to graph clauses generally.

The first origin must be a full `#topic@agent` — both halves explicit, no
inheritance. There is no ambient fallback inside a graph clause (ADR-0005's
sticky-agent lookup does not apply here; see "Accepted Route Syntax"), and
inheritance only ever pulls the omitted half from an already-resolved root.
The first origin *is* what establishes that root, so it has nothing to
inherit from yet. A bare `@agent` or bare `#topic` as the first origin is
invalid.

`;` separates lineage clauses. `>` and `=>` remain directed handoffs. `+` is
an explicit join operator: the downstream step runs after every listed
upstream step has completed. Context reaches the downstream step by **pinning
each joined branch's final message** into that turn — the same
pin-injection mechanism Squid already uses elsewhere (`pinned_ids`,
`lookback_via_pins` in `agent/server.py`; `_injectablePinnedIds` in
`ui/app.js`) — not a synthesized envelope specific to joins. A two-way join
(`@a+@b>@c`) pins exactly two messages into `@c`'s turn, one per branch;
an N-way join pins N. This is deliberately not the one-way/round-trip
envelope described under "Downstream Prompt Synthesis" below: that envelope
exists to hand a *single* previous step's output to the next step as an
explicit prompt, because there is no other channel carrying it there. A join
has no single previous step to describe that way, and the branches' actual
outputs are already available to the downstream turn via the pins — so
Squid still sends a handoff prompt (route, original user prompt), but does
not also try to textually concatenate multiple outputs into one synthesized
`<previous_step_output>` block.

Implicit joins are not part of Squid Flow syntax. For example:

```text
@review!>@origin;@test!>@origin
```

would mean two independent executions of `@origin`, not one joined execution.
A true join must be written explicitly:

```text
@review!+@test!>@origin
```

### Clause Separator: `;` Between Clauses, `,` Within a List

`,` is reserved for a list of peers that share one edge or one origin
declaration; it never separates independent clauses. This is one rule applied
in two positions, not two grammars: `,` always lists items that share one
input, and `;` always separates items whose inputs differ.

```text
#topic@a,@b;@a>@c;@b>@d;@c+@d>@e
```

Reading:

```text
@a and @b are independent origins on the literal user prompt
@a > @c
@b > @d
@c + @d > @e   (join: @e runs once both @c and @d complete)
```

Within a single `;`-delimited clause: if the clause has no operator, `,` lists
parallel origins (see Origin Broadcast below). If the clause has an operator,
`,` after `>` lists parallel targets fed by the same source, e.g. `@a>@b,@c`
means `@a`'s output goes to both `@b` and `@c` independently, with no join
between them. Target fan-out is in v0.1 scope (see "v0.1 Scope" above) — it
doesn't add hop depth, just parallel targets at the one hop v0.1 already
allows.

An earlier draft of this ADR used `,` for both roles. That was ambiguous: a
comma-separated clause opening with a bare `@agent` could be read either as a
new independent clause or as a continuation of the previous clause's target
list, and it could silently mean either a parallel fan-out or a sequential
continuation depending on which node the reader assumed the omitted source
was. Splitting the two roles across `,` and `;` removes that ambiguity: a bare
`@agent` list can never start mid-expression, only as an origin declaration.

### Origin Broadcast (Multiple Origins)

A clause with no operator is a bare, comma-separated agent list. Each listed
agent receives the literal user prompt directly, unwrapped, as an independent
origin:

```text
#topic@a,@b
```

`@a` and `@b` each run as independent origins on the same literal prompt;
neither receives the other's output and no chain envelope is created for
either. This is equivalent to sending `#topic@a` and `#topic@b` separately
with identical text, displayed as one Squid Flow start instead of two
unrelated messages — useful for asking multiple agents the same question for
comparison.

A bare no-operator clause is only legal as one of the graph's origin clauses.
Every downstream clause requires an operator (`>`, `+`, `<>`, `<N>`), so a bare
list can never be confused with a downstream step.

Autocomplete should prefer `!` on broadcast origins (`@a!,@b!`), for the same
reason it prefers `!` on chained request/response targets: comparing agents
usually wants independent fresh takes rather than each mutating its persistent
lane.

### Canonical Key (Storage/Dedup Identity)

Distinct from the human-readable `canonical` form above (which is
order-preserving and meant to be read back), a comma/plus-separated group also
has a condensed **key** form: a write-only identity token used for storage and
dedup ("is this the same broadcast/join as one we've already recorded"),
never re-parsed as flow syntax by anything. Because it's write-only, it's free
to do what the readable form can't — resolve every atom first (respecting the
order-sensitive rolling anchor, so that step alone still happens in original
order), then reorder and group the resulting *already-resolved* (topic,
agent) facts, since a fully-resolved atom has no positional ambiguity left to
preserve.

The grouping rule is not a single fixed sort axis. Sorting and grouping by
topic alone (drop a topic that repeats the previous entry's) leaves savings on
the table whenever agents repeat more than topics do in a given list, and
grouping by agent alone has the same blind spot in reverse — neither axis
dominates the other in general, and a single-axis rule also cannot mix which
field it drops within one list. Instead: repeatedly pick whichever remaining
atom would "cover" the most other remaining atoms, where one atom covers
another if they share a topic *or* an agent. That atom becomes a run's
anchor, written in full (`#topic@agent`); every atom it covers joins the run,
each dropping whichever one field it shares with the anchor. Remove the run
and repeat on what's left. Ties (equal coverage) break on ascending
`(topic, agent)` so the result stays a pure function of the atom set —
same set in, same key out, which is the actual requirement ("canonicalization
must produce exactly one shape per meaning," above); nothing about that
requirement demands a single sort axis, only that the function be
deterministic. This is a greedy approximation of minimum dominating set
(NP-hard in general) — not guaranteed globally optimal, but cheap and exact
in practice for the small atom counts a broadcast or join list actually has,
and it always matches or beats either single-axis sort.

Worked example: `#t2@a2,#t1,@a1` resolves (rolling anchor) to
`#t2@a2, #t1@a2, #t2@a1`. `#t2@a2` covers both other atoms (shares agent `a2`
with the second, shares topic `t2` with the third) and wins the coverage tie,
so it anchors the only run: the key is `#t2@a2,#t1,@a1` — the second atom
drops its agent (matches the anchor's), the third drops its topic (also
matches the anchor's). Reference implementation: `ui/flow-lang.js`
(`minimalGroupedText`, used by both `keyOriginGroupText` for origin/join
lists and `keyGroupText` for hop-target lists — the same rule applies
wherever a comma/plus list appears, not just at the root; see "Rolling anchor
is specific to origins, and only origins" above for why *resolution* still
differs between an origin list and a hop-target list, even though this
grouping step doesn't).

### Related Prior Art

Squid Flow's shape mirrors existing DAG expression conventions rather than
inventing a new one:

- Graphviz DOT separates edge statements with `;` (or newlines) and groups
  nodes with `{a b}` for fan-out/fan-in, e.g. `a -> b; a -> {c d};`.
- Apache Airflow's DAG bitshift operators compose the same fan-out/fan-in
  shapes with lists: `task >> [b, c]` (fan-out) and `[b, c] >> task` (join).
- Mermaid flowchart syntax uses `-->` for edges and `&` to chain multiple
  sources/targets on one statement, with newlines separating statements.

All of these use one token for "new edge/clause" and a different token for "a
list of peers on the same edge." Squid Flow's `;` and `,` follow the same
pattern instead of reusing one delimiter for both. A bracket-grouped
alternative closer to Airflow/DOT (`[@a,@b]>@c`) was considered and set aside
for now to keep expressions terser and consistent with Squid Flow's existing
single-character operators (`>`, `+`, `<>`, `<N>`); it remains an option if
comma/semicolon overloading proves confusing in practice.

The same operator family supports bounded request/response loops and scheduled
one-way handoffs:

```text
@a<5>@b
@a<5:1d>@b
@c=2>@a
@c=2:1d>@a
@c=5:1d>@a
```

`<N:T>` means run `N` request/response rounds with the next route step, waiting
duration `T` between every leg — the out-leg and the return-leg of each round
alike, symmetrically in both directions, not just between successive rounds.
(This is the one place in this ADR where "hop" and "leg" need to be kept
apart: `<N:T>` is one hop — the clause's single operator, same as any other
— but that one hop dispatches `2N` legs, alternating target/origin. "At most
one hop operator" elsewhere in this document is a grammar-depth rule, not a
cap on how many messages that hop sends; see the Squid Flow v0.1 Whitepaper,
"Hop vs. leg," for the full distinction.) A round is two legs (out, back), so
`N` rounds are `2N` legs. Matching the scheduled edge's rule below (every
repeat, including the first, is a full `T` after the trigger — never
immediate), leg `k` fires at `k*T` after the origin: leg 1 (round 1's
out-leg) is `T` after the trigger, leg 2 (round 1's return) is `2T` after
the trigger, and so on through leg `2N`. `<1:T>` is therefore not
degenerate: its one round still has two legs, at `T` and `2T`.
If the two directions need different delays, that isn't expressed on
`<N:T>` — write the round out explicitly as chained one-way edges instead,
e.g. `@a=1:1h>@b=>@a` (an hour out, no delay back) or `@a=1:1h>@b=1:2h>@a` (a
different delay each way). There is no unbounded `<*:T>` form: a round trip's
natural stopping point is semantic (e.g. "until the reviewer approves"), which
Squid Flow has no conditional edges to express, so an unbounded agent-to-agent
loop has no way to terminate and is intentionally not supported — use an
explicit bounded `N` instead.

`=>` is equivalent to `>`. The count always comes first, same as `<N>` —
`=N>` repeats the one-way handoff `N` times back to back
with no delay. `=N:T>` spaces those `N` repeats out by duration `T`, relative
to the triggering completion time. Every delayed run, including the first, is
a full `T` after the previous one (or after the trigger, for the first) — run
`k` fires at `k*T` after the trigger. Unbounded scheduled repeats (`=*`) are
not part of v0.1; use an explicit bounded `N`.

There is no wall-clock anchor (e.g. "run at 14:00 daily") — a schedule is
always a relative offset from the trigger, never pinned to a time of day.
An anchor attribute was drafted for this ADR but deliberately dropped before
implementation: it would need to exist consistently on both `=N:T>` and
`<N:T>`, and there was no use case compelling enough to justify designing and
maintaining that consistently across both operators.

The count/wait sit on the edge, but they only govern *reaching the target*
repeatedly — the origin side of that edge already ran once (whatever
triggered the edge in the first place) and is not itself re-run or delayed.
So for `@c=5:1d>@a`, `@c` runs immediately, once; `@a` is what fires five
times, one day apart.

Unbounded graph cycles are invalid. A cyclic Squid Flow must include an
explicit bound or schedule, such as `@c=5:1d>@a`. The v0.1 implementation uses
in-memory delayed dispatch for `:T`; persisted pending handoffs are a later
durability improvement, not part of v0.1.

The v0.1 implementation supports the single-operator subset while using the
same Squid Flow representation internally. Cyclic and multi-clause graph flows
can land incrementally without redefining the route syntax.

Complex graph expressions should also have a named alias form so the composer
does not need to display or edit the full lineage list for common workflows.
The convention is to put `.flow` in the agent position, as an agent-name suffix:

```text
#topic@review.flow
```

The suffix keeps autocomplete name-first: users type the meaningful Squid Flow
name, such as `review`, in the same place they would type an agent, then choose
the `.flow` type. A future config might define the alias as:

```yaml
flows:
  review:
    route: "@codex>@review!;@codex>@test!;@review!+@test!>@codex"
```

In this form, `#topic@review.flow` would expand to the configured Squid Flow
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
| `>#other@review` chained step | Switch to `#other`'s existing resumable `(other, review)` session; later bare `@agent` steps inherit `#other` |
| `>#other@review!` chained step | Switch to `#other`, start a fresh native session for that step |
| `>#other` chained step (no agent) | Inherits the root's agent, switched to `#other` — equivalent to `>#other@<root-agent>`, not `#other`'s sticky agent |

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
- Future versions may include a generic collaboration contract: follow the
  downstream agent's configured instructions when they define a specific role;
  otherwise act as an independent collaborator on the original request using
  the previous output as explicit context.

Implemented one-way shape:

```text
Squid route chain handoff.
Route: #squid@codex>@review!
Previous step: @codex
Current step: @review!
Original prompt: implement the feature

<previous_step_output>
...
</previous_step_output>
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

Example return shape (target direction, not yet implemented — the current
`<>` return step reuses the same one-way handoff template shown above under
"Implemented one-way shape", with `Previous step`/`Current step` swapped back
to the origin):

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

For the implemented UI-driven linear chain, the target step is started only
after the origin assistant output has completed and produced final content. A
failed or empty origin output therefore leaves only the origin turn visible and
does not synthesize the target handoff.

Scheduled flow edges use the same fail-stop default. If the source step fails,
no scheduled handoff is created. If a scheduled target run fails, Squid marks
that scheduled edge failed and does not enqueue remaining repetitions. For
example, if `@c=5:1d>@a` fails on the third `@a` run, the fourth and fifth
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
`#squid@codex<2>@review!` is the accepted future form for two request/response
rounds with fresh review sessions.

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
- Repeated chain execution is not implemented yet; when `<N>` lands, every
  repeated chained `>@agent!` execution must get a separate fresh native
  session.
- Numbers after chained `!` are unsupported.
- If any step fails, the chain stops and later steps are not run.
- Route autocomplete supports the implemented one-way forms and should grow to
  support `#squid@codex<2>@review!` when request/response loops land.
- No Squid-owned roles or profile registry are added.

Fresh chain sessions are ephemeral workflow sessions. They should not update or
replace the persistent `(topic, agent)` session row used by normal direct
messages.

`/clear` remains scoped to the origin lane in a route expression. For example,
`#squid@codex>@review /clear` clears `#squid@codex`, not both lanes. Clearing
the target lane requires an explicit target route such as `#squid@review /clear`.
The generic single-session `/clear` advisory is suppressed on Squid Flow route
chips because one turn count or one clear target is misleading when the composer
route contains multiple agents.

## Consequences

- Good: enables cwd-profile review agents without adding Squid role concepts.
- Good: autocomplete can expose the implemented workflow now and grow into the
  full Squid Flow syntax incrementally.
- Good: chained reviews are deterministic because they consume explicit chain
  input.
- Good: bare `@agent` consistently means the persistent `(topic, agent)` lane.
- Good: `>@agent!` gives a fresh one-way handoff without global recent-message
  lookback; `<>@agent!` extends that model when request/response loops land.
- Good: `;` as the clause separator and `,` reserved for same-input peer lists
  (targets after `>`, or origins in a bare broadcast clause) removes the
  ambiguity between independent clauses and continuation of a prior clause.
- Bad: chained execution needs fresh-session handling that is separate from
  both resumed sessions and adhoc lookback.
- Constraint: `!N` remains valid for standalone adhoc turns only. In chained
  route steps, `!` never takes a number.
