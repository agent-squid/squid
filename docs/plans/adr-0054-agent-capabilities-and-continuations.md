# Implementation plan: agent capabilities and durable continuations

Status: partial ADR implementation; milestones 4 and 5 complete for their documented supported subsets. Updated: 2026-11-07.

Design authority: [ADR-0054](../decisions/0054-agent-callable-squid-capabilities-and-continuations.md).
The milestone list below remains the full proposed contract. The first slice is
documented in [continuations](../continuations.md); it does not enable production
configuration or complete all milestones.

## Scope and existing integration points

Expose Squid functions and effective limits through shared tools, beginning with
capability discovery and one durable successor after completion/sync. Preserve
uncommitted sync, current YOLO permissions, watch restrictions, and normal
direct execution when isolation is disabled. The bounded same-topic route subset is documented in [routes](../routes.md);
general route launch remains outside this slice.

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

Status: complete for the supported `isolated`, `backing_git`, `task_review`, and
recorded-grant `task_publication` modes. Validated 2026-11-07: **577 tests passed**
across publication, continuation, ownership, watch descendants, queue, Flow,
realtime and runner suites. This does not complete the whole ADR, enable new
production configuration, or authorize publication.

Startup recovery and lifespan cleanup are implemented.
Sequence cancellation now commits its freeze before draining linked queued
children and stopping active children through existing controls. Periodic
maintenance retries interrupted stops even with new continuations disabled.
Startup and periodic reconciliation now share atomic completion-gate handling.
Periodic reconciliation leaves ungated live work alone, preserves credentials,
and advances successful saved plans once under competing calls. Cancelled
plans never advance. Its worker is independent of dispatch waits and is joined
at shutdown through the same lifecycle as cancellation maintenance.
Lifecycle transitions now enter the existing durable realtime outbox as scoped
`continuation.changed` events with parent/child links and status. Database
triggers cover ordinary, recovery and host publication writers atomically;
no-op retries emit nothing. This adds no custom UI.
History deletion now freezes affected sequences transactionally, retaining
terminal outcomes and marking potentially started work ambiguous. Stop cleanup
can find linked children after their message is deleted, without advancing or
replaying the sequence.
Fresh-process restart acceptance now covers thirteen persisted boundaries. A setup
process exits with `os._exit` after acceptance, successful sync, failed sync,
credential expiry, claim preflight, claim, unlinked message preparation, child
linkage, external worker start/crash, model-result acknowledgement, transcript
completion without a sync gate, successful sequence advancement, or cancellation.
Two fresh recovery processes each run
recovery twice and must produce identical continuation records and message
identities/counts. The expiry case verifies a persisted expired credential.
A separate admission process proves only eligible work can be claimed once;
unknown, failed and cancelled work cannot be claimed, and admission creates no
new messages. This checks duplicate advancement and credential revocation as
well as the scheduler's refusal to replay uncertain claims.
Publication coverage also exits a separate publisher process immediately before
or after a real push to a disposable local bare remote. Fresh recovery processes
retain the commit, distinguish unperformed from confirmed pushes, preserve
ambiguous continuation status, and neither repeat Git writes nor create PRs.
Repeated recovery leaves remote refs and child message counts unchanged. GitHub
is a read-only fake; no external service or real remote is used.
The test invokes the actual publication recovery entry point, including its
partial-outcome message persistence, rather than manually finishing observations.
The worker-start case launches a deterministic external process, appends an
observable effect, kills that worker with SIGKILL before a completion gate, then
abruptly exits the host. Fresh recovery preserves ambiguity and refuses another
claim; the effect is not repeated. Concurrent claim/cancellation admission is
also exercised with competing database transactions.

Acceptance follows this plan's disposable-repository and deterministic-harness
contract. Paid proprietary agents and live publication services are not required
milestone 4 gates. Tag/release operations remain unsupported; their no-replay
requirement is enforced by keeping them unavailable rather than adding them in
this milestone. Partial commit/push/PR publication is covered by the supported
host publication tests.

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


### Milestone 4 exit-check evidence

Evidence lives in [continuation tests](../../tests/test_continuations.py),
[publication tests](../../tests/test_task_publication.py), and
[runner tests](../../tests/test_runners.py).

| Exit check | Passing evidence |
| --- | --- |
| Crash after acceptance, eligibility, preparation, linkage, worker start; sync before enqueue | `test_fresh_process_restart_at_durable_boundaries` (thirteen boundaries, two fresh recovery processes and separate admission) |
| Recover once; no replay of uncertain effects | Same restart matrix, `test_sequence_restart_never_replays_uncertain_step`, and `test_competing_reconciliation_settles_and_advances_once` |
| Cancellation versus claim/preparation | `test_competing_claims_and_cancellation_freeze_before_dispatch`, `test_cancellation_after_claim_prevents_preparation`, and `test_cancellation_during_preparation_cancels_unlinked_child` |
| Parent/child/request history deletion and terminal evidence | `test_history_deletion_freezes_sequence_and_stops_surviving_process`, `test_deleting_child_request_freezes_only_its_unfinished_sequence`, and `test_deleting_completed_sequence_history_preserves_sequence_outcome` |
| Partial publication failure; never replay writes | `test_fresh_process_crash_at_push_recovers_without_replaying_writes`, `test_pr_failure_and_crash_never_duplicate_creation`, and `test_push_failure_records_commit_and_reconciles_only_confirmed_push` |
| Expired/revoked parent credentials | Restart matrix expiry boundary, persisted revocation assertions, and independent admission after recovery |
| Observable status and lineage | Scoped CLI/API tests and `test_continuation_outbox_is_atomic_scoped_and_idempotent` / `test_continuation_outbox_covers_link_cancel_and_external_writers`; existing transcript parent notice, no custom UI |
| Shutdown/startup failures and retryable stops | `test_startup_failure_cancels_worker_before_retry`, `test_shutdown_joins_cancellation_thread_despite_repeated_cancel`, `test_cancellation_retries_while_dispatch_waits_and_joins_on_shutdown`, and failed-signal runner regression |

Validation command:

```sh
uv run pytest -q tests/test_task_publication.py tests/test_continuations.py tests/test_continuation_owners.py tests/test_watch_descendants.py tests/test_topic_queue.py tests/test_flow.py tests/test_realtime.py tests/test_runners.py
```

Milestone 4 has no remaining acceptance blocker within these supported modes.
The next section records milestone 5's bounded route delivery; the broader
ADR's unimplemented capabilities remain separate work.

## Milestone 5: bounded route exposure and rollout

Status: complete for the explicitly opt-in same-topic immediate subset. Routes
use the existing parser, Flow DAG/store, dependency gates, topic workers and
transcript events. A managed-route maintenance task is their sole dispatch
owner; ordinary Flow dispatch skips those plans. CLI and HTTP do not depend on
the optional MCP SDK. `agentsquid mcp` maps the same operation catalog and
issuing-instance HTTP transport to native stdio tools without writing harness
configuration.

Acceptance reserves the entire plan atomically with its root provenance.
Fan-out, nested routes and local continuation sequences share finite turn,
depth, cumulative reserved-time and concurrency limits; topic/global ceilings
are charged in the claim transaction. Verified completion releases unused
time; uncertain starts retain their charge and are never replayed. Configuration
is off by default and has finite defaults/hard ceilings. Effective saved/current
limits, budget use, supported/enabled/executable state and blockers appear in
versioned discovery. Human work defers admission without invalidating running
steps. Cancellation, history deletion, restart and shutdown retain the existing
completion/sync and no-replay guarantees.

Unsupported combinations are rejected explicitly: cross-topic, delays, repeats,
multiple edges, task origins, watch origins, ordinary Flow origins and
continuation origins. Managed route members may schedule a nested route or
isolated continuations only. Existing recorded watch grants and publication
behavior are unchanged; this slice grants no new watch or publication authority.
The wider ADR remains proposed, including general route exposure and aggregate
budgets outside managed roots. See [operator contract](../routes.md) and
[data model/API](../data-model-and-api.md#bounded-route-exposure-adr-0054-milestone-5).

Validation on 2026-11-07: **1,750 tests passed** in the full Python suite.
After final changes, **114 tests passed** in the route/Flow/CLI acceptance recheck,
including **41 route tests**. An earlier expanded focused run passed 500 tests
across routes, Flow, continuations, ownership, watch descendants/CLI, queue,
context and worktree suites. No live agent or remote publication was required.

```sh
uv run pytest tests -q
uv run pytest tests/test_routes.py tests/test_flow.py tests/test_watch_cli.py -q
```

Pre-publication review on 2026-11-07 tightened managed member completion to
require the worker's saved completion/sync gate, including on recovery.
Dispatch-stream closure and SIGTERM delivery retain capacity until worker
cleanup; pre-launch worker exits release capacity without inventing success.
Individual stop failures no longer starve other workers. Task-bound Git-mode
requests are rejected at acceptance, and partial reconciliation refreshes its
saved publication message. Older records without owner evidence remain blocked;
the upgrade behavior is documented rather than inferring authority.

Review validation: **1,767 full-suite Python tests passed**, followed by **413
final lifecycle checks** and two targeted publication regressions. The changed
browser specs produced 188 passes and two stale fixture failures; both fixtures
were corrected to model process baselines/auth request idempotency and passed
on rerun. No production UI change was needed. Publication admission still runs
Git validation under the database write lock at that review; the subsequent
claim-admission follow-up below resolves that concern.

Exit-check evidence is in `tests/test_routes.py`:

| Exit check | Evidence |
| --- | --- |
| Nested route/continuation limits; fan-out accounting | Shared depth/turn exhaustion, competing root reservations, cumulative time and atomic fan-out/global/topic admission tests |
| Watch provenance restrictions | Real recorded watch owner denied route expansion; existing watch/Flow behavior retained |
| CLI/MCP parity | Shared registry catalog, issuing-instance transport comparisons and a real native MCP stdio subprocess calling a disposable loopback API |
| Completion, recovery and no replay | Parent sync gate, expired issuer credential, independent fresh-process recoveries, persisted ambiguous claim charge, and proven parent waiting for an unstarted nested route |
| Dispatch/lifecycle | Existing durable executor runs origin and parallel targets; cancellation/history deletion stop saved worker IDs; scheduler cancellation joins dispatch tasks |


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

### Acceptance scenario 6: restart and duplicate requests

Status: validated 2026-11-07. **1,778 tests passed** in the full Python suite
after the claim-lock fix and acceptance expansion. Targeted runs also passed
all thirteen restart boundaries, both PR-creation crash cases, and five
publication-claim lock/race cases.

```sh
uv run pytest tests -q
```

The restart/duplicate acceptance scenario uses disposable repositories and
fresh processes. It is distinct from the watch-PR plan's stage 6 live-model
proof, which requires its own explicitly authorized test repository.

The continuation restart matrix now includes Git-read preflight before claim,
a recorded model result without worker completion, and a transcript `done`
without a completion/sync gate, in addition to its prior ten boundaries.
Identical create requests are retried at each applicable boundary; changed
payloads conflict, expired credentials fail, repeated link/result/completion
calls cannot create a second child or advance twice. Separate recovery and
admission processes preserve message identities/counts and charge uncertain
starts without replay.

Fresh publisher processes also exit immediately before and after PR creation.
A persistent fake external PR record survives process loss. Two independent
recovery processes, each reconciling twice, must preserve identical results,
remote refs and message identities while making zero Git writes or PR creates.
A confirmed PR may complete the terminal publication; an unconfirmed PR remains
partial and cannot be automatically recreated. This supplements the existing
fresh-process crash coverage around real pushes to local bare remotes.

The claim-admission follow-up captures local Git reads before `BEGIN IMMEDIATE`
and rechecks current database authority/budgets under the lock using only
captured Git observations. A new identity read defers the claim. Tests prove
another writer can commit while validation runs, including revocation,
cancellation and expiry changes; those changes block admission. Recorded-watch
publication and review admission also execute no Git subprocess under the lock.
No observations survive into execution or publication effects.

Evidence: `test_fresh_process_restart_at_durable_boundaries`,
`test_fresh_process_crash_at_push_recovers_without_replaying_writes`,
`test_fresh_process_crash_at_pr_creation_recovers_without_duplicate_effects`,
`test_publication_claim_git_does_not_hold_write_lock_and_rechecks_authority`,
`test_changed_publication_identity_read_retries_outside_lock`, and the existing
recorded-watch publication and competing-claim/cancellation regressions.

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
PID stability. Implementation and validation evidence for the delivered subsets
is recorded in the milestone sections above.
