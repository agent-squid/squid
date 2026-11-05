# Implementation plan: agent capabilities and durable continuations

Status: partial implementation, opt-in single-successor CLI/HTTP slice. Updated: 2026-11-04.

Design authority: [ADR-0054](../decisions/0054-agent-callable-squid-capabilities-and-continuations.md).
The milestone list below remains the full proposed contract. The first slice is
documented in [continuations](../continuations.md); it does not enable production
configuration or complete all milestones.

## Scope and existing integration points

Expose Squid functions and effective limits through shared tools, beginning with
capability discovery and one durable successor after completion/sync. Preserve
uncommitted sync, current YOLO permissions, watch restrictions, and normal
direct execution when isolation is disabled. General route launch is a later
slice, not a dependency for the first useful continuation.

Read these implementations before editing:

- `agent/cli.py`, `agent/watch_cli.py`, `agent/watch_api.py`: current shared
  CLI/API discovery, strict input validation, and per-turn credential handling.
- `agent/context.py` and runtime templates: effective constraints and delivery.
- `agent/server.py`: turn preparation and worktree setup.
- `agent/topic_queue.py`: queue ownership, worker completion, sync, and cleanup.
- `agent/flow.py`, `agent/stats_db.py`: durable identities, claims, message
  linkage, recovery, and realtime outbox.
- `agent/watch_dispatch.py`, `agent/watch_flows.py`, `agent/watch_runs.py`:
  watch-owned scheduling and inherited local-work limits.

## Milestone 1: contract and capability discovery

- Extract/reuse a shared capability registry without changing watch behavior.
  Keep CLI usable without native MCP integration or optional MCP dependencies.
- Define versioned JSON schemas for discovery, continuation create/show/list/
  cancel, request-key conflicts, modes, limits, and structured failures.
- Report effective isolation/fallback, roots, available operations and modes,
  blockers, and permissions. Exclude unimplemented operations from availability.
- Deliver concise discovery guidance with current dynamic constraints through
  the existing runtime context path. Never put credentials in prompts or args.
- Document that calls schedule future work; they cannot keep a CLI turn alive.

Exit checks: schema/API/CLI parity, disabled capability errors, isolation on/off
and fallback output, issuing-instance routing, and credential redaction.

## Milestone 2: durable storage, lineage, and budgets

- Decide how continuation records link to or extend existing Flow steps. Write
  the concrete schema/API changes in `docs/data-model-and-api.md` with migration.
  Preserve a single scheduler owner and exclude watch-owned plans from native
  continuation dispatch.
- Persist parent/root request IDs, server-derived provenance, payload, execution
  mode, request key, message linkage, prerequisites, outcomes, and timestamps.
- Implement idempotent create and atomic claims. Begin with one immediate
  successor per turn; reject conflicting successor requests explicitly.
- Persist finite root-level depth, started-turn, execution-time and concurrency
  limits with global/topic ceilings. Reserve budget atomically before start;
  release unused reservations on proven pre-start failure. Unknown starts retain
  their charge. Descendants cannot reset the counters.
- Define transactional cancellation and history-deletion behavior with outbox
  events, without rewriting existing terminal outcomes.

Exit checks: migration/reinitialization, duplicate and concurrent create/claim,
payload conflicts, lineage spoofing rejection, budget races, descendant limits,
rollback, cancellation, and deletion preserving scheduler ownership.

## Milestone 3: completion/sync gate and queue execution

- Locate the actual durable success point after worker completion and all repo
  sync outcomes. Persist an eligibility decision; do not dispatch from an early
  stream `done` event. Cover no-op sync and isolation-disabled execution.
- Keep requests waiting until their parent succeeds. Block on errors, cancelled
  work, unknown completion, conflicts, or promotion failures, including one
  failed repo in a multi-root turn. Explicit resume revalidates every condition.
- Prepare/link successor messages idempotently and submit through the ordinary
  topic queue. Recheck blockers and effective limits at execution, respecting
  existing human-work priority and cancellation rather than creating a new lane.
- Support isolated continuation turns through existing worktree setup. Define
  the backing-repo Git mode explicitly, reusing the current git-only policy.
  Inspect actual mode support before advertising it; do not call prompt-based
  no-edit rules an enforced filesystem sandbox. Detect violations and block
  descendants, while reporting already-performed side effects honestly.
- Include original intent, parent outcome, relevant diff/sync evidence, latest
  repo state, and current constraints in bounded continuation context. Reuse
  ADR-0046 assembly; avoid replaying unbounded transcripts.
- Give the child fresh credentials; parent token revocation must not erase an
  accepted request. Server tasks own execution after the model process exits.

Exit checks: edit/sync/commit sequencing; no edits before backing-Git actions;
ordinary edits never auto-commit; accumulated multi-turn commit scope retained;
isolated child edits can sync and continue again; disabled isolation permits a
single edit/commit turn; intervening user work and sync blockers are rechecked.

## Milestone 4: restart, cancellation, and observable results

- Add startup and periodic reconciliation using persisted prerequisites, claims,
  and message evidence. Reuse existing Flow recovery where its semantics fit.
- Recover waiting/unstarted work once. Mark uncertain started work ambiguous;
  never automatically replay commits, tags, pushes, or releases.
- Persist cancellation before draining queued descendants; cancel active turns
  through existing mechanisms. Keep partial external success visible.
- Surface status and parent/child links in transcript/API/CLI with existing
  realtime events. Any UI addition must identify its exact parent element,
  use the common themed modal if needed, and update all five cache versions.
- Ensure shutdown waits for reconciliation/cancellation work it owns; an
  in-memory task or model promise is not durable scheduling.

Exit checks: crash after accept, after eligibility, after message preparation,
and after worker start; restart after sync but before enqueue; cancellation
versus claim; parent/child history deletion; partial release failure; expired
parent credentials; no duplicate advancement by competing schedulers.

## Milestone 5: bounded route exposure and rollout

- Expose route validation and then route launch using the existing parser and
  durable Flow executor. Start with its supported route subset; reject unsupported
  combinations rather than silently changing semantics.
- Propagate the same root provenance and aggregate budgets through route fan-out
  and continuations. A route is not a new budget or permission boundary.
- Keep watch-triggered descendants within recorded watch execution grants;
  publication remains disabled until ADR-0053's publication milestone ships.
- Map the stable API to native MCP where supported; CLI remains the common
  transport. Do not modify persistent user harness configuration implicitly.
- Roll out behind explicit configuration. Ship finite defaults and show effective
  limits in discovery. Update ADR status/progress only after checks pass.

Exit checks: nested route/continuation limit exhaustion, fan-out accounting,
watch lineage restrictions, CLI/MCP parity, and ordinary Flow/watch regression.

## End-to-end acceptance scenarios

Use disposable Git repos, a deterministic test harness, and fake publication
endpoints. Do not push branches/tags or create releases in real remotes.

1. Isolation on: edit and test, accept a continuation, sync, commit accumulated
   requested changes in the backing repo. No per-turn commit is manufactured.
2. Edit, commit, tag, release: version-file edits happen before sync; successor
   uses the resulting commit/tag. Failed release preserves successful prior
   outcomes, with explicit recovery instead of automatic replay.
3. Further review requires edits: an isolated child edits, syncs, then schedules
   a Git-only successor, all charged to the original root budget.
4. Isolation off: edit/commit works in one turn without compulsory scheduling.
5. Multi-repo conflict, parent failure/cancellation, and ambiguous completion:
   no downstream action starts; show the blocking reason.
6. Restart and duplicate requests at every scheduling boundary: at most one
   linked execution per logical successor; uncertain external effects remain
   visible and require reconciliation.
7. Watch-triggered request attempts publication or budget expansion: reject it
   under existing restrictions even when ordinary turns use YOLO execution.

Run focused tests after each slice, then relevant queue, Flow, context, worktree,
and watch regression suites. Verify restart through `/health`/`BOOT_TIME`, never
PID stability. No runtime implementation or test execution is claimed by this
documentation-only change.
