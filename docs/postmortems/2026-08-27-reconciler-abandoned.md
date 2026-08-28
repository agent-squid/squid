# Postmortem: ADR-0041 normalized transcript store & reconciler, abandoned

**Date:** 2026-08-27

**Status:** Reconciler removed; client-side rendering reverted to per-call-site
direct DOM discipline on a rebuilt branch (`dom-ws-rebuild`, based on
`v0.1.5rc1`). `main` is preserved at `archive/reconciler-20260827` for
reference.

**Related decision:** [ADR-0041](../decisions/0041-normalized-client-transcript-store-and-reconciler.md)
(superseded), following [ADR-0040](../decisions/0040-versioned-realtime-protocol-over-websocket.md)
and the [2026-08-12 WebSocket UI migration postmortem](2026-08-12-websocket-ui-regression.md)

## Summary

ADR-0041 proposed a normalized, transport-agnostic transcript store plus one
idempotent reconciler as the single code path allowed to mutate `#messages`,
to replace the per-call-site direct-DOM discipline that the 2026-08-12
postmortem found unreliable. It was implemented incrementally from
2026-08-18 to 2026-08-27 (~24 commits, four producers migrated) and shipped
as the default renderer.

It did not deliver the promised outcome. Instead of collapsing the
timing-combination bug class into invariants enforced once, it introduced a
comparable volume of new, narrower bugs on top of the ones it targeted,
required repeated retirement/rework of its own migration scaffolding, and
added ~680 lines of production code plus ~3,300 lines of dedicated
regression tests to maintain — without the underlying WebSocket transport
(ADR-0040, which predates and is independent of this work) needing any of
it. The decision on 2026-08-27 was to abandon the reconciler architecture
entirely rather than continue investing in it, and rebuild the client from
the pre-reconciler baseline instead.

## What happened

- **2026-08-18** (`cbe2854`): store, reconciler, and history registry added
  alongside unrelated fixes in the same commit; producer 1 (HTTP history)
  begins shadow-mode integration.
- **2026-08-18** (`cf15b5e`, same day): the migration's own progress doc
  records producer 1 is *not* ready to become the default renderer. A trial
  flip of the default from `renderer=dom` to `renderer=store`, run against
  the full pre-existing 39-file e2e suite, went from 43 pre-existing
  (unrelated) failures to 56 — 13 new failures, all in history-rendering
  paths, none of them dedicated ADR-0041 tests. Two new gaps were found by
  this process, not by the ADR-0041 test suite itself:
  - **Gap 1:** a history row without an explicit `status` field rendered
    nowhere under the store path. The direct-DOM path it replaced was more
    lenient — it rendered anything not explicitly `'pending'` as completed.
    The store path was strictly less forgiving of the same real-world data
    shape.
  - **Gap 2:** a live in-flight prompt could end up in the wrong position
    relative to store-rendered completed turns after a filter round-trip —
    the exact class of ordering bug the reconciler was supposed to make
    structurally impossible.
- **2026-08-20 to 2026-08-22** (`31079fb`, `5b3f886`, `5c3dc7c`): direct-DOM
  rollback paths and fallbacks for producers 1 and 2 are explicitly
  "retired" — twice, in separate commits — each retirement itself requiring
  new regression specs to confirm nothing broke. `a9dfcee` (2026-08-22) adds
  "forget SSE-completed ids from reconciler," "reject stale non-terminal
  patches wholesale," and registry scroll-anchoring fixes in one commit —
  three independent reconciler-internal bugs, not application bugs, found in
  a single pass.
- **Test suite required non-default execution to be trusted.** The
  migration's own verification notes record that "the default parallel run
  is flaky — different tests intermittently fail from worker contention, not
  a real bug — so use `--workers=1` for a trustworthy local read." The final
  verification run (2026-08-22) reports `chat.spec.js`'s four failures
  "exposed the stale-frame success/no-op regression, and all four passed
  together after the fix" — a real rendering regression that did not show up
  as a consistent, individually-attributable test failure; it needed the
  full suite run together, under controlled worker settings, to be caught at
  all. This matches the pattern reported independently by engineers working
  with the branch day to day: individual test runs passing while the actual
  UI, exercised by hand, did not behave correctly.
- **2026-08-27** (`4d9a8ce`, the final commit before abandonment): titled
  "Fix live transcript reconciliation and session boundary" — a core
  reconciliation bug was still being found and fixed on the last day of the
  effort, nine days and roughly two dozen commits after the architecture
  first shipped.
- **Final footprint:** `ui/reconciler.js` (192 lines) + `ui/transcript-store.js`
  (486 lines) = 678 lines of new production code; `transcript-store.spec.js`
  (1,475 lines), `reconciler.spec.js` (325 lines), `history-registry.spec.js`
  (936 lines), `history-store-renderer.spec.js` (564 lines) = 3,300 lines of
  dedicated regression tests, plus substantial expansion of `chat.spec.js`.
  14 of the ~24 ADR-0041 commits touched `reconciler.js`/`transcript-store.js`
  directly; 7 commit messages contain "retire," "dirty," "stale," "rollback,"
  or "gap" — language describing repeated discovery and repair of the
  architecture's own internal state-management bugs, not application
  features.

## Impact

- Engineering time was spent building, then repeatedly patching, a
  client-side architecture layer that the underlying WebSocket transport
  (ADR-0040) never required — ADR-0040 shipped and worked independently of
  whether ADR-0041 existed.
- Multiple rounds of "shadow mode → cutover → retire the fallback" had to be
  redone per producer (four producers, several retirement commits each),
  meaning the migration's own staging discipline did not prevent rework — it
  mostly relocated where the rework happened.
- The reconciler introduced bugs (Gap 1's silent non-render, Gap 2's ordering
  regression, the SSE stale-frame no-op regression) in categories the direct
  DOM code, despite its acknowledged multiple-owner risk, had not exhibited
  in the same test surface.
- The dedicated test suite (3,300+ lines across four spec files) increased
  confidence in the reconciler's internal invariants but did not reliably
  catch integration-level UI regressions under default (parallel) execution
  — those needed a full-suite, `--workers=1` run to surface, and even then
  only as an aggregate failure count, not a clearly attributable single test.
  Passing tests were not a reliable proxy for correct UI behavior throughout
  this effort.

## Root cause

ADR-0041's own Context section correctly diagnosed the underlying problem —
multiple independent call sites in `ui/app.js` each re-deriving correct
transcript behavior for every new timing combination — but the proposed fix
(a normalized store plus a serial reconciler as the sole DOM-mutation path)
substituted one hard problem for a different hard problem. Enforcing "one
canonical message record," "atomic terminal transition," "exactly one live
registration," and deterministic ordering *as data-structure invariants*
required the store and reconciler to correctly model every timing
combination that direct DOM code used to handle ad hoc — and getting that
modeling wrong (Gap 1's status-inference leniency, Gap 2's anchor
computation, the stale-frame no-op path) produced bugs that were harder to
locate than the direct-DOM bugs they replaced, because the failure was now
one layer removed from the DOM state a developer could visually inspect.

The staged migration plan (shadow mode, one producer at a time, feature-flag
rollback) was sound in design and was followed. It bounded blast radius per
step, but did not bound total effort: each producer's cutover still
surfaced new gaps that required their own fix-and-retest cycle, and the
final commit's title — still describing a "reconciliation" fix — indicates
the invariant-enforcement promise was not fully realized even at the point
the effort was abandoned.

## Contributing factors

- The reconciler's invariants were verified primarily through dedicated
  reconciler/store unit-style e2e specs, which by construction test the
  reconciler's model of the world rather than the rendered DOM an end user
  sees. Integration-level regressions (stale-frame no-op, live/completed
  ordering) were caught by the pre-existing general-purpose suite
  (`chat.spec.js`), not by the reconciler-specific tests built to validate
  this exact architecture.
- Default (parallel) test execution was known to be unreliable for this
  suite ("flaky... from worker contention") well before the effort
  concluded, which reduced the value of "tests are green" as a fast signal
  during day-to-day development.
- Each producer's migration required retiring its own rollback/fallback
  path in a separate commit, meaning the codebase carried both the old and
  new rendering logic simultaneously for extended periods — more surface
  area to reason about, not less, during the transition.

## Decision

Abandon ADR-0041. Do not attempt a lighter-weight version of the same
architecture (e.g., a smaller live-turn-only registry) as a direct
replacement goal. Preserve `main`'s reconciler-era history at
`archive/reconciler-20260827` for reference. Rebuild the client
(`dom-ws-rebuild`) from `v0.1.5rc1` — the last point before the reconciler
work began — keeping ADR-0040's WebSocket transport (which was never the
source of the bugs described here) and the per-call-site direct-DOM
rendering discipline it already had. Independent, non-reconciler-coupled
fixes and features that landed on `main` during the reconciler period are
being cherry-picked back individually.

## Lessons

- A documented, structurally sound migration plan (staged, shadow-mode,
  flagged rollback, dedicated regression tests) does not by itself guarantee
  the target architecture is a net improvement. It reduces risk *during* the
  migration; it does not validate the destination.
- When a new architecture is meant to make a bug class structurally
  impossible, the regression tests that matter most are the ones written
  against the *previous* architecture's behavior (here, the general
  `chat.spec.js` suite), not new tests scoped to the new architecture's own
  internal model — the latter can pass while validating the wrong thing.
- "Tests pass" is only a useful signal if the suite's execution mode is
  trustworthy. A test suite known to be flaky under its default run mode
  should not be treated as a reliable gate without calling that out loudly
  and fixing it, not routing around it with special invocation instructions.
- Repeated "retire the fallback" commits within a single migration are a
  signal worth noticing in real time, not just in retrospect: each one meant
  the previous step's confidence was incomplete.
- Prefer the smallest structural fix that addresses the specific documented
  failure (here: a shared registry keyed by `msg_id` for *live* turns only)
  over a general-purpose architecture that re-solves the entire transcript
  rendering problem, when the general solution's own migration risk and
  bug surface end up comparable to what it replaces.
