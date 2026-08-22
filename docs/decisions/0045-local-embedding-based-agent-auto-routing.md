---
status: proposed
date: 2026-08-22
updated: 2026-08-22
---
# ADR-0045: `@auto` — Usage-Pattern-Based Single-Hop Agent Resolution

## Context and Problem Statement

ADR-0032 lets a user write an explicit route chain, e.g.
`#squid@codex>@review!`, naming every hop by hand. That's deterministic, but
it means the user must already know which agent should run each message. As
the per-topic agent pool grows (multiple coder profiles, multiple reviewer
profiles, a local-model agent for cheap/offline work, ...), typing the right
target on every message doesn't scale.

Two concrete cases motivate this ADR, both **single-hop** — v0 of `@auto`
resolves one message to one agent, nothing more:

1. `#squid@auto push the changes` — this topic's own history should already
   show that "push the changes"-shaped prompts get sent to `@opencode!`
   (adhoc, fresh session) in this topic specifically. `@auto` should
   reproduce that pattern, including the adhoc-ness, not just pick a
   plausible agent name.
2. The user selects/replies to a message that carries a code diff and sends
   a short, generic follow-up like "thoughts?" or "review" to `@auto`. The
   fact that the reply target has a diff attached, combined with a
   short/generic prompt, should shift resolution toward a review-flavored
   agent — plausibly a *different model* than whatever produced the diff, or
   the same model under a distinct reviewer cwd-profile — not toward another
   coder-flavored agent just because the literal words are vague.

An earlier draft of this ADR reached for `@auto>@auto` — `@auto` deciding
its own downstream chain/graph. Set that aside for v1 (see Non-Goals): it's
real added complexity (a new conditional-edge primitive, chain-generation
logic) and isn't needed to solve either case above, which are both about
resolving one message well using the topic's own history, not about
generating a workflow graph.

This also has to be reconciled with a decision ADR-0032 already made
deliberately:

> Keep route chains as routing syntax only; the route must not make Squid
> infer agent-specific roles such as reviewer, summarizer, tester, or
> implementer.

`@auto` must not become Squid quietly inventing a "reviewer" concept. The
signal driving resolution is the topic's own real history of (context →
agent the user actually picked) — plus user-authored agent descriptions for
cold start — never a taxonomy Squid ships or infers on its own.

## Non-Goals

- **No chain/graph generation in v1.** `@auto` resolves exactly one message
  to one atom (agent + session-type + optional model). It does not decide
  `@auto>@auto`, does not synthesize a downstream hop, and does not gate on
  any post-turn predicate (e.g. "did this produce a diff"). Whether `@auto`
  should ever expand into a multi-step chain on its own is deferred — see
  Future Work.
- No general conditional-edge / boolean-expression language for Squid Flow.
  Not needed since v1 has no chain generation at all.
- No cloud router tier by default. Routing must work fully offline against
  local embeddings; a hosted cheap-tier model (Haiku, etc.) is an optional
  per-topic override for the rare tiebreak case, not the baseline.
- No Squid-owned role registry ("coder", "reviewer", "tester" are not
  concepts Squid defines or hardcodes anywhere).
- No change to explicit route semantics. `@auto` is one more atom kind
  alongside a literal `@agent`; once it resolves to a concrete
  `(agent, adhoc, model)`, everything downstream (ADR-0032's session
  semantics, stats, etc.) behaves exactly as if the user had typed that atom.

## Decision Drivers

- **Learn from the topic's own real usage, not just static self-authored
  descriptions.** The same literal prompt should route differently over
  time as the user's own past choices establish a pattern in that topic —
  "push the changes" going to `@opencode!` is a fact about this topic's
  history, not something any agent's description would say.
- Reuse existing mechanisms rather than build parallel ones: topic memory
  frontmatter (ADR-0020) for config, the provider/harness split (ADR-0028)
  plus the Ollama/local-provider queueing and lifecycle machinery (ADR-0037,
  ADR-0043) for whatever model computes embeddings or breaks ties, and
  `chat_messages`' existing `agent`/`adhoc`/model columns (already read by
  `stats_db.py`, e.g. `last_model`/`last_harness`/`last_provider` on
  `topics`) as the historical corpus — no new log, no separate store of
  "what happened before."
- Cheap and fast by default. A similarity lookup against a topic's own past
  turns and a handful of agent-description vectors is sub-10ms; that must be
  the default path, with any LLM call reserved for genuine ties.
- Fully user-configurable and opt-in per topic, matching this repo's
  existing pattern of putting topic-scoped structured config in topic memory
  frontmatter rather than a separate settings surface.
- Auditable. A user must be able to see why `@auto` picked a given atom, and
  which past turn (if any) it matched against.

## Considered Options

### Option A: Route every `@auto` prompt through a small local LLM classifier

Send the prompt plus candidate agent descriptions to a local
Qwen2.5-1.5B-class model (via the existing Ollama/local-provider path,
ADR-0037), ask it to pick one.

Good: flexible with vague or compound prompts; one mechanism for every case.
Bad: 100s of ms to seconds even locally, on every `@auto` message;
nondeterministic; has no natural way to weight "this exact topic has done
this before" over a generic reading of the words.

### Option B: Static agent-description embedding only

Embed the prompt and each candidate agent's description; cosine-match.

Good: sub-10ms, deterministic, simple.
Bad: has no memory. It can't produce this ADR's motivating case at all —
nothing about a static "runs shell commands, pushes branches" description
for `@opencode` gets stronger just because this topic has actually sent
"push the changes" to it ten times before. Description quality alone caps
how well this generalizes, and it never improves with usage.

### Option C: Historical usage-pattern matching (nearest neighbor over past resolutions), static description embedding as cold-start fallback

Build the match set from this topic's own past turns where the user
explicitly typed a concrete atom (not from prior `@auto` outputs — see
"Training-set policy" below): embed each historical prompt (plus its reply
context, if any) once, tagged with the atom the user actually chose
(`agent`, `adhoc`, `model`). For a new `@auto` prompt, embed it the same way
and find the nearest historical neighbor. If its similarity clears a
threshold, resolve to that neighbor's atom. If the topic has too little
history yet (cold start) or no neighbor clears the threshold, fall back to
Option B's static description match.

Good: directly produces the motivating case — "push the changes" converges
on `@opencode!` because that's what actually happened before, not because
any description says so; degrades gracefully to Option B when there's no
history yet; still sub-10ms (nearest-neighbor over a small per-topic set is
as cheap as Option B's candidate match); the corpus is derived entirely from
data Squid already persists (`chat_messages`), so there's no new source of
truth to keep consistent.
Bad: needs a real policy for which past turns count as training examples
(see below), or it risks reinforcing an early wrong `@auto` guess; a topic
with no history and thin agent descriptions has a genuine cold-start gap
where neither signal is strong.

### Option D: Keyword/regex rules only, no embeddings or model at all

Good: fully deterministic, zero latency, zero model dependency.
Bad: brittle — every new phrasing needs a new rule; can't pick up the
reply-context signal (case 2) at all, since that's a similarity judgment
over prior examples, not a pattern a regex can express.

## Decision Outcome

**Option C**, layered under Option D's rules and above Option B's fallback
and an LLM tiebreak of last resort:

1. **Rules first.** A topic's `squid.routing.rules` (see config below) are
   evaluated in priority order. If one matches, `@auto` resolves immediately
   — no embedding call at all. Keeps the case the user already knows they
   want both instant and fully auditable.
2. **Historical nearest-neighbor next.** Embed the new prompt plus its reply
   context (below) and compare against this topic's own past
   explicitly-typed turns. If the closest one clears the similarity
   threshold, resolve to its exact atom (agent, adhoc, model).
3. **Static description fallback.** If there's no history yet, or nothing
   clears the threshold, cosine-match against candidate agents' own
   descriptions (Option B), same as a topic using `@auto` for the first
   time.
4. **LLM tiebreak last, only on a genuine tie** between the top two
   candidates from whichever of steps 2/3 produced a result. Uses a
   configured local or hosted-cheap-tier model on just the tied candidates,
   not the full pool. No tiebreak model configured → fail closed, ask the
   user to disambiguate.
5. **Resolution target is a full atom, not just an agent name**: `(agent,
   adhoc: bool, model: optional override)`. Matching only the agent and
   defaulting session-type would silently lose exactly the distinction the
   first motivating case depends on (`@opencode!` vs plain `@opencode`).
6. Both the embedding model and any tiebreak LLM run through the existing
   local-provider path (ADR-0037/0043) — a local embedding model
   (`bge-small`/`nomic-embed-text` class, CPU-only, ~100-300MB) and,
   optionally, a small local instruct model or configured hosted cheap-tier
   model for tiebreaks. No new model-serving mechanism.

### Context features beyond the literal prompt

Case 2 (reply-to-review) needs more than the prompt's own text: "thoughts?"
alone is nearly content-free. The embedding input for a prompt is therefore
the prompt text plus, when the message is a reply to / has a selected
target:

- whether the target message carries a code diff (boolean, already known
  from ADR-0020/0025's per-turn diff data — no new computation);
- the target's own `(agent, model)`, so the historical match can prefer
  "user replies to a diff from `@codex` with a short prompt → historically
  sends it to `@review-gpt`" style patterns, distinct from "short prompt,
  no diff in context" patterns.

This is still embedding *text* — the diff-present flag and target
agent/model are folded into the string fed to the embedding model (e.g. a
short structured prefix), not a separate scoring dimension — keeping this
one mechanism rather than a second scoring path bolted on for replies.

### Training-set policy: only explicit resolutions, not prior `@auto` outputs

Open but decided-by-default: the historical corpus indexes only turns where
the user typed a concrete `@agent` themselves, not turns `@auto` previously
resolved. Rationale: an early wrong `@auto` guess would otherwise become
"history" that reinforces itself — the second wrong guess would look
consistent with the first, not corrected by it. `@auto`-resolved turns are
still fully visible and still recorded in stats (below), just excluded from
the nearest-neighbor index by default. If a user never corrects an
`@auto` choice, that's weaker evidence of intent than a message they typed
by hand; this can be revisited (e.g. count an uncorrected `@auto` turn
after N days as implicitly confirmed) if the strict version proves too
data-starved in practice, but the strict version is the safer starting
default.

### Stats attribution

Every `@auto`-resolved turn's stats row must record that it was
auto-invoked — e.g. an `invoked_via: "auto"` flag alongside the existing
`agent`/`adhoc`/model columns already on `chat_messages` — attributed
against the resolved agent (so `@opencode`'s stats correctly count an
auto-routed adhoc turn as its own activity, not as some separate `auto`
pseudo-agent). This is required for two reasons, not just observability:

- The user needs to be able to see, per turn, that `@auto` made the call and
  what it resolved to.
- The training-set policy above depends on being able to tell "the user
  typed this atom" apart from "`@auto` picked this atom" when building the
  nearest-neighbor index — without the flag, the two are indistinguishable
  in `chat_messages` and the anti-feedback-loop policy can't be enforced.

### Per-topic routing config in topic memory frontmatter

Extends the existing `squid:` block (ADR-0020 precedent: `code_roots`) with
a `routing:` key:

```yaml
squid:
  code_roots:
    - /absolute/path/to/repo
  routing:
    candidates: [codex, opencode, review-gpt]   # pool @auto may resolve into
    rules:                                       # evaluated in order, before history/embeddings
      - match: '^(fix|debug)\b'
        agent: codex
    tiebreak_agent: haiku-router                 # optional; omit to fail closed on ties
    tie_margin: 0.03                             # optional override of the default
    history_min_examples: 3                      # below this, always fall back to description match
```

Omitting `routing` entirely means `@auto` is not available for that topic —
opt-in per topic, not a global background feature Squid turns on unasked.

### Agent description requirements (cold-start path only)

Only matters for step 3 (no history yet). A one-line functional summary
("runs shell commands, pushes branches") under-discriminates; descriptions
used for `@auto` cold start should include a couple of concrete example
prompts. Once a topic has real history, step 2 dominates and description
quality matters less — this requirement fades in importance as a topic
accumulates usage.

### Quota/budget as a routing input, not a model question

Remaining quota/budget (per provider or per agent) is read as a structured
fact and applied as a deterministic filter on the candidate pool before
history/embedding matching runs — e.g. "exclude a candidate whose provider
is over budget" — never handed to any model to reason about.

## Future Work (explicitly deferred, not v1)

- **`@auto>@auto` / `@auto`-generated chains.** Letting `@auto` decide its
  own downstream graph (e.g. auto-triggering a review hop only when the
  producing turn's diff is non-empty) is a real idea but adds a new
  conditional-edge primitive to Squid Flow (which has none today) on top of
  chain-generation logic — real complexity this ADR doesn't need to take on
  to solve either motivating case. Revisit once single-hop `@auto` has real
  usage data to learn from.
- **Confirmed-but-uncorrected `@auto` turns feeding the training index.**
  Noted above as a possible loosening of the strict "explicit-only" policy.

## Consequences

- Good: directly solves both motivating cases — usage-pattern reproduction
  ("push the changes" → `@opencode!`) and reply-context-sensitive resolution
  (short prompt + diff-bearing reply target → a review-flavored pick) —
  neither of which a static-description-only design (Option B alone) can
  produce.
- Good: sub-10ms in the common case; degrades gracefully to description
  matching when a topic has no history yet.
- Good: the historical corpus is derived from data Squid already persists
  (`chat_messages`); the only new column is the `invoked_via`/auto flag.
- Good: stays inside ADR-0032's "no Squid-owned role registry" boundary —
  signal is the topic's own real history plus user-authored descriptions,
  never a taxonomy Squid defines.
- Good: scope is deliberately single-hop for v1 — no new Squid Flow
  primitives, no chain-generation logic to get wrong.
- Bad: cold-start topics (no history, thin descriptions) have a genuine gap
  where neither signal is strong; first-use accuracy will be lower than
  steady-state accuracy.
- Bad: the anti-feedback-loop training policy (index only explicit choices)
  means `@auto` doesn't learn from its own corrections unless a follow-up
  policy is added later — a user silently accepting a slightly-wrong `@auto`
  pick doesn't improve the model.
- Bad: reply-context embedding (diff-present + target agent/model folded
  into the embedded text) is a slightly unusual construction that needs
  care to keep deterministic and cheap to compute per message.
