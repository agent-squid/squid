---
status: proposed
date: 2026-11-01
---
# ADR-0054: Agent-Callable Squid Capabilities and Durable Continuations

## Context

Squid already owns topic queues, routes, durable Flow execution, isolated-turn
sync, and watch lifecycles. Agents need a discoverable interface to use these
functions and understand their limits. This is broader than publication or
watches: a request may combine editing, review, committing, tagging, releasing,
or further work that must happen in another turn.

With ADR-0025 isolation enabled, an editing turn cannot commit its resulting
changes: they reach the backing repo only after the model process exits and
sync completes. Today users split the request into an editing prompt and a
git-only prompt. With isolation disabled, agents edit the backing repo directly
and can edit and commit within one turn. No deferred step is inherently needed.

Exposing commands and documenting that constraint cannot bridge the lifecycle
boundary. The one-shot process will not resume. Squid must persist and execute
any requested continuation itself. Conversely, a catalogue of hardcoded release
actions would duplicate repo-specific procedures that agents already understand.

## Decision

### Shared capability interface

Extend ADR-0053's shared tools surface rather than introducing a second tool
registry or scheduler. The server owns capability schemas, validation, effective
limits, scheduling, and results. CLI and MCP expose equivalent operations;
native harness integration is an adapter, not a prerequisite for CLI delivery.

Discovery reports implemented operations and current effective constraints:
topic and agent, isolation/fallback state, editable roots and backing repos,
available routes and execution modes, permissions, remaining budgets, and
blocking sync state. Distinguish supported, enabled, and currently executable.
Do not advertise planned functionality as available. Dynamic execution limits
stay current even when explanatory context uses ADR-0046's delivery cadence.

The agent interprets the user's request and chooses appropriate calls. There
is no mandatory extra planning/classification model pass on every prompt.
Squid validates every request against current state; a schema description or
model promise is not enforcement.

### Durable continuation as the first execution primitive

Allow the current turn to request a subsequent agent turn after its successful
completion and sync. Proposed CLI spelling (not yet implemented):

```sh
agentsquid continue create --input continuation.json
agentsquid continue show <id>
agentsquid continue cancel <id>
```

An initial request contains a client request key, continuation prompt, supported
execution mode, and optional configured agent. The server derives the issuing
turn, topic, original request, and parent Flow/watch lineage from its credential;
caller-provided IDs cannot substitute another origin. The response returns a
durable ID and state, not a claim that execution already happened.

The first execution slice supports one immediate successor per turn. A successor
may schedule another successor only within the same root execution budget.
Later slices may expose bounded route execution through the same interface.
Do not invent a second route language or implicitly parse a prompt into a DAG.

Continuations are normal agent turns, not necessarily git-only. An isolated
continuation can review or edit, sync, and request another step. A continuation
explicitly intended for backing-repo Git operations makes no file edits and
uses a distinct execution mode. Release version bumps belong in an editing
turn; commit/tag/push can follow its sync. Mode violations stop execution
before later steps become eligible; they must not silently promote a turn to
broader access. Host operations may later optimize concrete steps without
requiring a fixed catalogue of commit/tag/release actions now.

### Completion and sync gate

Persist requests while the parent is running, but make them eligible only after
verified successful turn completion and successful sync for every participating
repo. A repo with no changes must have an explicit successful no-op result;
the absence of a worktree row alone is not success evidence. Direct execution
with isolation disabled records that sync was not applicable. Errors,
cancellation, unresolved conflicts, promotion failure, and unknown completion
block the successor. Resolving a conflict does not implicitly replay a blocked
publication request; explicit resume must revalidate its prerequisites.

Admission into the ordinary topic queue is not permission to execute against
stale state. Recheck blockers, execution mode, effective permissions, and budgets
at execution time. Reuse queue serialization and repo promotion coordination;
sync success is not an exclusive reservation of the backing repo. Discovery and
the continuation context must make intervening changes inspectable.

### Durability, ownership, and recovery

Reuse ADR-0042's durable step identities, atomic claims, message linkage, and
transactional realtime outbox where practical. Each logical continuation has
one scheduler owner; native Flow, watch, and continuation recovery must never
independently advance the same step. Extend the store or add linked records
only where the lifecycle needs information the existing model cannot represent.

Use idempotent request keys scoped to the parent turn. Identical retries return
the existing request; a changed payload with the same key conflicts. Link a
claimed continuation to its prepared chat messages before dispatch. Persist
waiting, eligible, claimed, running, successful, failed, blocked, cancelled,
and ambiguous outcomes, with explicit reasons and linked parent/child IDs.
The final SQL and state names are implementation details.

Restart recovers persisted waiting work and reconciles prepared/started work
against durable completion evidence. Never automatically replay an ambiguous
started turn: commits, tags, pushes, and external releases are not generally
safe to repeat. This provides deduplicated scheduling, not exactly-once
external side effects. A failed release after a successful push reports both;
it does not claim rollback. Resume/retry is explicit and preserves prior results.

Cancellation prevents new descendant claims and uses existing cancellation for
running turns. History deletion must not orphan runnable work: preserve ownership
and terminal evidence or cancel linked pending execution transactionally.

### Permissions and bounds

Use existing execution permissions, including trusted YOLO operation. Do not
add mandatory confirmation dialogs or a new grant system merely to schedule a
continuation. YOLO approval behavior does not bypass sync gates or budgets.
Descendants retain the original request's scope and provenance; a model-authored
continuation cannot manufacture a new human request or increase permissions.

Watch local-work grants continue excluding commit, push, and publication.
Watch-triggered Flow descendants keep that restriction. Supporting watch
publication remains ADR-0053 work and is not enabled by this decision.

Configure finite maximum depth, total started turns, cumulative execution time,
and execution concurrency per root request, with topic/global ceilings. Persist
budget accounting atomically with claims; splitting into new descendants or
restarting cannot reset it. Add cost/token ceilings where reliable accounting
exists; do not claim strict monetary enforcement from incomplete telemetry.
Expired credentials stop new requests, not already accepted server-owned work;
each successor receives fresh scoped credentials and current constraints.

### Preserve the user's Git workflow

Ordinary sync leaves accumulated changes uncommitted. Neither a continuation
nor a successful edit automatically creates a user-visible commit. Commit,
tag, push, or release work occurs only within the user's requested scope.
"Commit what we have done" may cover accumulated changes across turns; do not
force a commit per turn or default every commit to the last turn's patch.
Keep turn diffs for attribution and drift review, not as mandatory commit scope.

For "edit, commit, tag, release", the agent performs file edits and checks,
schedules the appropriate successor, and ends. Squid syncs and starts it. If
further edits become necessary, another isolated turn/sync boundary is valid.
When isolation is off, tools remain available but must not force needless splits.

### Visibility

Show pending continuations, prerequisites, parent/child links, mode, remaining
limits, cancellation, and final outcomes through the shared API/CLI and ordinary
transcript/realtime mechanisms. A parent response can say a continuation was
scheduled only after durable acceptance; it cannot say the remaining work has
completed. Scheduling failure must be visible in that response.

## Alternatives

- Commit-only post-sync helper: useful optimization, but cannot express review,
  additional edits, or repo-specific release judgment.
- Unrestricted model-started routes: omit lifecycle ownership and allow loops;
  expose route execution only after durable bounds and recovery exist.
- Hardcoded publication workflow engine: unnecessary for the first release;
  normal turns already handle repository procedures.
- Prompt-only continuation: cannot run after the one-shot process exits.

## Consequences and scope

This adds durable orchestration, so it warrants a phased implementation and
failure/restart testing. It reuses existing queues and Flow concepts rather
than replacing them. Branchless isolation is not an OS security sandbox;
backing-repo access is possible today. Prompt instructions and post-run checks
must not be described as prevention of arbitrary shell side effects. Any
stronger mode enforcement requires an actual execution boundary.

This ADR remains proposed. An opt-in [first CLI/HTTP slice](../continuations.md)
implements one successor per human turn, enabled automatically by
`worktree.enabled` without a separate continuation option; broader discovery, MCP, route and
budget requirements remain pending. See the [implementation plan](../plans/adr-0054-agent-capabilities-and-continuations.md).

## References

- [ADR-0025: isolation and uncommitted sync](0025-per-turn-worktree-isolation.md)
- [ADR-0042: durable Flow execution](0042-durable-squid-flow-execution-store.md)
- [ADR-0046: runtime context delivery](0046-layered-runtime-and-user-context-injection.md)
- [ADR-0053: shared tools and watches](0053-persistent-watches-and-shared-agent-tools.md)
