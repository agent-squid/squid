# Implementation plan: transparent context, shared tools, and persistent watches

Status: implementation in progress. Updated: 2026-11-01.

Design authorities: [ADR-0046](../decisions/0046-layered-runtime-and-user-context-injection.md)
and [ADR-0053](../decisions/0053-persistent-watches-and-shared-agent-tools.md).
Neither ADR nor this plan implies the capabilities already exist. Names for
new files, endpoints, tables, and flags below are proposed contracts. Existing
symbol names are navigation anchors, not promises about their final placement.

## Progress (2026-10-31)

- Milestone 1 runtime foundation complete: packaged templates, strict startup
  validation and caching, typed blocks, assembly after worktree resolution,
  compatibility helpers, and exact prompt parity. Focused verification: 80
  tests passed, 144 execution combinations matched prior prompts, wheel
  templates rendered successfully. Restart validated through `/health`.
- Milestone 1 remaining: unified optional-layer assembly and global user-context
  inheritance. Cadence selection is implemented and tested; production runtime
  templates use every-turn and do not yet use delivery-history persistence.
- Milestone 4 started independently of milestone 3: internal durable observation
  store and bounded polling loop in `agent/watches.py`. Includes idempotent
  proposals, versioned transitions, leases, atomic cursor/event ingestion,
  deduplication, expiry, bounded retries/backoff, and pause after five errors.
  Review fixes preserve cursors on empty observations, reset the failure budget
  on resume/activation, distinguish manual/error pauses, and avoid idle write
  locks. Lifecycle operation keys replay the original result even after later
  transitions; unkeyed requests still enforce versions. Creation keys remain
  reserved for the lifetime of the row; new watches require new keys. Adapter
  event keys must include source revisions/attempts. Diagnostics log only the
  error type and watch ID, never raw exception text.
  Verification: 155 context/memory/queue/Flow/watch tests passed, including
  fake-clock expiry/backoff, simultaneous claims, interrupted-poll recovery,
  and transaction rollback. No production adapter is registered and
  no server task starts this loop: watches are not available to users yet.
- Generic probe foundation implemented in `agent/watch_probes.py`: registered
  executable argv with JSON stdin, bounded output, timeout/cancellation cleanup,
  and an injected server-owned MCP client using the same revision contract.
  First snapshots baseline quietly unless explicitly actionable; immutable
  revision keys deduplicate repeats. Unknown/malformed output fails the poll.
  Internal creation now defaults to 24 hours; idempotent retries retain expiry.
  This is an MCP client bridge, not a shipped MCP transport or user registration.
  Verification: 70 probe/watch/context tests passed, including executable limits,
  timeout cleanup, MCP argument routing, restart deduplication, and expiry retries.
- Probe review fixes: shared storage-size validation, process-group cleanup with
  bounded pipe draining and cancellation-safe closure, Python 3.10-compatible
  timeouts, and registration checks reserving cleanup time within the loop
  timeout. Revision semantics explicitly require source versions/attempt IDs;
  status-only cycles are unsupported. Focused verification: 78 tests passed on
  Python 3.10, including recurring statuses with distinct attempt identities.
- Test MCP fixture implemented: `tests/fixtures/watch_mcp_server.py` exposes
  `task_snapshot` over real stdio using the official SDK v1 (`dev` dependency
  group only; pinned below v2). Tests control comment revisions, closure,
  delay and errors through an atomically replaced state file. Coverage includes
  negotiation/discovery, structured output, timeout recovery, and reconnecting
  a new fixture/client against durable cursors/events. This is not production
  transport wiring or a full Squid server restart test.
- Streamable HTTP fixture coverage added alongside stdio, following the
  [official SDK v1 transport API](https://py.sdk.modelcontextprotocol.io/v1/).
  The same six transport cases cover discovery, comment updates/edits, closure,
  provider errors, timeout recovery, and durable deduplication after a fresh
  server/client connection. HTTP uses an ephemeral loopback socket with bounded
  startup/shutdown. This initial fixture slice omitted authentication and
  broken-live-session recovery; production polling remains unwired. Focused MCP,
  probe, and watch verification: 65 tests passed on Python 3.10 and 3.11.
- Internal [trusted probe registry and concrete MCP client](../watch-probe-registry.md)
  implemented: strict server/probe mappings, optional SDK extra, fixed command
  argv, HTTPS/loopback URL validation, environment-referenced bearer credentials,
  registered tool enforcement, structured-result bounds, and sanitized failures.
  Every poll opens a fresh session; endpoint outage/restart and timeout recovery
  are covered over real transports. OAuth and wire-size bounds remain deferred.
  Registry loading was initially explicit; host integration is recorded below.
  Verification: 96 focused tests passed on Python 3.10 and 3.11; lockfile checked.
- Pre-publication review fix: redact MCP/HTTP SDK logging within probe tasks,
  including exception and cleanup paths. Regression coverage reproduces malformed
  stdio output leaking through SDK logs and verifies concurrent diagnostics and
  cancellation cleanup retain the correct logging scope. Verification: 99 focused
  tests passed on Python 3.10 and 3.11.
- Additional review fixes: optional MCP tests skip without the SDK; probe
  registrations cap output limits at 65,536 bytes; missing/invalid credentials
  preserve a distinct safe error type; HTTP timeouts follow probe configuration.
  Verification: 109 focused tests passed on Python 3.10 and 3.11; without the
  optional SDK, 92 passed and the MCP transport module skipped.
- Host configuration integration: `watches.enabled` (false by default) and
  trusted `watches.registry` now validate at startup and in the config editor.
  Disabled registries expose no adapters; loading never opens transports or
  starts polling. Authorization, API/CLI, and dispatch remain pending.
  Verification: 124 focused watch/config/context tests passed on Python 3.11.
- Internal observation authorization slice: strict global/topic narrowing,
  bounded observation lifetimes, durable exact-watch grants, hashed instance-local
  turn credentials, and shared create/list/show/lifecycle service. Grant checks
  run inside lifecycle transactions, including keyed replays; revocation pauses
  active observation and invalidates polling leases. Server lifespan creates and
  closes the service. See [authorization boundary](../watch-authorization.md).
  Trusted grant/token controls have no authenticated transport yet. No launch
  hook delivers/revokes turn tokens; no user-facing registration is enabled.
  Verification: 296 watch/service/config/context/server tests passed on Python
  3.11, including real MCP transports.
- Milestone 3 observation transport slice: authenticated HTTP and JSON CLI,
  separate operator control authentication for exact-watch grants/revocation,
  lazy client entry point, scoped one-shot/native-shell environment delivery,
  credential revocation on terminal/closure paths, and minimal CLI discovery.
  Persistent harness refresh and native MCP delivery remain unsupported.
  `/tools` reports actual availability; activation/resume return 409 until the
  production loop is wired. Registration remains proposed, never active
  monitoring. See [authorization and transport contract](../watch-authorization.md).
  Verification: 503 watch/context/queue/runner/server/Flow tests passed on Python
  3.11; 60 focused API/CLI/service/config tests passed on Python 3.10. Wheel
  entry point and both client help commands passed without importing server
  configuration; lockfile checked. No live server restart or polling was claimed.
- Milestone 3 review fixes (2026-10-31): self-reexec preserves only the
  operator verifier, consumed at startup and stripped from harness children;
  watch API/control and launch mint/revoke run off-loop with cancellation-safe
  worker completion. Grant IDs validate before SQL. Non-loopback plaintext
  listeners skip unusable turn credentials with a safe warning; resumed and
  slash-command prompts receive no extra discovery hint. Human grant audits
  record shared-operator key identity and exact scope. Revoking one grant
  preserves active observation when another covers the full lifetime. Existing
  foreign-key enforcement and cascade deletion are regression-tested.
  Verification: 527 watch/context/queue/runner/server/Flow tests passed on Python
  3.11; 84 focused tests passed on Python 3.10. Restart coverage intercepts the
  real command's exec boundary; no live server restart was performed. Strict
  startup configuration/credential validation remains intentional.
- Follow-up review fixes (2026-10-31): launch and CLI share one standalone
  URL validator, including loopback aliases and explicit malformed-URL rejection.
  Queue delivery passes original request text separately from the assembled
  runtime prompt so slash-command discovery suppression works in production.
  Regression coverage exercises the real queue/context path, raw-prompt fallback,
  normal requests, and accepted/rejected URLs through both launch and CLI.
  Verification: 543 watch/context/queue/runner/server/Flow tests passed on Python
  3.11; 96 focused watch API/CLI/queue tests passed on Python 3.10. Lockfile checked.
- Milestone 4 authorized observation slice: lifespan starts/stops a bounded
  `WatchLoop` when enabled. Policy, full-lifetime grants, expiry, and claim
  ownership are checked transactionally at claims and ingestion and before I/O.
  Authenticated activation/resume now works while the worker is running. `/tools`
  reports monitoring only; agent execution and publication remain disabled.
  Pending events persist without acknowledgement. Real-host subprocess coverage
  checks `/health` boot-time changes, quiet baselines, saved cursor/expiry,
  restart replay deduplication, a later source change, and cancellation.
  Verification: 559 watch/context/queue/runner/server/Flow tests passed on Python
  3.11; 98 focused watch polling/API/service/store tests passed on Python 3.10.
  Both runs include a disposable real server and executable probe. Lockfile checked.
- Milestone 4 generic local-work dispatch slice: opt-in execution policy and
  authenticated exact-watch local-work grants with agent/root identity and finite
  budgets; durable runs/event membership and atomic message links; ordinary topic
  queue execution with user priority and per-topic watch coordination. Source
  refresh and scope checks precede execution. Completion proof and clean sync are
  required before acknowledging only captured events. Ambiguous started work
  pauses for reconciliation. Generic orphan recovery excludes linked watch turns.
  Publication and external mutations remain disabled. No production configuration
  is changed. Disposable Echo-host coverage includes restart replay and run limits.
  Verification: 583 watch/context/queue/runner/server/Flow tests passed on Python
  3.11; 159 focused tests passed on Python 3.10, both including disposable real
  hosts. Final focused controls checks passed (97 tests), followed by 40 final
  dispatch/polling checks covering shutdown and real-host recovery; lockfile checked.
- Initial disabled-mode verification (2026-10-31): production configuration at
  that point had no `watches` section. Loading that configuration confirmed
  `enabled: false`, no exposed adapters, and no monitoring or dispatch worker.
  The running server reported `/health` status `ok` at version `0.1.6rc26`;
  `/`, `/topics`, `/queue`, and `/processes` return HTTP 200. Full Squid suite:
  1,133 tests passed on Python 3.11 (two dependency deprecation warnings).
  This verifies disabled-mode compatibility; it is not a new restart, live
  agent-turn, or production watch activation test. No configuration was enabled.
- Operator credential-file support and restart review: enabled cold startup
  without an environment override provisions `~/.squid/watch-control.token`
  (owner-only regular file, mode `0600`). Human CLI operations use this file and
  `--api-url` without requiring exported variables. Agent operations still need
  scoped turn credentials. Explicit environment credentials take precedence;
  self-restart then preserves the inherited verifier before file access, so
  existing operator keys do not silently rotate. Regression tests cover missing
  and existing key files and rejection of invalid restart verifiers. Latest full
  Python 3.11 suite: 1,140 passed, two dependency deprecation warnings.
- Production smoke-test preparation: global observation/execution switches and
  the synthetic `prod-smoke-clock` probe were configured. A dedicated
  `watch-prod-test` topic narrows policy to observation only, a 180-second maximum
  lifetime, and a 30-second minimum poll interval. Production proposal creation,
  denial of ungranted activation, and cancellation passed; active polling with
  file credentials remains unverified. See the [smoke-test configuration and
  limits](../watch-authorization.md#bounded-observation-smoke-test). No live
  agent execution or publication was tested or authorized by this preparation.
- Agent-built observation-plan v1 implemented: host MCP connections with explicit
  read-tool approval, on-demand tool/plan schemas, real-read validation and preview,
  immutable plan/schema/connection bindings, fixed sanitized setup errors, and
  normal authorized polling/dispatch. Bounded snapshot plans support up to four
  independent reads, JSON Pointer extraction, source identities and equality
  predicates. Legacy probes remain compatible. Pagination, step bindings and
  automatic stale-head reconciliation remain pending; no production settings
  or publication behavior changed. See [supported contract](../watch-observation-plans.md).
  Verification: full Python 3.11 suite passed (1,158 tests; two dependency
  deprecation warnings). Coverage includes actual stdio discovery against two
  provider response shapes and disposable-host plan polling/local-work dispatch,
  `/health` boot-time restart recovery, replay deduplication and run limits.
  No production watch or provider account was used.
- Observation-plan review hardening (2026-11-01): schema validation uses an
  isolated worker with a three-second budget and an explicit deny-all retrieval
  registry (including dynamic references). Polling and execution refresh recheck
  authorization before subsequent operations; service locks run off-loop. MCP
  sessions/catalogs are reused within one observation, create retries return the
  saved watch independently of provider availability/schema drift, and `plan` is
  always reserved. Catalog-limit errors, argument-scope trust and the first-poll
  quiet baseline now have explicit feedback/documentation. Regression coverage
  includes revoke/cancel/expiry between reads, execution refresh revocation,
  external-reference HTTP traps, a pathological regex, lock contention, session
  reuse and offline idempotent retry. Verification: 131 focused tests and the
  full Python 3.11 suite (1,172 tests) passed; two dependency deprecation warnings
  remain. Real-host restart checks use `/health` boot time; production settings
  and Shore files were unchanged.
- Next watch slice: richer observation plans (design below), richer source-specific predicates
  and stale-head reconciliation, and operational UI/ambiguous-run resolution.
  Deadline and publication infrastructure remain separate milestones.
- Milestones 2 and 5–8 remain unimplemented. No automatic publication is enabled.

## Watch completion boundary

Authorized observation is available behind `watches.enabled` (false by default).
Lifespan polls registered probes only for active watches with durable grants.
Milestone 4 and autonomous watch handling are **not complete**: generic local-work dispatch is opt-in and separately granted. No dedicated PR
adapter, deadlines, or publication stage is shipped.

| Component | Current status |
| --- | --- |
| Durable watch store, claims, expiry, retries, cursor/event ingestion | Implemented and unit-tested |
| Executable probes and injected MCP-client bridge | Implemented and unit-tested |
| Python 3.10/3.11 compatibility and review regressions | 96 focused tests passed on each runtime |
| Trusted probe configuration and concrete MCP transport/client | Configured probes polled through authorized lifespan worker |
| Observation policy/grants and scoped service | Implemented with authenticated controls and one-shot turn delivery |
| API/CLI registration | Proposals, grants, activation/resume and versioned controls implemented |
| Server lifespan startup/shutdown wiring | Implemented with cancellation and awaited cleanup |
| Durable run batching, queue dispatch, acknowledgement and recovery | Generic local-work path implemented; ambiguous outcomes pause |
| Real-server restart and end-to-end automatic handling | Observation and opt-in Echo dispatch/restart/budget coverage implemented |

Next completion target: register one authorized watch through the service,
observe a GitHub PR change through an agent-built plan, dispatch a bounded
ordinary topic turn, reconcile stale CI heads and review feedback, and inspect
its durable outcome through operational controls. Generic probe/Echo coverage
already verifies dispatch, captured-event acknowledgement and restart without
duplicated work. Verify restart through `/health` or `BOOT_TIME`.
Keep execution opt-in; require source-specific validation before claiming
autonomous PR handling. The
observation-only switch remains opt-in; do not mark the whole milestone complete
based on observation tests.

## Delivery order and dependencies

Implement the shared context foundation first, then one complete PR watch.
Completing every part of ADR-0046 is not a prerequisite for the watch engine.
The engine/store can be developed independently of context UI and MCP wiring.

| Milestone | Depends on | Reviewable outcome |
| --- | --- | --- |
| 1. Runtime templates and assembly | Inventory | Existing turns receive equivalent, inspectable instructions |
| 2. Context audit and viewer | 1 | Users can inspect added context and distinguish historical changes |
| 3. Tool policy, scoped API, CLI | 1 | A turn can register an authorized watch through one shared interface |
| 4. Durable engine and generic observation plans | 3 | PR changes enqueue bounded ordinary turns across restarts |
| 5. Deadline rules | 4 | Supported response windows survive downtime and trigger once per generation |
| 6. MCP and skill delivery | 3; use 4 for end-to-end validation | Supported harnesses discover the same centrally configured tools |
| 7. Lifecycle UI and recovery coverage | 2, 4; 5 for deadline controls | Users can inspect, pause, resume, and cancel watches |
| 8. Controlled publication | 4, 7 | Authorized fixes sync, commit, and push with verified remote outcomes |

Milestones 5 and 6 can follow the first working PR flow independently. Until
8 is complete, label released watches as monitoring or local-work capability;
never promise automatic publication. Each milestone needs the focused tests
below before enabling its behavior. Keep feature flags off by default until a
working vertical slice and recovery tests pass.

## Existing implementation touchpoints

- `agent/memory.py`: `oneshot_protocol_prompt_block`,
  `code_roots_prompt_block`, and `topic_memory_prompt_block` provide compatibility/topic helpers; runtime wording
  lives in `agent/runtime/` and is assembled by `agent/context.py`.
- `agent/server.py`: `_prepare_chat_turn` prepares context and messages;
  `stream_response` dispatches normal work; `_lifespan` owns maintenance tasks.
- `agent/topic_queue.py`: `TopicDispatcher`, queue items, and worktree setup
  provide execution and isolation. Preserve existing completion/persistence
  paths rather than bypassing them for watches.
- `agent/stats_db.py`: `_connect`, `init_db`, session context tracking, durable
  Flow records, and realtime persistence provide storage conventions.
- `agent/flow.py`: reference for conditional claims and recovery, not a second
  scheduler to copy wholesale or a reason to alter Flow retry policy.
- `agent/harnesses.py`, `agent/runners.py`, `agent/sandbox_home.py`: harness
  capability checks, launch configuration, environment, and scoped homes.
- `agent/context_sync.py`: inspect its legacy MCP cleanup before adding new
  managed configurations; new assets must not be deleted by old cleanup rules.
- `agent/worktree.py`: `sync_after_turn` and existing conflict handling are the
  publication integration boundary.
- `pyproject.toml`: CLI entry point and packaged assets. `agent.server:main`
  currently owns the `agentsquid` command; preserve existing startup/auth paths.
- `ui/app.js`, `ui/style.css`, `ui/index.html`, and `ui/sw.js`: inspect actual
  asset locations and current UI structure before editing.

Related plans cover context shelf/arming and bare/echo transparency. Preserve
their request eligibility and deduplication rules; this plan does not make all
global pins automatically inject into every topic.

## Milestone 1: extract wording and centralize assembly

1. Inventory all Squid-added prompt strings and delivery paths, including
   ordinary chat, resumed sessions, ad hoc turns, Flow, and native shell.
   Record owner, source, conditions, and current cadence. Separate instructions
   from provider-specific formatting and computed execution facts.
2. Add a small template loader/renderer, for example `agent/context.py`, and
   packaged runtime Markdown under `agent/runtime/`. Move one-shot, code-root,
   and isolation wording first, preserving effective behavior.
3. Validate frontmatter and named substitutions at load/render time. Unknown
   conditions, missing required values, or malformed templates fail explicitly;
   do not silently start an isolated agent without its required context.
4. Produce typed context blocks: ID, owner, source, revision/hash, cadence,
   reason, required flag, delivery channel, and rendered text. Render current
   isolated paths only after worktree resolution. If preparation precedes that
   resolution, carry unresolved typed inputs until final launch assembly.
5. Preserve order: runtime, global user context, topic context, request context,
   user request. Use existing session identity/injection tracking where useful.
   Update delivery tracking only after dispatch accepts the relevant context;
   failed preparation must not suppress it on retry.
6. Keep current execution facts every turn. Session-start guidance is resent
   after revision changes or known context loss. With uncertain continuity use
   a minimal fallback hint. Stateless invocations have no remembered delivery.
7. Add global user-context configuration with explicit topic inheritance and
   overrides. Keep existing topic memory selection and bare/echo eligibility.
   Memory content cannot enable a disabled server permission.
8. Include templates in wheel builds; test rendering from an installed package
   layout, not just a source checkout. Avoid duplicating wording in legacy
   helper functions; make compatibility helpers delegate during migration.

Acceptance: equivalent required instructions reach supported execution paths;
fresh paths update on consecutive isolated turns; unrelated optional context
does not appear; malformed required context visibly prevents launch.

## Milestone 2: context provenance and inspection

Persist an additive context manifest linked to the prepared message/turn. Store
block metadata and runtime variables, not credentials or full repeated rendered
snapshots. Reuse stored message references for pins/lookback. Store source hashes
for user-owned content and reconstruct runtime text only when template revisions
match. Expose bounded turn-detail queries for these records.

The viewer shows source, owner, inclusion reason, cadence, and actual delivery
channel. Distinguish “sent this turn,” “previously delivered to this session,”
and “available through native discovery.” For edited/missing sources show
“changed since this turn” or “historical text unavailable”; never present current
text as the original. Do not invent visibility into schemas loaded internally
by a harness. Existing messages without manifests remain readable as legacy.

Before UI edits, identify and state the exact existing turn-details parent
element and context/composer parent being extended. Reuse the common themed
modal/viewer. After changing app JS, CSS, or HTML, bump all five cache version
strings across `sw.js` and `index.html` under the repository's existing rule.

Acceptance: inspect a fresh, resumed, ad hoc, and legacy turn; edit memory and
confirm accurate historical labeling; confirm tokens/environment secrets are
absent from both stored records and the viewer.

## Milestone 3: central tool registry, authorization, API, and CLI

### Policy and authorization

Define a validated global tools section in `squid.yaml` with topic-level
narrowing. Declare supported transports and adapters, operations, default/max
poll intervals, turn/attempt limits, expiry constraints, and publication scope.
Defaults must not silently authorize writes. Reject unknown policy fields.

Implement one service layer, for example `agent/tools.py`, used by HTTP, CLI,
MCP, and UI. Bind grants to authenticated user actions and originating messages.
A message reference is provenance, not proof that its text authorizes every
requested action. Use a structured authenticated grant/policy control path;
when no existing grant covers the request, create a proposal. Explicit grants
already made must not lead to repetitive confirmation prompts.

Mint opaque per-turn tokens at launch. Store only verifiable credential state,
bind it to instance, turn, topic, operations, expiry, and grant ceilings, and
revoke on success, failure, cancellation, or timeout. Process restart invalidates
prior credentials. Review all exception/logging paths for redaction. A shell
agent's ambient filesystem access is outside what an API token alone can
sandbox; describe and test the API boundary without overstating containment.

### API and command contracts

Proposed HTTP operations:

| Operation | Endpoint | Important behavior |
| --- | --- | --- |
| Discover | `GET /tools` | Effective operations and transport availability; no secrets |
| Create | `POST /watches` | Idempotent request key; returns ID, status, effective scope |
| List/show | `GET /watches`, `GET /watches/{id}` | Topic authorization, pagination, bounded run/event detail |
| Pause/resume/cancel | `POST /watches/{id}/{action}` | Version-checked transition; idempotent repeat |
| Activate proposal | `POST /watches/{id}/activate` | Authenticated human grant or existing trusted activation policy |

The proposed create payload includes target, observation plan (or legacy adapter), topic, execution agent,
instructions, triggers, requested actions, poll interval, stop conditions,
deadline rules, watch expiry, limits, request key, and grant reference. Return
structured failures for unsupported predicates, insufficient scope, invalid
agents/targets, and stale versions. Never accept raw SQL, arbitrary shell
polling commands, or caller-supplied authority assertions.

Add `agentsquid tools --help` and `agentsquid watch` subcommands without changing
the normal server startup command. CLI mutations accept structured JSON/file
input for complex rules; emit JSON, nonzero error codes, and sanitized stderr.
Use `SQUID_API_URL` and `SQUID_TURN_TOKEN` from the issuing instance; no fallback
to another instance's home config for agent calls. Human CLI authentication is
separate. No command prints bearer tokens as a diagnostic convenience.

Acceptance: two instances route correctly; cross-topic IDs cannot be queried
or changed; expired/revoked tokens fail; grants cannot be broadened; duplicate
creates return one watch; existing server/login/pair commands still work.

## Milestone 4: durable engine and agent-built observation plans

### Storage and claims

Add migrations through existing database initialization. Proposed entities:

| Entity | Essential fields and constraints |
| --- | --- |
| `watches` | ID, topic, adapter, canonical target, agent, instructions, grant/policy, lifecycle state, version, cursor, next poll, expiry, limits/counters, last error |
| `watch_events` | Watch ID, unique source event key, source/observed times, head/revision, bounded payload, pending/handled/superseded disposition |
| `watch_runs` | Unique dispatch key, watch, state/lease, attempt, message IDs, expected head, outcome, publication state |
| `watch_run_events` | Batch membership; an event cannot belong to two unreconciled active runs |

Persist transitions and durable realtime notifications atomically using the
existing outbox conventions. Index due active watches, pending events, and
unfinished runs. Poll network calls outside database write transactions;
commit fetched events and cursor advancement together with version checks.

Add a lifespan-owned maintenance task with bounded concurrency, provider
timeouts, jitter/backoff, and cancellation on shutdown. Persist due times;
sleeping tasks are wakeup optimizations. Conditional claims/leases protect
against overlapping ticks. Support the current single-server deployment;
this is not a distributed worker rollout.

### Generic observation plans: next implementation slice

Design change (2026-10-31): replace mandatory per-tool probe configuration with
agent-built declarative plans, as specified in ADR-0053. The full design remains planned work;
the API now accepts the v1 snapshot plan subset documented above; richer plan features remain pending. Keep existing probes and
all observation/execution grants working. No production enablement, migration
activation, or publication is authorized by this document.

Implement in these reviewable steps:

1. **Durable tool discovery.** Inspect existing host/harness connection ownership
   and reuse it where possible. Expose topic-permitted read operations, schemas,
   result formats, availability and limits through shared discovery. Identify
   which tools the server can call after the originating CLI exits. If no host
   connection exists, return a specific connection/access setup requirement.
   Do not assume native harness tools or session tokens are reusable by Squid.
2. **Plan schema and evaluator.** Define a versioned JSON contract for operation
   references/arguments, bounded step bindings, extraction, pagination, event
   identities, filters, baseline and stop predicates. Implement only a bounded
   declarative vocabulary, with structured and JSON/text result handling explicitly
   advertised. Reject unsupported parsing/predicates; never run generated code.
   Map tool output into the existing observation/event boundary.
3. **Validation and preview.** Add scoped API/CLI operations to validate a plan
   using real authorized reads and show a bounded baseline/event/completeness
   preview. Static checks precede I/O; proposal creation alone starts nothing.
   Enforce operation policy independently of model claims or tool annotations.
   Return stable sanitized errors for missing access, schema mismatch, absent
   event identities, unsupported conditions and exhausted pagination limits.
4. **Persistence and authority.** Store immutable plan versions/digests, target,
   connection references, validation evidence and cursors through existing DB
   migrations. Bind observation grants and validation to the exact plan; changing
   read scope requires revalidation and matching authorization. Include the plan
   digest in creation idempotency/conflict checks. Preserve legacy adapter rows,
   grants and restart behavior; never silently convert or activate them.
5. **Polling and dispatch.** Execute persisted plans from the lifespan worker
   using host-owned credentials after the request process exits. Recheck current
   policy/access before reads and ingestion. Reuse bounded retries, atomic event
   ingestion, queue priority, separate local-work grants and run recovery.
   Unchanged polls launch no model. Schema drift and incomplete observations
   must not advance completeness cursors or falsely satisfy stop/deadline rules.
6. **Agent discovery and vertical verification.** Teach the shared CLI help and
   eventual MCP/skill surface to discover, build, validate, save and activate a
   plan, then report its actual status. Test "watch this PR and resolve issues"
   and an unrelated issue source through the same evaluator with different tool
   argument/result shapes; no per-tool YAML or custom wrappers. Use a disposable
   host and fixture credentials; verify after the originating process exits and
   after restart using `/health` or `BOOT_TIME`.

Acceptance includes multi-tool PR state/CI/reviews, edited feedback, distinct CI
attempts and repeated status cycles, pagination limits, quiet/actionable initial
state, closure, stale heads, revoked access, schema changes, malformed/oversized
results, plan tampering, cross-topic references, secret redaction and restart
replay. Assert no LLM invocation on unchanged polls. Exercise a source that cannot
supply required data and verify actionable setup feedback instead of a fabricated
watch or generic "configure a probe" response. Keep optional optimized adapters
as extensions; dedicated GitHub or Linear implementations are not prerequisites.

Finish service expiry policy: configurable 24-hour default/max and absolute
expiry in responses; the internal store already implements the default and
retry preservation. Downtime never renews the lifetime. Expiry must block new
dispatch and publication; pending-event disposition and running-turn behavior
still need dispatch integration.

### PR observation and execution

Use the generic plan for reads and event normalization; reuse shared interfaces
for canonicalization, current-state reconciliation and prompt materialization.
External mutations remain outside observation-plan execution. The first plan validation case accepts GitHub PR URLs, including the
underlying PR of a Graphite workflow. Resolve ambiguous Graphite-only links
explicitly rather than guessing repositories. Authenticate with the existing
host GitHub mechanism; if using `gh`, invoke argv directly with timeouts and
bounded output. Do not interpolate external content into shell commands.

Normalize CI failures by check/run identity and attempt, and feedback by stable
comment/review identity plus revision where needed. Handle pagination before
advancing a completeness cursor. Initial observation includes actionable current
failures/unresolved feedback once, not all historical comments. Successful CI
does not launch an agent. Provider errors do not mean closed or silent.

One service reconciles pending events, checks limits/expiry, refreshes head,
claims one batch, prepares linked chat messages idempotently, and dispatches
through the normal topic queue. Audit whether current per-agent lanes permit
concurrent same-topic turns; add a topic coordination boundary for watch work
where needed. Pending human messages outrank undispatched watch work. Refresh
the watch batch at execution time, since queued state can already be stale.

Allow one active run and one accumulated pending batch per watch. Events that
arrive while running stay pending. Bind CI work to the current SHA; keep old
review feedback until resolved or superseded on its merits. Include bounded
job logs, comments, source links, and explicit truncation notices in prompts.
Label external content as task data, never authorization.

On terminal agent success, acknowledge only the event IDs captured by that
run; do not consume events that arrived later. If publishing is required, wait
for verified publication before marking handled. Retries create new attempts
under the same logical batch identity and consume finite counters. Pause on
repeated equivalent failures, limit exhaustion, or unrecoverable conflicts.

Recovery reconciles leases with linked messages, queue/process state, worktree
outcomes, and any external action before reclaiming. Never blindly retry a
turn because the in-memory queue disappeared. Persist ambiguous outcomes and
surface them for reconciliation rather than promising exactly-once side effects.

Acceptance: a failed check triggers one ordinary turn; self-push CI remains
observable; events batch under load; head changes invalidate obsolete CI;
server restart loses neither pending events nor run identity; human work takes
priority; merge/closure stops new work; loops hit a visible finite limit.

## Milestone 5: durable deadlines and continuous response windows

Add persisted deadline rules with ID, response predicate, anchor source/time,
UTC due time, generation, reset/repeat/late-response policies, timeout action,
and outcome. Index current due generations. Validate rule support against the
adapter; PR support does not imply arbitrary issue or request mutation support.

At due time, fetch authoritative qualifying activity before claiming a timeout.
Atomically create a synthetic inbox event keyed by watch/rule/generation.
Recheck again before acting; invalidate queued timeouts when a response at or
before the due time is found. Apply the declared late-response policy for later
activity. API errors mean unresolved state, not absence of response.

Resets increment generation and replace the due time only for qualifying
activity. Persist original anchors through pause/resume; on resume/startup
reconcile overdue generations and coalesce catch-up instead of replaying an
action storm. Repeating rules must have a future next anchor and still obey
finite action/run budgets. Purely deterministic adapter actions do not need an
LLM but use the same claim, grant, and audit path.

Separate watch `expires_at` from an external `mark_expired` action. Watch expiry
stops monitoring/dispatch and blocks publication; external expiry requires a
grant and recorded remote result. Check watch stop/expiry before deadline work.
Where provider conditional writes are unavailable, record the possible response
race and surface conflicting outcomes; do not invent atomicity across systems.

Acceptance: exercise no response, boundary/late response, nonqualifying activity,
reset, provider outage, pause/resume, overdue restart, repeat limits, cancel
before action, and expiry/action collision using a fake clock and adapter.

## Milestone 6: native MCP and shared skill discovery

Expose the service operations as compact structured MCP tools. Select an MCP
transport and SDK after checking supported installed harness versions; record
the compatibility matrix and any dependency in `pyproject.toml`/`uv.lock`.
Use a scoped local proxy or authenticated HTTP transport so every call retains
the issuing turn's identity. Transport must not grant extra operations.

Add capability declarations to harness metadata: supported MCP config delivery,
skill discovery location, shell fallback, and limitations on resume. Generate
per-turn launch config in scoped directories; preserve user MCP servers, clean
credential-bearing files on all completion paths, and ensure legacy context
cleanup cannot delete new active configuration. Resumed/persistent processes
must receive refreshed authorization through a supported mechanism; otherwise
reconnect/restart them or use the CLI fallback, never retain a stale token.

Deliver one canonical watch skill, packaged and synchronized only to managed
harness locations. Concise frontmatter advertises monitoring, CI/review feedback,
and response-window workflows; full instructions require explicit registration
and honest status reporting. For unsupported discovery use a minimal shared
tools hint through milestone 1's context assembler. Do not maintain independent
per-model skill logic or repeat complete descriptions every turn.

Acceptance: API/CLI/MCP operations have equivalent scope and outcomes; supported
harnesses can discover/register/list a watch; unsupported paths fall back
explicitly; resume does not leak or reuse credentials; secrets never enter tool
schemas or prompts; native schema overhead is measurable, not assumed zero.

## Milestone 7: watch operations UI

Choose and state the exact existing topic-header/status parent before adding
a watch badge, and the existing drawer/modal parent for watch details. Reuse
the common themed modal for proposal activation and controls. Show active vs
proposed status, target, agent, granted actions, last/next check, next deadline,
expiry, budget/counters, pending/running work, and pause/error reason.

Link each automatic turn to the watch run and triggering event details. Use
durable notifications/realtime snapshots; reconnect or browser refresh must
not fabricate an active monitor from old DOM state. Keep cancellation separate
from explicit stop of an already running agent and explain that distinction.
Use versioned mutations to reject stale controls cleanly. Observe the five
cache-string update rule after any UI asset edits.

Acceptance: refresh/reconnect, proposal activation, pause/resume/cancel races,
expired credentials, and completed watches display authoritative state. Context
viewer and watch UI expose no secret configuration.

## Milestone 8: controlled sync, commit, and push

Do not relax isolated-turn Git restrictions. Add a server-owned publication
record linked to watch run, intended repository/remote/branch, expected head,
captured changes, sync result, commit SHA, push result, and remote verification.
Reject publication requests outside the user's grant. Do not publish changes
from another watch, topic, turn, or unrelated dirty file.

Serialize backing-repository mutations. Check cancellation/expiry, grant
revocation, branch identity, local changes, expected remote SHA, and isolation
conflicts at each boundary. Use the existing sync/conflict path; don't blindly
pull or force-push to solve human changes. Commit only the captured successful
change set, push only its authorized branch, then verify the remote SHA.

Persist progress before external actions where possible. On restart after
commit/push, inspect local/remote results before repeating. Mark events handled
only after the required publication outcome. A successful no-change run may
acknowledge feedback without a synthetic commit; record its disposition.

Acceptance: sync conflict, unrelated dirty changes, human push, branch switch,
revocation, crash before/after commit/push, and remote timeout all preserve
user work. The agent's successful fix starts CI whose later failure can trigger
another bounded run. No internal per-turn branch reaches the remote.

## Verification, rollout, and rollback

Use focused unit/service tests for context rendering, authorization, transitions,
cursor transactions, claims, and deadline generations. Use fake clocks and
provider fixtures for deterministic scheduling tests; simulate crashes between
durable boundaries. Add queue integration tests and Playwright coverage for
context inspection/watch controls. Avoid real publication in default tests;
use temporary local remotes for Git race/recovery coverage.

Run existing context/session, Flow, worktree, and server tests affected by each
milestone. Add installed-package smoke checks for CLI routing/templates/skills.
Manually exercise native adapters against supported installed harness versions
with a disposable target before declaring cross-harness support. Verify server
restart recovery through `/health` or `BOOT_TIME`, not process identity alone.
Update API/data-model and realtime docs with shipped contracts, and annotate
ADR implementation status by milestone rather than marking the entire feature
accepted after only scaffolding lands.

Roll out behind independent context, tools/watch, and publication switches.
Start with a disposable PR, short bounded polling, and tiny turn limits; enable
writes only after publication checks pass. Disabling watches stops claims and
mutations while retaining durable records. Running turns and already-started
external actions need reconciliation, not deletion. Migrations are additive;
older code must not be allowed to resume unknown active work after rollback.

Completion means a user can authorize a watch, see exactly what it permits,
close the UI, restart Squid, observe a new event or deadline launch the expected
ordinary turn, inspect its context/outcome, and stop the watch without losing
events or publishing unrelated changes. Continuous waiting itself runs no LLM.


## Watch flow execution targets (2026-11-01)

Status: bounded same-topic sequential execution implemented. Grants persist a
canonical `flow_route` and all-member/plan revision. Watch runs link durable Flow
runs and per-step start/message evidence; native Flow advancement/recovery skips
watch-owned runs. Predecessor context, isolated execution, authorization/source
refresh, full-flow acknowledgement and conservative partial recovery are covered.
Validation: 125 focused watch/API/Flow tests, followed by the full Python 3.11
suite (1,186 tests, two dependency deprecation warnings), passed. Disposable-host
Echo tests cover two members per run, provider-shaped MCP plans and legacy probes,
`/health` boot-time restart recovery, replay deduplication and run limits. No live
agent, production configuration, Shore files or publication behavior changed.

Implement ADR-0053's explicit `agent` / `flow_route` target union before enabling
flow execution. Reuse the Flow parser/plan/store and topic queue; retain existing
single-agent behavior. Support only same-topic sequential 2–8-step plans without
delays, forks or joins initially. Keep production configuration unchanged.

1. Extend grants with a canonical route and all-member/plan fingerprint; migrate
   existing grants unchanged. Validate route scope, members and finite budgets.
2. Persist a deterministic flow-run link and per-turn start/message evidence.
   Watch dispatch owns advancement; prevent ordinary Flow continuation/recovery
   from independently starting watch-owned steps.
3. Execute steps in order via normal preparation/queue/worktree paths, with pinned
   predecessor context and authorization/source refresh before every turn. One
   watch attempt consumes one run budget, with at most eight agent turns.
4. Settle only after all linked steps prove completion and clean sync. Stop on
   errors, revoked authority, expiry, cancellation or user priority; pause partial
   ambiguous outcomes on restart rather than replaying a successful prefix.
5. Verify agent compatibility, route rejection and idempotency, changed member
   configuration, end-to-end two-agent Echo dispatch and acknowledgement, step
   failure/revocation, priority, run limits, recovery, and no native double dispatch.
   Update CLI help and the operator contract with concrete flow-grant input.


### Flow deletion review fixes (2026-11-01)

Implemented immutable watch ownership on Flow runs, atomic topic-deletion
cancellation, nullable flow-turn message references, and active-flow cancellation
and pause during filtered agent-history deletion. Migration preserves existing
start evidence, backfills legacy ownership and cancels orphaned watch flows.
Regression coverage includes native takeover after deletion, all history filters,
queued reviewers, completed outcomes, rollback and upgrade/idempotency behavior.
Verification: 103 focused tests and the full Python 3.11 suite (1,195 tests) passed,
with two dependency deprecation warnings. Production configuration and publication
remain unchanged.
