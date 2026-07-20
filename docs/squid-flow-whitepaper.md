# Squid Flow v0.1 — Technical Whitepaper

This is a technical reference for how Squid Flow v0.1 actually works: grammar,
resolution semantics, branch expansion, canonical forms, and the server-side
execution model. For the *decision record* — why this design was chosen, what
tradeoffs it makes, and the boundary of what v0.1 covers versus the broader
Squid Flow direction — see
[ADR-0032](decisions/0032-route-chains-with-cwd-profile-agents.md). The two
documents are complementary: the ADR is the authoritative scope boundary and
rationale; this paper is the implementation reference for engineers building
against or extending the language.

There are two independent implementations of everything in this paper:
`ui/flow-lang.js` (parse/resolve/expand/render — used by the composer, the
in-app Flow view, and the standalone `ui/flow-playground.html`) and
`agent/flow.py` (parse/resolve/dispatch — the only one that actually executes
a chain). They are not shared code; each is a from-scratch port of the same
grammar and resolution rules, and comments in both files call out the sync
obligation (e.g. `chain_handoff_prompt`'s docstring: "Port of the UI's
chainHandoffPrompt (ui/app.js) — must stay in sync with it"). Anywhere this
paper cites one file's line numbers, assume the other has an equivalent.

---

## 1. Grammar

A Squid Flow v0.1 clause is: one **origin group**, optionally followed by
**one hop** (an operator plus a **target group**).

```
clause   := group (operator group)?
group    := atom ((',' atom)* | ('+' atom)*)      -- ',' and '+' never mix in one group
atom     := ('#' topic)? ('@' agent)? '!'?          -- at least one of topic/agent required
operator := '>' | edge N? (':' duration)? '>'
edge     := '<' | '='                             -- bidirectional vs one-way; the only structural difference
duration := digits [smhd]
```

- `topic`/`agent` match `[A-Za-z0-9_.-]+`.
- `!` marks an atom "fresh" (start a new native session instead of continuing
  the persistent one).
- `;` (multi-clause DAG) is recognized only to reject it with a clear error —
  out of v0.1 scope. See ADR-0032, "Out of scope."
- `>` is shorthand for `=>`, itself shorthand for `=1>` — the `N=1, T=none`
  case of the general `edge N? (':' duration)? '>'` production with `edge='='`
  and everything after it omitted. It's a separate top-level alternative
  (rather than reachable by omitting `edge` from the general production)
  because `edge` itself isn't optional — but semantically it's exactly the
  same edge as `=>`/`=1>`, not a third thing.

Reference implementation: `ui/flow-lang.js` `parseAtom`/`parseGroup`/`parseClause`
(`:35-177`); `agent/flow.py` `_parse_atom`/`_parse_group`/`_split_operator`
(`:47-201`).

### Edge types

There are exactly **two** edge *types*, and the grammar is now fully
symmetric between them — the only structural difference is the leading
character (`<` = bidirectional, `=` = one-way):

| Type | Canonical form | Meaning |
|---|---|---|
| **forward** | `=N:T>` (shorthand `>` for `N=1, T=none`) | One-way, from the same origin output, repeated `N` times (default 1), each optionally delayed. Terminal — nothing can follow it in a v0.1 clause. |
| **roundtrip** | `<N:T>` | Forward to the target, then send the target's response back to the origin, for `N` rounds (default 1). |

Both are parsed by the *same shape* of regex — `SCHEDULED_RE` and
`ROUNDTRIP_RE` (`ui/flow-lang.js:21,27`) differ only in their leading
character, both making count optional (`(\d+)?`) with an `N ? parseInt(N) :
1` default; `_parse_operator_token` (`agent/flow.py:164-186`) mirrors this
exactly for both types. `expand()`'s "scheduled" branch (`ui/flow-lang.js`,
comment: `// scheduled (op.type is always 'scheduled' here — '>'/'=>' are
just its count=1/wait=null case)`) is the one shared code path for every
forward edge regardless of how it was spelled; only `roundtrip` gets its own
branch, structurally the same split as the type table above. At dispatch
time, `agent/flow.py`'s `_dispatch_or_schedule` checks `delay <= 0` first and
dispatches immediately in that case, so `>`, `=>`, and `=1>` all take the
identical immediate-dispatch path — `schedule_key`/`_SCHEDULED_DISPATCHES`
are only ever populated once `delay > 0` (`next_chain_steps`'s scheduled
branch passes `schedule_key=schedule_key if delay_unit else None`, mirroring
the round-trip loop's own `schedule_key if wait_seconds else None`).

**Canonical rendering always prefers the shortest spelling** for a given
`(count, wait)`, on both types symmetrically: `opToText`/`_op_to_text`
(`ui/flow-lang.js:350-358`; `agent/flow.py:189-206`, kept in sync the same
way `chain_handoff_prompt` is) render `count=1, wait=None` as `>` (never
`=1>` or `=>`), `count=1, wait=T` as `=:T>` (never `=1:T>`), and `rounds=1,
wait=None` as `<>` (never `<1>`) — every non-canonical spelling still parses
(`=1>`, `=>`, `=1:5m>`, `<1>` are all valid *input*), it just never comes back
*out*. This is a real identity guarantee, not cosmetic: `agent/flow.py`'s
`route`/`key` (the DB dedup token, §4) is built from this canonical text, so
`#a>@b`, `#a=>@b`, and `#a=1>@b` all resolve to the identical stored
`flow_route` — they're the same workflow, not three different ones.

### Edge attributes

`N` (count) and `T` (wait) are **attributes** of both types, symmetrically —
both optional, both defaulting the same way:

| Attribute | Syntax | Forward (`=N:T>` / `>`) | Roundtrip (`<N:T>`) |
|---|---|---|---|
| count | `N` | optional, default `1` — `=>` is exactly `=1>` | optional, default `1` — `<>` is exactly `<1>` |
| wait | `:T` | optional; spaces the `N` repeats out from the trigger, `T`, `2T`, `3T`, … | optional; applied symmetrically to *every* leg, out and back alike (`<N:T>` waits `T` before each of the `2N` legs — one hop, `2N` legs; see §3's hop/leg distinction) |

So the full accepted surface is both types × their attributes:
`>`, `=>`, `=N>`, `=N:T>`, `=:T>`, `<>`, `<N>`, `<N:T>` — matching
ADR-0032's "at most one hop operator" list, plus `=:T>` (count-omitted,
wait-present), which is new: previously only reachable by writing `=1:T>`
explicitly.

---

## 2. Resolution semantics

Every atom that omits a half (`#topic` or `@agent`) has to inherit the
missing half from somewhere. The rules differ for origins and targets:

### 2.1 Rolling anchor (origins only)

An origin atom has no parent to inherit from, so within an origin *list*
every bare atom borrows from a rolling `root`. `root` starts **pre-seeded to
the first fully-explicit atom in the whole list** — so a bare atom written
*before* any explicit sibling still resolves, borrowing forward from
whichever explicit atom eventually shows up (`#t3,#t1@a1` resolves `#t3` to
`#t3@a1`, exactly as if it had been written after it) — and from there,
every fully-explicit atom encountered scanning left to right supersedes
`root` for everything after it. So a bare atom's donor is the nearest
*preceding* fully-explicit atom if one has appeared yet; otherwise, the
first fully-explicit atom in the list overall, regardless of which side of
it the bare atom sits on.

This means order only changes the resolved meaning when there are **two or
more** fully-explicit atoms that disagree, because only then does "which one
is nearest" depend on position: `#t1@a1,#t3,#t2@a2` resolves `#t3` to
`#t3@a1` (nothing has superseded the root yet), while `#t1@a1,#t2@a2,#t3`
resolves the same `#t3` to `#t3@a2` instead (`#t2@a2` already superseded the
root by the time `#t3` is reached) — same three atoms, different graph. With
only *one* fully-explicit atom in the list, reordering never changes
anything: `#t3,#t1@a1` and `#t1@a1,#t3` both resolve to the identical pair
(`#t3@a1`, `#t1@a1`) — there's only one candidate for "nearest," so which
side of it a bare atom sits on doesn't matter.

Implementation: `resolveGroupAgainstState(group, state, isOrigin=true)`
(`ui/flow-lang.js:178-221`); `_resolve_origin_group` (`agent/flow.py:73-88`).

### 2.2 Target independence

A hop-target atom's parent is fixed — the edge's source — so every target
atom resolves independently against that one parent state, never against a
sibling in the same target list, no matter how that sibling is written. A
comma target list decomposes into fully independent branches before any
inheritance happens.

Implementation: `resolveGroupAgainstState(group, state, isOrigin=false)`
(`ui/flow-lang.js:184-192`); `_resolve_target_group` (`agent/flow.py:100-108`).

### 2.3 Principle of join

**A join (`+`) always requires a downstream consumer.** `+` pins every
joined branch's final message into one later step's turn — that pinning is
the join's entire reason to exist. A join with nothing to pin *into* is
meaningless; this is the one rule that decides whether any given `+` is
legal, in any position:

- **Origin position always qualifies** — a joined origin group feeds the
  clause's one hop: `#t1@a+#t2@b>#t3` pins both `#t1@a` and `#t2@b`'s output
  into `#t3`'s turn.
- **Target position qualifies only under `<>`/`<N>`** — the round-trip's
  return leg is a real consumer, already built into one operator token:
  `#t1@a1<>#t2+#t3` dispatches `#t2@a1` and `#t3@a1` in parallel from `t1`'s
  forwarded output, waits for *both*, then fires one return turn to `t1@a1`
  pinning both. Every other operator (`>`, `=>`, `=N>`) is terminal — nothing
  can follow it in a single-clause v0.1 expression — so `+` on their target
  is never valid, full stop: `#t1@a1>#t2+#t3` is rejected regardless of
  anything else in the clause.

A bare `#topic@a+@b` with no hop after it is rejected the same way, for the
same reason: `ui/flow-lang.js` `parseClause` (`:169-171`, the origin case;
`:65-67` inside `parseGroup`, the "no consumer at all" position check for
every other target).

### 2.4 Agreement when a join's output must be inherited

An origin-side join's forward state (used if a *later* step needs to inherit
a bare topic/agent) is only defined per-field when every joined member
agrees on that field: `#t1@a+#t2@a>#t3` resolves `#t3` to `#t3@a` (agents
agree); `#t1@a1+#t2@a2>#t3` is rejected — the agent is ambiguous and Squid
refuses to guess.

A target-side join under `<>`/`<N>` has no such ambiguity to resolve: the
return leg's destination is always the fixed origin atom, already fully
known before the join is even considered, and chaining after a round-trip is
rejected at parse time — so nothing downstream ever needs to infer a field
from the joined targets.

Implementation: `finishGroupResolution` (`ui/flow-lang.js:222-239`);
`_join_forward_state` (`agent/flow.py:91-97`).

---

## 3. Branch expansion

`expand(clause)` (`ui/flow-lang.js:255-346`) turns one parsed clause into a
list of independent **branches**, each a linear sequence of resolved steps:

1. The origin group becomes either one `join` step (if `kind === 'plus'`) or
   N independent `atom` steps (comma/single) — one branch per origin.
2. For the one hop (if present), each branch's last step feeds the next:
   - **oneway/scheduled**: every resolved target atom forks its own branch
     (target fan-out) — `atom` steps, tagged with the operator (`via`).
   - **roundtrip, non-join target**: same fan-out, but each branch's new step
     is a `roundtrip` step carrying that one target plus an `appendedSeq`
     (the alternating out/return leg sequence used for rendering).
   - **roundtrip, join target** (`hop.target.kind === 'plus'`): **one**
     branch, one `roundtrip` step with `target` holding every resolved
     target atom and `join: true` — the joined targets are not decomposed,
     mirroring how an origin-side join step isn't decomposed either.

An `M`-origin × `N`-target clause (comma × comma) therefore expands to `M×N`
branches; a join on either side of a hop collapses its side back down to one.

**Hop vs. leg.** A clause has *at most one hop* — this is the grammar-level
unit ADR-0032's v0.1 scope rule counts ("at most one hop operator after the
origin"; one hop regardless of which operator it is). A **leg** is one
actual dispatch at execution time, and a single hop can produce more than
one: a forward hop with count `N` produces `N` legs (`>` is the `N=1` case:
one leg); a roundtrip hop with `N` rounds produces `2N` legs, alternating
target/origin (`agent/flow.py`'s `next_chain_steps` names this loop variable
`leg` directly: `for leg in range(op.get("rounds", 1) * 2)`, `:461`; `ui/flow-lang.js`
uses the same word, `legGroupText`/`appendedSeq`). So `#a<>#b` is **one hop,
two legs** — the hop-count restriction ("at most one") is about grammar
depth (can you chain a second operator after this one — no, in v0.1), not
about how many messages the one hop you're allowed ends up sending.
ADR-0032's own round-trip timing section calls each leg a "hop" too ("a
round is two hops," `:576`) — that's the same overloaded word for a
different, execution-level concept; read "leg" there.

---

## 4. Canonical forms

Two distinct rendered forms come out of a parsed clause — they serve
different purposes and must not be confused:

- **`canonical`** (`canonicalForBranch`, `ui/flow-lang.js:368-388`) — one
  string per branch, human-readable and re-enterable, order-preserving. This
  is what the "branch breakdown" section of the playground shows.
- **`key`** (`canonicalKeyForClause`, `ui/flow-lang.js:526-569`) — a single
  condensed, **order-independent** string built from the clause itself (not
  the expanded branches), used as the DB identity/dedup token for "same
  workflow." It is write-only: nothing in the codebase re-parses `key` as
  flow syntax.

`key` grouping is a greedy max-coverage dominating-set algorithm
(`minimalGroupedText`, `ui/flow-lang.js:432-472`; ported in
`_minimal_grouped_text`, `agent/flow.py:111-143`): repeatedly pick whichever
remaining atom "covers" the most others (two atoms cover each other if they
share a topic or an agent), write it in full as a run's anchor, and let every
atom it covers join the run dropping whichever field matches the anchor.
This beats a fixed single-axis sort (always group by topic, or always by
agent) whenever the other field repeats more, and — unlike a fixed axis — can
mix which field drops within a single run. It's a greedy approximation of
minimum dominating set (NP-hard in general), not guaranteed globally optimal,
but exact for the small atom counts a real broadcast/join list has, and it
always matches or beats either single-axis sort.

Origin-side grouping (`keyOriginGroupText`) can safely reorder atoms because
origins are already fully resolved (rolling anchor already applied) before
grouping runs. Target-side grouping (`keyGroupText`) is order-*preserving*
when it can't fully resolve every atom (branches upstream disagree on a
field a target needs) — falling back to `verbatimGroupText` rather than
guessing at a grouping for an unresolved value.

---

## 5. Execution model

The client only ever sends the **origin** turn. Everything after that is
dispatched server-side, entirely decoupled from any connected browser tab.

### 5.1 Dispatch hook

`TopicWorker` (`agent/topic_queue.py`) runs every turn to completion
independent of any client connection, persisting through to
`chat_messages`/`run_events`. `agent/flow.py` hooks into that same completion
point: the moment a step's message is marked done, the same completion hook
asks `next_chain_steps(flow_run_id)` whether there's a next step and, if so,
dispatches it in-process — no HTTP round trip, no dependency on the
originating tab staying open. A boot-time sweep (`sweep_incomplete_flows`)
resumes any chain whose last step finished with no next step ever dispatched
(server restart mid-chain, or a chain stranded by older client-driven
behavior).

The browser polls `GET /chat/flow/{flow_run_id}/steps` purely for UX — to
discover server-dispatched steps and attach to their live stream. The
chain's correctness never depends on that poll.

### 5.2 `next_chain_steps` — the per-branch state machine

For each branch in the parsed chain (`agent/flow.py:396-500`):

- **Origin gate**: every origin in the branch's `origins` list must already
  have a completed assistant row (`_origin_assistant`) before anything in
  this branch is "ready." This is the origin-side join gate — for a
  non-join branch, `origins` has exactly one member, so this degenerates to
  "has the origin turn finished."
- **`scheduled`**: dispatch whichever of the `count` repeats haven't been
  sent yet, each independently delayed by `wait * repeat_index`.
- **`oneway`**: dispatch the target if it hasn't been sent yet after the
  origin. Terminal — nothing further happens in this branch.
- **`roundtrip`**: walk legs `0..rounds*2`, alternating
  `leg_targets = branch["targets"]` (even legs — every joined target, or
  just the one non-join target) and `[origin]` (odd legs, always singular).
  For each leg:
  - Look up every leg member's completion (`_system_assistant`). If **all**
    are done, advance: `current_previous_ids` becomes every member's
    assistant id (pinning all of them, mirroring the origin-side join gate),
    and move to the next leg.
  - If **any** are still pending, dispatch whichever of them haven't been
    sent yet (in parallel — this is the fan-out for a joined target) and
    stop for this call; a later completion re-triggers `next_chain_steps`
    and this leg is re-evaluated.

This is a genuine barrier/join-gate on the target side, symmetric to the
existing origin-side one — same "don't advance until everyone here is done"
shape, just applied to a hop's target group instead of the clause's origin
group. There is no other join-gate in the codebase; a target-side join
outside `<>`/`<N>` has nowhere to acquire one (see §2.3).

### 5.3 Prompt construction

`_step`/`chain_handoff_prompt` build the actual turn sent to each dispatched
target: route, previous step's label (`previous_agent` — `"@t2+@t3"` for a
joined leg, mirroring the origin-join label format), previous message id(s),
and the original user prompt. A join's previous output reaches the
downstream turn via **pins** (`pinned_ids`/`lookback_via_pins` in
`agent/server.py`; `_injectablePinnedIds` in `ui/app.js`) — one pin per
joined branch's final message — not a synthesized envelope. This is
deliberately different from the one-way/round-trip handoff, which *does*
synthesize a `<previous_step_output>` block, because a single previous step
has no other channel carrying its output forward. A join's branches already
have theirs available via the pins.

### 5.4 Completion detection

`expected_row_count(route)` (`agent/flow.py:520-541`) is a heuristic upper
bound on total message rows, used only for client-side "is this chain
probably done" polling (`GET /chat/flow/{flow_run_id}/steps`) — it does not
gate dispatch. Per round-trip branch: every target in `branch["targets"]`
contributes 2 rows (user+assistant) per round, plus 2 more for the return
leg: `rounds * (len(targets) * 2 + 2)`. This reduces to the pre-join-target
`4 * rounds` when there's exactly one target.

---

## 6. Worked examples

| Route | Reading |
|---|---|
| `#squid@codex>@review!` | One-way handoff: `#squid@codex`'s output becomes fresh `@review`'s prompt. |
| `#t1@a,#t2@b<>#c,#d@x` | 2 origins × 2 targets, one shared round trip — 4 independent branches. |
| `#t1@a+#t2@b>#t3` | Origin-side join: `#t3` runs once, after both `#t1@a` and `#t2@b` complete, with both pinned into its turn. |
| `#t1@a1<>#t2+#t3` | Target-side join under round-trip: `#t2@a1` and `#t3@a1` run in parallel from `#t1@a1`'s output; once both finish, one return turn to `#t1@a1` pins both. |
| `#squid@codex=3:1d>@review` | Scheduled: 3 independent one-way repeats to `@review`, one per day. |

---

## 7. See also

- [ADR-0032](decisions/0032-route-chains-with-cwd-profile-agents.md) — scope
  boundary, decision drivers, and the accepted-syntax history this paper
  assumes as background.
- `ui/flow-playground.html` — standalone, dependency-free interactive
  playground for the grammar in this paper.
- `ui/flow-lang.js` / `agent/flow.py` — the two reference implementations.
