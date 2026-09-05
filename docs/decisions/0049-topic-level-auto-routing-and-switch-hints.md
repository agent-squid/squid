---
status: proposed
date: 2026-09-04
---
# ADR-0049: Topic-Level Auto-Routing and Switch Hints

## Context and Problem Statement

ADR-0045 resolves `@auto` to an agent *within a topic already known to be
correct*. It deliberately keeps that scope narrow: same code roots, same
memory, same history either way, so a wrong pick is cheap to recover from.

There's a related but distinct problem ADR-0045 doesn't touch: figuring out
which **topic** a message belongs to at all. Two concrete cases:

1. **Topic-less entry.** The user wants to type into one place — "just talk
   to the meta harness" — without first picking a topic. A prompt like "any
   update on the auto-routing ADR?" should land in the topic that's actually
   about that work, not force the user to navigate there first.
2. **Mid-conversation drift.** The user is sitting in topic A but the prompt
   content matches topic B's own usage pattern or subject matter (e.g. asking
   a `shore`-broker question while the active topic is `squid`'s UI work).
   Squid could notice the mismatch instead of silently answering in the wrong
   context.

This is not a bigger version of ADR-0045's candidate pool. Crossing a topic
boundary changes the code roots (ADR-0020), the injected memory, and the
entire history a downstream `@auto` would match against. A wrong topic guess
is expensive in a way a wrong agent guess inside the same topic is not, so it
needs its own decision, its own confidence handling, and — per the guardrail
below — cannot reuse ADR-0045's "resolve silently or fail closed" shape
unmodified.

## Non-Goals

- **No silent cross-topic auto-switch by default.** Unlike ADR-0045's
  within-topic resolution, a high-blast-radius wrong guess (wrong code roots,
  wrong memory, wrong history) must not happen invisibly. See Decision
  Outcome for the confidence-tiered alternative.
- **No Squid-owned topic taxonomy.** Same boundary ADR-0045 draws for agent
  roles: Squid does not invent categories like "work topic" or "personal
  topic." The signal is a topic's own user-authored memory/description plus
  its own real usage history — nothing Squid infers or ships as a fixed
  ontology.
- **No chain generation, still single-hop.** This ADR decides one thing: which
  topic a message belongs to. It hands the message to that topic's normal
  `@auto` (ADR-0045) or explicit-atom resolution unchanged. It does not chain
  "resolve topic → resolve agent → resolve next hop" into one combined
  decision; that composition falls out naturally from the two ADRs being
  separate stages, not from new chain-generation logic.
- **Not a replacement for manual topic switching.** Purely additive — a user
  can always ignore a hint and switch (or not) by hand as today.

## Decision Drivers

- Reuse ADR-0045's resolution mechanism (historical nearest-neighbor over
  embeddings, static-description cold start, LLM tiebreak/fail-closed) rather
  than build a second, parallel routing system.
- Confidence must gate autonomy level. Because a wrong topic guess costs more
  than a wrong agent guess, the same "resolve or fail closed" binary ADR-0045
  uses is insufficient here — there needs to be a middle tier that surfaces a
  hint without acting on it.
- A topic-less entry surface is required for "just talk to it" to mean
  anything; without one, this ADR only helps with in-topic drift detection
  (case 2), not cold entry (case 1).
- Auditable and opt-in per topic, matching ADR-0045 and ADR-0020's existing
  pattern of topic-scoped, user-visible config.

## Considered Options

### Option A: Flatten the agent pool across all topics inside ADR-0045's `@auto`

Let `routing.candidates` reference agents in other topics; resolve straight
to an agent, implicitly carrying its topic along.

Bad: conflates two different resolution problems. An agent atom alone
doesn't carry which topic's code roots/memory/history the turn should run
under, so this either silently smuggles a topic switch inside what looks
like an agent pick, or requires bolting topic identity onto every atom —
real complexity ADR-0045 was deliberately scoped to avoid.

### Option B: A separate topic-resolution stage feeding ADR-0045

Resolve the topic first (this ADR), then hand off to that topic's existing
`@auto`/explicit-atom resolution unchanged.

Good: clean separation of concerns — each ADR owns one resolution axis;
reuses ADR-0045's mechanism without modification; the confidence-tier problem
(this ADR's real new piece) stays isolated from ADR-0045's simpler
resolve-or-fail-closed shape.
Bad: needs a topic-less entry surface to be useful for cold entry (case 1),
which is real UI work, not just an algorithm.

### Option C: Fuzzy topic search/typeahead only, no auto-resolution

A compose-time picker that ranks topics by similarity as the user types, but
never resolves or switches on its own.

Good: zero misroute risk.
Bad: doesn't achieve "just talk to it" — the user still has to recognize and
pick the right suggestion every time. Useful as the low-confidence fallback
inside Option B, not as a standalone answer.

## Decision Outcome

**Option B**, using Option C as its low-confidence tier rather than a
separate mechanism.

1. **Confidence tiers, not a binary resolve.**
   - **High confidence** → auto-switch, then proceed into that topic's
     ADR-0045 resolution as normal. Recorded (see Stats/audit) so the switch
     is visible and auditable after the fact — this is the one point where
     ADR-0045-style silent resolution is reused, because the confidence bar
     to reach this tier is deliberately high.
   - **Medium confidence** → answer in the current context, but surface a
     lightweight "this looks like it belongs in #topic — switch?" affordance.
     No context switch happens unless the user accepts.
   - **Low confidence** → no suggestion at all; falls through to Option C's
     typeahead only if the user is at the topic-less entry surface with
     nothing else to route into.
2. **A topic-less entry surface** ("the meta harness") is where cold entry
   (case 1) happens: a compose box not bound to any topic. If nothing clears
   even the low-confidence tier there, Squid offers the normal
   create-a-new-topic flow rather than guessing.
3. **Corpus and mechanism, reused from ADR-0045**: each participating topic
   gets a representative embedding built from its topic-memory description
   plus a rolling sample/centroid of its own recent turn embeddings. New
   topic-less prompts are embedded the same way and matched against this set.
   Same local-provider embedding path (ADR-0037/ADR-0043) — no new
   model-serving mechanism, no new store.
4. **Per-topic opt-in, not automatic inclusion.** A topic only participates in
   the meta-routing pool if its frontmatter sets it explicitly (e.g.
   `squid.routing.discoverable: true`). This prevents a topic — including
   ones with sensitive content — from being silently offered as a switch
   target from an unrelated conversation just because it exists.
5. **Handoff is unchanged.** Once a topic is resolved (auto-switched or
   user-accepted), the message enters that topic and is resolved to an atom
   by ADR-0045 or an explicit route exactly as if the user had opened the
   topic and typed it there. This ADR never picks an agent itself.

### Stats attribution and anti-feedback-loop policy

Every topic-switch — auto-applied or hint-accepted — is recorded (which
topic, which confidence tier, accepted/rejected for hints), mirroring
ADR-0045's `invoked_via` flag. Same rationale as ADR-0045's training-set
policy: only *accepted* switches (auto-applied ones, and hints the user
explicitly took) feed the per-topic representative-embedding corpus. A hint
the user ignores is not treated as evidence the guess was right, for the same
reason ADR-0045 excludes its own uncorrected resolutions from its training
index — an unconfirmed guess must not silently reinforce itself.

## Future Work (explicitly deferred, not v1)

- **Multi-candidate disambiguation UI** when two or more topics tie at
  similar confidence — analogous to ADR-0045's LLM tiebreak, but at topic
  granularity (likely a short list rather than a single tiebreak call, since
  the cost of asking "which topic did you mean" is much lower than the cost
  of guessing wrong).
- **Auto-creating a new topic** when nothing matches and the prompt looks
  self-contained (e.g. names a new, unseen repo path). Deferred: real risk of
  topic sprawl if this fires too eagerly.
- Once both this ADR and ADR-0045 have real usage data, a combined "topic +
  agent both inferred" experience falls out of the two stages composing —
  worth revisiting whether that composition needs anything beyond "run stage
  0, then stage 1," but not before either stage has been validated alone.

## Consequences

- Good: makes "just talk to it" achievable without inventing a new mental
  model — reuses ADR-0045's mechanism, embedding infra, and guardrails
  (no invented taxonomy, opt-in, auditable) rather than building a parallel
  system.
- Good: the confidence-tier design keeps the genuinely expensive mistake
  (silently answering in, or switching to, the wrong topic) from happening
  invisibly, which a direct reuse of ADR-0045's binary resolve-or-fail-closed
  shape would not have guaranteed.
- Good: per-topic opt-in and accepted-only training corpus mirror ADR-0045's
  existing anti-feedback-loop and privacy-conscious defaults.
- Bad: requires a new topic-less entry surface — real UI work, not just a
  routing algorithm change.
- Bad: cold-start gap is worse than ADR-0045's. A brand-new topic has no
  usage history and possibly a thin description, so early "meta harness"
  sessions will lean on manual topic picking or Option C's typeahead until a
  topic accumulates enough signal.
- Bad: topics with overlapping language (e.g. several client-project topics
  that all mention "push the changes") will produce more medium-confidence
  hints than clean auto-switches, which is more interruption than ADR-0045
  typically produces for in-topic agent resolution.
