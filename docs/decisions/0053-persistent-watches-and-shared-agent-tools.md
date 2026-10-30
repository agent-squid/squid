---
status: proposed
date: 2026-10-30
updated: 2026-10-30
---
# ADR-0053: Persistent Watches and Shared Agent Tools

## Context

Users want Squid to watch a PR or issue and act when CI fails, review feedback
arrives, or requirements change. An agent turn ends when its one-shot CLI
process exits (ADR-0019). Keeping a conversation open does not schedule future
turns, and a promise to monitor does not create monitoring infrastructure.

Squid supports multiple harnesses. A capability implemented only as one
harness's native tool or skill would require separate setup and behavior for
each harness. We need one server-owned watch lifecycle that any shell-capable
agent can manage, without an additional LLM classification pass.

This ADR proposes new functionality. It does not claim watches, their CLI,
authorization, or automatic publishing are implemented today.

## Decision

### One API, exposed through MCP and a shared CLI

Implement watch operations in the Squid server and expose them through
`agentsquid watch create/list/show/pause/resume/cancel`. The CLI is a client of
the API, not a separate scheduler. A Squid MCP server exposes equivalent
operations as structured tools. MCP, CLI, and UI all use the same validation,
authorization, and lifecycle; transport does not change authority.

The ordinary agent turn interprets a request such as "watch this PR until it
merges and fix CI failures" and invokes the command. There is no preprocessing
LLM that classifies every incoming prompt. Monitoring begins only after the
server has committed an active watch and returned its ID and status.

The proposed create contract includes:

- canonical target and topic;
- event and deadline triggers and instructions for subsequent turns;
- execution agent, independent of the agent creating the watch;
- requested action scope, including any publication permission;
- polling interval, deadline/reset/repeat policy, watch expiry, stop conditions,
  and run/retry limits;
- a client request key for idempotent creation.

Commands return structured JSON and meaningful exit codes. Creation retries
with the same request key return the existing watch. Invalid or unsupported
targets fail explicitly; a saved proposal must not be reported as active.

### Central enablement and per-turn delivery

Squid configuration controls global availability, topic enablement, supported
adapters, and action/budget ceilings. The effective policy is the intersection
of global policy, topic policy, and the user's grant for this watch. A watch
cannot expand its own permissions or budget, including through a later turn.

Maintain one central tool registry with topic-level enablement and narrowing
of permissions. Harness adapters translate the effective configuration into
supported native MCP launch parameters, environment, or generated scoped
configuration files. There is no universal CLI flag across harnesses. Do not
modify users' persistent harness configuration or silently replace unrelated
MCP servers. Unsupported harnesses use the shared CLI; agents with neither
shell nor native tool access cannot invoke watches.

Distribute one Squid-owned watch skill to harnesses with skill discovery. Its
name and description explain when to use persistent monitoring; the full body
teaches explicit tool/CLI calls and successful-registration checks. Keep one
canonical skill source, with delivery adapted per harness rather than separate
model-specific instructions. Frontmatter matching does not guarantee use.

Use ADR-0046's context hierarchy and cadence for global/topic preferences and
any fallback discovery hint. Prefer native skill/tool discovery and on-demand
help over full descriptions every turn. Where needed, provide one minimal
shared entry point, `agentsquid tools --help`, at session start, capability
revision changes, or known context loss. Stateless invocations get that hint
when native discovery is unavailable. Keep changing execution constraints and
per-turn credentials current regardless of explanatory-text cadence. Do not
build separate watch-specific session memory. Tool schemas and skill metadata
also have context costs; enable only relevant capabilities and make delivered
context/configuration inspectable under ADR-0046.

Squid makes the CLI executable available in the turn's PATH. Per-turn
environment variables identify the issuing instance and carry a short-lived,
scoped credential, for example `SQUID_API_URL` and `SQUID_TURN_TOKEN`. The CLI
must not rediscover the server from a default home-directory config: concurrent
instances can have different ports and identities. Credentials stay out of
prompts, command arguments, transcripts, and logs; diagnostic output redacts
them. Remote transport must protect credentials in transit. MCP adapters use
the same scoped credential boundary through supported environment or protected
configuration delivery; no credential appears in model-visible tool arguments.
Generated credential-bearing files are restricted to the turn and cleaned up
on completion.

The server binds credentials to the turn, topic, allowed operations, and
expiry; it revokes them when the turn ends. Restart invalidates old turn
credentials, and later turns receive new ones. Polling uses server-owned
adapter credentials, never a finished turn's token. Neither a caller-supplied
topic nor an "I am the user" flag grants additional authority.

### Activation follows user authorization

A user's explicit watch request may activate a watch immediately when the
requested scope fits a server-recorded grant. An agent-generated idea without
that authorization is saved as `proposed`, consuming no polling or turn budget
until the user activates it. A topic may require proposals for all agent
creation, but this is an explicit policy, not an implicit extra confirmation
after every already-authorized request.

The server records the originating user message or authenticated UI/CLI
action and the granted scope. Model-supplied text claiming approval is not
proof. Until a grant can be established through the trusted control path,
creation remains proposed. Direct human CLI calls require user authentication
distinct from the scoped agent credential.

Review comments, issue descriptions, CI logs, and other external content are
task data. They cannot authorize new watches, broaden permissions, or override
the user's instructions. Watch-triggered turns cannot create further active
watches unless the user's grant explicitly permits that scope.

### Durable lifecycle owned by the server

Persist three entities alongside the existing execution store:

- **Watch:** target, topic, execution agent, instructions, policy/grant,
  status, polling cursor, next check time, deadline generation and due time,
  repeat/reset policy, watch expiry, limits, counters, and timestamps.
- **Event inbox:** source identity, target revision/head SHA, event details,
  observation time, and disposition (`pending`, `handled`, or `superseded`).
- **Watch run:** a claimed event batch, dispatch key, attempt, linked chat
  message IDs, expected head SHA, outcome, and any publication state.

Watch states are `proposed`, `active`, `paused`, `expired`, `completed`, and `cancelled`.
Waiting and running are observable execution phases of an active watch. A
completed agent turn does not complete its watch. Pause stops new dispatch;
cancel also prevents future monitoring. Stopping an already-running turn uses
the existing explicit cancellation mechanism. Publication rechecks current
watch authorization so cancellation or scope revocation blocks a later push.

The server owns polling and restart recovery. No idle agent process is kept
alive. Use persisted due times, conditional claims, and linked message IDs,
following ADR-0042's execution-store principles rather than reconstructing
state from transcript text. Reuse `agent/topic_queue.py` for normal turns;
do not create a separate agent runtime or change Flow retry semantics.

Persist observed events and the corresponding cursor advance atomically.
Source identities are unique per watch. "Seen" is distinct from "handled":
claiming or queueing an event does not acknowledge successful work. An event
is handled only after its run reaches the required successful outcome,
including publication when requested. Retries are bounded and visible.

After restart, reconcile claims against linked turns and external results
before retrying. A crashed process does not prove an action did not happen.
Guarantee durable ingestion and idempotent dispatch identity; do not promise
exactly-once external side effects. Ambiguous publishing outcomes require
remote-state reconciliation before another attempt.

### Generic observation probes

A source-specific plugin is optional. Register either an executable argv or a
server/tool pair on an existing central MCP service. Executables use existing
unattended host authentication; MCP uses a server-owned authenticated client.
Registration is trusted configuration, not arbitrary shell text from a watch.
Source access does not grant authority to act on detected changes.

Both transports accept `{target, cursor}` and return a bounded JSON object:
`{revision, changed?, summary?, url?, closed?}`. A provider wrapper maps raw
output into this contract. Revisions identify immutable source versions, including
comment edits and run attempts. Recurring labels (`ci:fail`) and content hashes
alone are insufficient: a later occurrence needs its own source version/attempt
ID. Re-observing an old version is a replay and retains its original event key.
Hashing previous plus current status does not distinguish repeated cycles.
Status-only probes without occurrence identities are not supported. Initial snapshots establish a quiet baseline
unless `changed: true` explicitly identifies actionable current state. Later
revision changes generate deterministic deduplicated events; `changed: false`
advances the baseline without dispatch. Adapters needing lossless individual
comments must reconcile pagination/history before returning a new revision;
snapshot comparison alone does not promise every intermediate change.

Polling uses no LLM. One central MCP service can cover Linear, Graphite, and
other sources through registered probes. Source-specific adapters remain useful
for richer predicates, completeness, and reconciliation. The ordinary request
turn resolves ambiguous corporate task references with its available context
and tools, then registers a canonical target and concrete handling scope.

### Poll first, enqueue only actionable changes

The first adapter watches one GitHub PR per watch, including PRs viewed through
Graphite. Poll PR state, CI results, and review feedback using authenticated
GitHub access; use conditional requests where supported. It needs no public
webhook endpoint. Polling intervals and backoff respect provider limits, and
checks do not invoke an LLM. API/authentication failures remain errors, not
evidence that a PR has closed or disappeared.

An initial snapshot evaluates current actionable failures and unresolved
feedback once. Record the baseline explicitly so successful checks and old,
already-resolved comments do not become new work. Subsequent terminal CI
failures and new actionable feedback enqueue ordinary turns. Passing checks
update status without launching a model. Merge or closure completes the watch.

Each turn receives current PR state, its head SHA, relevant comment bodies and
links, and bounded failing-job details/logs. Missing or truncated context is
explicit. Immediately before execution, refresh state and supersede CI work
for an obsolete head. Review feedback on older commits remains eligible until
resolved or judged no longer applicable; age alone is not dismissal.

The agent's push alone does not trigger another turn. CI results on that push
must still be observed. Deduplication and failure budgets prevent loops;
blanket suppression of events caused by the agent would hide failed fixes.

Serialize watch turns with topic work. Allow at most one active watch run and
one accumulated follow-up batch per watch; pending user messages take priority
over undispatched watch work. New events remain durable while a turn runs.
After user work or a head change, rebuild the pending batch from fresh state.
This does not imply preempting an already-running agent turn.

### Continuous watches and durable deadlines

An active watch can continue across any number of successful turns until its
stop condition, explicit cancellation, expiry, or budget limit. No new event
means no agent turn. Continuous monitoring is a server loop, not an LLM loop.

Support event triggers and deadline triggers. For example: "Watch this request;
if its recipient has not responded within four hours, mark the request expired;
if they respond, handle the response and stop watching." The adapter must
support the target and required response data; reject unsupported rules rather
than suggesting a PR adapter can monitor arbitrary requests.

A deadline rule specifies the qualifying response predicate (including actors
and event types), clock anchor, absolute UTC due time, timeout action, and
policy after a response or timeout: stop, continue, or reset after qualifying
activity. Use source timestamps where reliable and record observation time
separately. Continuous watches do not implicitly reset deadlines after every
poll, agent message, or CI update. Reset only on specified qualifying activity;
record the new due time and increment a deadline generation transactionally.

At due time, reconcile source state before atomically claiming the current
deadline generation and creating a timeout event in the durable inbox. Use a
unique identity per watch/rule/generation so restarts and concurrent checks do
not duplicate expiry actions. A queued timeout is invalidated if fresh state
shows a qualifying response at or before the deadline. Responses after the
deadline follow the declared late-response policy; do not silently undo an
already completed external action. If response timing cannot be established
or the provider is unavailable, retry reconciliation or pause, never infer
silence from an API failure. Immediately before an external timeout action,
recheck the rule and target and use conditional updates where supported; an
unavoidable provider race must be recorded and surfaced rather than described
as exactly-once behavior.

Persist due times, anchors, generations, and outcomes. Startup processes
still-valid overdue deadlines once after reconciliation. Pause suspends new
dispatch; resume reconciles missed activity and overdue deadlines using their
original clock, unless the user explicitly requests a reset. Do not replay a
burst of missed periodic actions: coalesce to the current valid generation.
Repeating rules specify their next anchor and cannot create unbounded
immediate retries.

Use a configurable 24-hour default watch lifetime, bounded by server policy.
Explicit expiry overrides the default. Return the absolute expiry in registration
responses; retries preserve it rather than extending the window. Downtime does
not extend expiry. Restart reclaims abandoned polling leases, reconciles current
state since the saved cursor, and coalesces missed intervals into one poll.
Pending events remain durable; run recovery follows linked turn identities.

Watch expiry (`expires_at`) stops monitoring and new dispatch; it is distinct
from a rule that changes an external request to an expired state. The latter
requires explicit mutation permission and a successful recorded action. At
watch expiry, pending undispatched work is superseded and publication is
blocked; a running turn follows the existing cancellation boundary. Evaluate
watch stop conditions and expiry before dispatching deadline work. A later
reconciliation cannot revive a completed or cancelled watch. Deadline actions
may be deterministic adapter operations or normal queued agent turns when
reasoning is required; both use the same scope, audit, and retry limits.

### Publication respects isolated turns

Watch agents edit only isolated roots and obey ADR-0025. They do not commit or
push from a file-editing turn. Publication is an explicit server-owned stage
after successful sync, under the watch's user-granted authority.

Bind publication to the configured backing repository, remote, branch, expected
PR head, and captured change set. Serialize repository mutations with other
Squid work. If a human changes the branch, remote head, or conflicting local
files, reconcile through the normal isolation mechanism or pause; do not
blindly pull, overwrite, force-push, or commit unrelated user changes. Never
publish internal per-turn branches.

A run is not published until the intended remote commit is verified. Persist
sync, commit, and push failures as distinct outcomes. This stage is a required
implementation milestone for autonomous fixes; creating a watch must not
advertise working automatic publication before it exists.

### Limits and visibility

Require finite turn/attempt and repeated-failure limits. Support expiry and a
cost ceiling where accounting is enforceable; do not claim a hard monetary
cap from delayed or estimated usage. Pause on exhausted limits, repeated
equivalent failures, unresolved publication conflicts, or unavailable required
credentials, with an actionable reason. Use bounded polling-error retries and
backoff before pausing for persistent failures.

Show target, execution agent, status, last/next check, next deadline, watch
expiry, repeat/reset policy, run count, granted scope,
and pause/resume/cancel controls on the topic. Each automatic turn records its
watch ID and triggering events. Lifecycle changes and notification records
are durable and use existing realtime publication conventions; the transcript
is the work record, not the scheduler database. Any confirmation UI uses
Squid's common themed modal.

Where a harness offers native scheduling, instruct it to use Squid watches
for this lifecycle. Do not assume those schedulers share Squid's persistence
or authorization, and do not add blanket blocking of unrelated tools in v1.

## Delivery and non-goals

Implement the server store/API, central tool registry, shared CLI and MCP
adapter, skill delivery with harness fallbacks, PR poller, queue/recovery path,
and lifecycle visibility. Include durable deadline infrastructure; expose
specific response/timeout rules only when an adapter implements their required
observations and actions. Autonomous publication
requires the separate sync/commit/push stage above. Until then, expose any
released capability accurately as monitoring or local work only.

Dedicated Linear issue adapters, inbound webhooks (potentially through Shore),
multiple targets per watch, distributed workers, automatic merge,
and general-purpose cron scheduling are later work. All adapters must feed
the same durable event and authorization boundary.

## Alternatives and consequences

- A long-lived CLI polling loop is simple for short waits but loses lifecycle
  ownership when the turn exits and consumes an agent slot while idle.
- A separate LLM classifier adds latency and cost without registering durable
  work. The normal agent can invoke an explicit operation instead.
- Independently maintained per-harness skills or native schedulers duplicate
  behavior. A centrally distributed skill and MCP adapters teach/expose the
  shared API while the server remains the lifecycle authority.
- One shared CLI works across shell-capable agents and keeps policy central,
  but requires credential delivery, durable recovery, provider adapters, and
  careful coordination with repository isolation and publication.

## Required verification

Verify cross-harness command availability and correct instance selection;
topic/token isolation, expiry, revocation, and redaction; proposal versus
authorized activation; policy narrowing; idempotent creation; atomic event
ingestion/cursor updates; duplicate polls and dispatch claims; restart before
and after turn preparation and publication; event arrival during active work;
user-message priority; initial baseline handling; stale CI versus unresolved
old review comments; self-push CI results; human-push conflicts; publication
scope and unrelated-file protection; cancellation before push; retry/limit
pauses; provider outages; and durable UI state. Crash tests must distinguish
observed, queued, handled, and remotely published outcomes.

Also verify central registry translation and topic narrowing across harnesses;
MCP/CLI authorization parity; native discovery and CLI fallback; on-demand skill
loading; context-loss handling without full per-turn descriptions; generated
configuration cleanup; and observable tool/configuration provenance. Deadline
tests cover no-response expiry, qualifying and nonqualifying activity, exact
boundary and late responses, source outages, timeout/response races, generation
resets, restart with overdue work, pause/resume, repeated rules, coalesced
catch-up, watch expiry versus external expiry, and cancellation before mutation.

## Implementation progress (2026-10-30)

Delivery status: **partial implementation, unavailable to users**. The internal
observation core is complete for its current scope; the full watch lifecycle
is not complete. Restarting the server does not start monitoring or handling
comments. Keep ADR status `proposed`; internal scaffolding does not satisfy
this ADR's end-to-end verification requirements.

The durable internal store/loop and review fixes are implemented, including
operation-key replay, cursor preservation, failure reset, idle lock avoidance,
and pause reasons. `agent/watch_probes.py` adds bounded executable probes and
an injected MCP-client bridge with one normalized revision contract. Creation
defaults to 24 hours and retries preserve the original expiry.

These components are internal: production registry/configuration, a concrete
MCP transport/client, grants, API/CLI, lifespan startup, durable run dispatch,
and publication remain pending. No user-facing automatic watch is enabled.

Probe timeout handling supports the declared Python 3.10 minimum via
`asyncio.wait_for`. Loop registration validates that the probe timeout plus
the one-second cleanup allowance is strictly below the loop timeout. Cleanup
kills the process group and closes pipes on timeout or cancellation; detached
descendants cannot hold cleanup open indefinitely. Event validation uses the
same JSON encoding and 64 KiB payload limit as persistence.
