# Watch observation policy and authorization

The shared watch service supports observation and separately granted local-work
turns through HTTP and `agentsquid watch`. Both `watches.enabled` and
`policy.execution_enabled` default to false. Creation returns an observation
proposal. Observation activation/resume requires a full-lifetime observation
grant and a running polling worker; a stopped worker returns
`409 watch_monitoring_unavailable`.

Automatic local turns additionally require a distinct exact-watch execution
grant, an explicitly selected configured agent, instructions, finite run/attempt
limits, and execution enablement in effective global/topic policy. Observation
authority never authorizes a model turn. Publication, external mutation grants,
deadlines, and dedicated PR semantics remain unavailable.

## Host policy

Trusted `squid.yaml` settings extend the existing registry:

```yaml
watches:
  enabled: false
  registry:
    servers: {}
    probes:
      snapshot:
        argv: [/absolute/path/to/read-snapshot]
  policy:
    operations: [create, list, show, activate, pause, resume, cancel]
    adapters: [snapshot]
    min_poll_seconds: 60
    max_lifetime_seconds: 86400
    execution_enabled: false
    max_runs: 3
    max_attempts: 1
  topics:
    example:
      operations: [create, list, show, cancel]
      max_lifetime_seconds: 3600
```

Omitted policy permits the supported observation lifecycle operations and
registered adapters, with a 30-second polling minimum and 24-hour maximum
lifetime. The top-level enablement switch still defaults to false. Topic
policies intersect operation/adapter allowlists, increase polling minimums, and
reduce lifetime ceilings. They cannot enable globally disabled watches or
expand scope. Default expiry is the lesser of 24 hours and the effective maximum;
idempotent creation preserves the original absolute expiry. Saved configuration takes effect after restart. The worker reconciles current
policy and durable grants inside claim and ingestion transactions and immediately
before source reads. It pauses unauthorized active watches with `policy_changed`
or `grant_required`, invalidates their claims, and rejects late observations.

## Trusted control versus agent operations

`WatchService` derives topic and operations from a credential, never from a
caller-supplied topic or user assertion. It provides bounded list/show, idempotent
proposal creation, and versioned lifecycle operations using the existing store.
Activation and resume additionally require an unrevoked durable grant for the
exact watch, explicitly included in the credential's scope. Grant checks run
inside the lifecycle write transaction and before replaying keyed operations.
Pause/cancel require their respective token operations but no activation grant.
Responses omit internal polling lease tokens.

`WatchControl` is a **trusted Python host interface**, not an authenticated
transport and never an agent tool. Authenticated human controls validate a structured observation grant before
calling `record_grant`. The recorded
`user_action` is an audit reference, not proof that arbitrary referenced message
text authorizes monitoring. An agent cannot call the service to mint credentials,
record grants, or broaden permissions. The human HTTP/CLI control path described below records grants. A separate authenticated activation/resume action attaches only
the requested grant to a temporary credential, then uses the same lifecycle
service. Default agent launch credentials carry no grants, so an agent cannot
activate a proposal merely by supplying a grant ID in its arguments.

Grants cover only the referenced watch's immutable target/adapter/topic and its
recorded observation lifetime; they authorize no agent execution or external
mutation. Revocation durably marks the grant. If no other unrevoked grant
covers the watch through its expiry, it atomically pauses active observation
and clears its lease so a late provider result cannot be ingested. Another
full-lifetime grant preserves active observation. Once paused, a new grant and
authorized resume are required to continue. Records survive restart.
Durable lifecycle notifications remain a separate integration task.

## Credential lifetime

Each service instance keeps only SHA-256 hashes of random opaque credentials.
Tokens bind a turn, topic, operation allowlist, grant allowlist, and expiry of at
most one hour. A fresh server lifespan creates a fresh service, invalidating
old tokens; shutdown closes the service and clears its credentials. One-shot launch integration revokes credentials on success, failure, timeout,
cancellation, and generator closure. The topic queue supplies instance-specific
`SQUID_API_URL` and `SQUID_TURN_TOKEN` through the child environment, with a
one-hour maximum token lifetime. Persistent interactive harnesses receive no
watch credential pending a supported refresh mechanism. Native shell turns
receive the scoped environment too. Inherited control/turn credentials are
removed from child environments.

No credential is persisted in the watch/grant database or included in service
responses. The credential registry uses no diagnostic token output. HTTP errors and CLI diagnostics use fixed sanitized errors. The CLI accepts
HTTPS or loopback HTTP only (including loopback aliases such as `127.0.0.2`),
using the same URL validator as turn launch. It follows no redirects and ignores proxy environment
settings. The server rejects credentials over non-loopback plaintext requests. This API boundary does not sandbox an agent's filesystem or shell.


## HTTP and CLI contract

The console entry point dispatches `watch`/`tools` before importing server or
home-directory configuration. Agent calls require explicit `SQUID_API_URL` and
`SQUID_TURN_TOKEN`; there is no fallback server discovery. `agentsquid tools`
returns effective topic operations/adapters and reports the running worker’s
monitoring and dispatch availability, execution budget ceilings, and local-work
scope. Publication remains explicitly disabled. `agentsquid tools --help` and
`agentsquid watch --help` describe the client. Enabled one-shot model launches
receive a minimal discovery hint and the executable directory on PATH. Hints
are omitted for resumed sessions and slash commands, detected from the original
request supplied by the topic queue before runtime/context assembly. Plaintext listeners bound
to a specific non-loopback address disable turn tool delivery with a safe
warning, rather than issuing unusable credentials. Configure a loopback or
wildcard listener for the current local client transport.

| Agent endpoint | Behavior |
| --- | --- |
| `GET /tools` | Effective observation capability and availability |
| `POST /watches` | Idempotent proposal; adapter, target, request_key, optional poll_interval/expires_at |
| `GET /watches` | Topic-bound list; limit 1–100 and nonnegative offset |
| `GET /watches/{id}` | Topic-bound public row; no poll lease credential |
| `POST /watches/{id}/{action}` | Version-checked lifecycle; activate/resume require a running worker and scoped grant |

Mutation bodies reject unknown fields and exceed neither 65,536 bytes nor the
underlying service limits. Lifecycle JSON includes `version` and optional
`operation_key`/`grant_id`. Conflicts return 409, denied credentials return 403,
and malformed/unsupported requests return 400 without echoing request data.

For human controls, enabled cold startup without an environment override provisions
`~/.squid/watch-control.token` with a random key and mode `0600` if missing.
The server and human CLI require an owner-only regular file owned by the current
user; symlinks and unsafe permissions are rejected. The CLI reads this file only
for explicit `--human-topic` operations. Agent operations still require their
scoped turn credential. The file is operator authority: any process with access
to the same user's files can read it; this is not a filesystem sandbox boundary.
Do not copy it into prompts, transcripts, source control, or shared storage.

`SQUID_WATCH_CONTROL_TOKEN` remains an optional override (32–512 characters).
Startup consumes that override and strips it from harness children. Otherwise
a self-restart validates and preserves the inherited verifier before reading or
creating a file. Built-in restart carries only the verifier, never the plaintext
secret; it does not silently switch an environment-provided key to a file key.
Cold starts without an override use the file, so file-based operation needs no
exported key. File-key changes take effect on a cold start.
Disabled startup does not create a key file.

Human CLI commands accept `--api-url` instead of requiring `SQUID_API_URL`:

```sh
agentsquid watch list --human-topic squid --api-url http://127.0.0.1:8000
```

The explicit URL still selects the instance; the credential file does not
implicitly discover a server. Remote operators can continue to supply their
matching credential through the environment. Credentials never appear in
command arguments or normal output.

Human endpoints live under `/watch-control/{topic}/watches`, supporting the
same create/list/show/lifecycle paths. Additional `/{id}/grant` accepts only
`{"expires_at": UTC_UNIX_SECONDS}` and records an exact-watch observation grant
with a server-generated authenticated action ID, host-control key fingerprint,
operation, topic, watch ID, and granted expiry. This identifies the shared
operator credential, not an individually authenticated person. `/{id}/revoke` accepts only
`{"grant_id": ID}` and verifies both topic and watch binding. Human credentials
cannot access agent endpoints and turn credentials cannot access human controls.
These endpoints are not added to Shore capabilities in this slice.

Examples (instance URL supplied through `--api-url` or `SQUID_API_URL`):

```sh
agentsquid watch create --input proposal.json
agentsquid watch list
agentsquid watch show WATCH_ID
agentsquid watch grant WATCH_ID --human-topic TOPIC --input grant.json
agentsquid watch activate WATCH_ID --human-topic TOPIC --input activation.json
agentsquid watch cancel WATCH_ID --input cancel.json
```

JSON mutation files may be replaced with `--input -` for stdin. All successful
commands emit JSON; failures return a nonzero exit code and sanitized stderr.
Execution-agent selection and local-work budgets are recorded through the
separate human execution-grant control below. Deadline rules, external mutation
grants, and shared native MCP delivery remain later slices; unsupported fields
are rejected explicitly.


Blocking watch service and control operations run in worker threads, including
launch mint/revoke and shutdown. Cancellation waits for an outstanding worker
before credential cleanup, preventing a delayed mint from surviving revocation.
Grant IDs are validated before SQL; malformed identifiers return a sanitized
client error. Foreign keys are enabled by the existing shared database connector,
so deleting a watch cascades to its grant records.


## Bounded observation smoke test

The production test topic is `watch-prod-test`. Its deployment policy narrows
the global settings as follows (these are not shipped defaults):

```yaml
watches:
  enabled: true
  registry:
    probes:
      prod-smoke-clock:
        argv:
          - /absolute/path/to/squid/.venv/bin/python
          - -c
          - 'import json,time; print(json.dumps({"revision": str(int(time.time())//30), "summary": "Production smoke-test clock tick"}))'
        timeout: 5
        output_limit: 1024
  topics:
    watch-prod-test:
      execution_enabled: false
      adapters: [prod-smoke-clock]
      min_poll_seconds: 30
      max_lifetime_seconds: 180
      max_runs: 1
      max_attempts: 1
```

Use a stable absolute interpreter path, not a per-turn isolated worktree path.
Merge this example into existing watch configuration rather than replacing
other registered probes or policies. Save through the configuration editor/API;
watch settings take effect at startup. Migrating from an environment-provided
key to the automatic file requires a cold stop/start without that override:
self-restart intentionally retains the existing verifier. Run lifecycle commands
outside an agent turn that the stop would terminate.

The probe reads no external service and returns one immutable revision per
30-second clock interval. Polling launches no model. The first snapshot quietly
establishes a baseline; a later revision records a pending change event.
`min_poll_seconds` is a lower bound, not a global schedule: create the watch with
`poll_interval: 30` for this test. The 180-second ceiling bounds each watch's
lifetime from creation (including time spent proposed), not the server or topic
lifetime. Omitted expiry uses that ceiling; retries and restarts do not renew it.
Topic execution is disabled even if global execution is enabled.

Using `--human-topic watch-prod-test --api-url http://127.0.0.1:8000`, create a
proposal with adapter `prod-smoke-clock`, a test target, a unique request key, and
`poll_interval: 30`. Grant observation through the returned `expires_at`, activate
with the returned grant ID and current version, then show the watch until its
cursor advances. Cancel with its latest version when finished. Without an
execution grant no model turn is authorized; this topic additionally disables
execution by policy. Expiry or cancellation stops further polling. Observation
records remain durable for inspection.

Production checks so far cover loaded settings, proposal creation, denial of
activation without a grant, and cancellation. Active polling with file credentials
has not yet been verified. This synthetic test establishes no GitHub PR semantics,
agent execution, or publication support.

## Observation lifecycle and recovery

Lifespan starts one bounded `WatchLoop` only when globally enabled and cancels
and awaits it before closing the service. Registered executable/MCP probes use
server-owned authentication. Per-turn tokens are never retained for polling.
Loop timeouts include the configured probe timeout and its cleanup allowance;
provider failures use existing bounded retries/backoff and sanitized diagnostics.

For activation, `activation.json` contains `version`, `grant_id`, and an optional
`operation_key`. The grant must be unrevoked and cover the watch’s entire expiry.
Pause stops new polls and invalidates in-flight observations. Resume reconciles
from the saved cursor without renewing expiry. Cancel prevents further polling.
Expiry supersedes pending events and blocks late ingestion. Closed source results
complete the watch. Lifecycle changes remain readable through list/show; topic
UI and durable lifecycle notifications are still later work.

Observed revisions and cursor advancement commit atomically. Events stay
`pending` for observation-only watches; separately authorized execution handles
only successfully completed captured batches. Shutdown cancels
probe I/O and preserves outstanding leases. On restart, abandoned claims are
reclaimed after lease expiry, overdue checks coalesce into one poll, and replayed
source revisions retain their event identity. Downtime does not extend lifetime.
A disposable real-server test verifies a changed `/health` boot time, saved cursor
and expiry, durable pending events, replay deduplication, a subsequent change,
and cancellation. It makes a saved poll overdue to avoid a wall-clock interval
sleep; the execution variant also verifies local-turn dispatch and budgets. Neither
variant claims exactly-once external effects or publication.


## Explicit execution grants and local work

Opt in through `watches.policy.execution_enabled: true`, with global `max_runs`
and `max_attempts` ceilings (defaults 3 and 1). Topic policy can disable execution
or lower ceilings; it cannot expand them. Configuration takes effect on restart.
The existing observation grant and activation are still required. No production
configuration is changed by implementing this feature.

An authenticated operator may call
`POST /watch-control/{topic}/watches/{id}/execution-grant` with exactly:

```json
{
  "agent": "configured-agent-name",
  "instructions": "Inspect the observed change and make an authorized local fix",
  "max_runs": 3,
  "max_attempts": 1,
  "request_key": "operator-chosen-idempotency-key"
}
```

The response identifies a `local_work` grant. The grant covers this immutable
watch target/topic and original expiry, selected agent/runtime/cwd, and topic
code roots. Agent or root configuration changes invalidate execution until a
new explicit grant is recorded. No commit, push, publication, comment posting,
merge, or external mutation is authorized. Normal isolated-turn preparation and
sync apply; this API boundary does not sandbox the agent's ambient filesystem
or shell. Watch-turn instructions explicitly restrict scope and label source
content as untrusted task data.

Only one unrevoked execution grant may exist for a watch. An identical request
key replays the original grant without renewing its expiry or undoing revocation;
conflicting fields return 409. To revoke, send `{"grant_id":"ID"}` to
`/execution-revoke`; revocation pauses active monitoring and blocks a later
start. Grant/revoke are human-only controls and never agent token operations.
Default agent turn tokens carry no activation grants and cannot create further
active watches from a triggered turn.

```sh
agentsquid watch execution-grant WATCH_ID --human-topic TOPIC --input execution.json
agentsquid watch execution-revoke WATCH_ID --human-topic TOPIC --input revoke.json
agentsquid watch runs WATCH_ID
agentsquid watch runs WATCH_ID --human-topic TOPIC
```

Run listing is topic-authorized and bounded (`limit` 1–100, HTTP default 20; the shared CLI limit defaults to 100; nonnegative
`offset`). `GET /watches/{id}/runs` and the human-prefixed equivalent expose run
identity, batch, attempt record, linked messages, state, and outcome. `attempt`
is the dispatch record's sequence within a batch and can include safe deferrals;
deferrals consume no model or failure budget. Grant IDs and messages do not
constitute new authority.

## Durable dispatch, priority, and recovery

The lifespan-owned dispatcher commits one bounded captured event batch and
stable run identity before preparing messages. User/assistant message pairs,
run links, and realtime records are committed atomically; preparation replay
returns the same pair. A partial unique index permits one unresolved active run
per watch. New observations remain pending while a run executes.

Watch work yields to pending user messages, including requests still being
prepared, and coordinates across all topic agent/provider lanes. User turns
retain their existing concurrency with one another. A watch turn runs alone
within its topic and does not preempt an already-running user turn. User work
arriving before the agent starts safely defers the batch. Automated preparation
does not change the topic's human-selected sticky agent or last prompt.

Immediately before execution, the ordinary topic worker rechecks grant, policy,
agent/root identity, expiry, budget, and pending user work, then refreshes the
registered source. Refresh failures pause before model execution. A closed
source stops the watch without running a model. Fresh data is bounded and
explicitly marked if truncated; source-specific stale-head handling belongs to
the future dedicated PR adapter. Refresh does not acknowledge newer events or
advance the polling cursor outside its durable ingestion transaction.

Only a successful linked ordinary turn with a durable completion event, no
error event, and no unresolved sync conflict handles the captured event IDs.
Partial text or `status=done` alone is insufficient. Newer events are not consumed.
Recorded failures retry only within finite attempt and watch-wide run ceilings;
failed preparation counts toward limits. Budget exhaustion pauses visibly.

Startup reconciles watch records independently of generic orphan handling,
even when execution was disabled after restart. Known successful completion is
acknowledged; unstarted work can be safely retried. Started work with uncertain
completion or sync outcomes pauses for reconciliation and is never blindly
retried, even after an observational resume. Inspect the run/messages and local
state; no automatic resolution control is shipped for ambiguous runs. Cancel
and recreate a watch only after establishing a new safe scope. Shutdown settles
known evidence, cancels owned refresh probes, drains queued watch work, and uses
the normal process cancellation mechanism. Existing Flow retry semantics are
unchanged. This is one server/dispatcher per database, not distributed workers.

A disposable real-server test with the opt-in Echo harness verifies one event
launches one normal turn, restart replay launches none, a later event launches
one more, and a finite run budget stops another dispatch. No real coding agent,
network source, Git publication, or production instance is used in that test.

Worktree blockers defer execution without consuming the model budget. The durable
run outcome records `worktree_blocked`; dispatch waits 60 seconds before trying
that watch again, including after restart. Observation polling continues during
this delay, and authorization is checked again before execution.


## Flow execution targets

Execution grants now accept exactly one `agent` or `flow_route`, using the same
authenticated human control path. A flow grant binds its same-topic sequential
plan and every member's configuration/roots. One finite watch-run budget unit
covers an entire 2–8-turn flow attempt. Partial ambiguous runs pause rather than
replaying a completed prefix; events require successful completion and clean sync
of every step. See [flow grant input and limitations](watch-observation-plans.md#run-a-flow-for-each-event-batch).
Observation and publication authority remain separate; no publication is enabled.


Watch-flow ownership survives deletion of its watch: it is recorded permanently
on the Flow run. Deleting a topic atomically cancels watch flows and pending
steps before removing watch state. Clearing an agent's selected history stops
and pauses affected active flows; per-step message references become null while
start evidence is retained. Successfully completed outcomes are preserved.
Native Flow scheduling cannot take over these flows after deletion. Upgrades
backfill legacy ownership and cancel orphaned watch flows.
