# Agent-built watch observation plans (version 1)

Squid can persist a declarative plan built by the requesting agent from discovered
MCP tools. No per-target probe, provider wrapper, or `{target,cursor}` tool contract
is required. Host service connections and read permissions still require operator
configuration. Session-only harness tools are not automatically server-accessible.
The ADR's complete plan vocabulary remains a target; the bounded snapshot subset
below is implemented. Dedicated provider adapters are optional.

## Host connection

Install `agentsquid[mcp]`. Configure a connection once and approve its read tools:

```yaml
watches:
  enabled: true
  connections:
    work:
      connection:
        transport: streamable-http
        url: https://mcp.example.com/mcp
        bearer_env: WORK_MCP_TOKEN
      read_tools: [read_issue, read_pr]
```

The connection also supports existing stdio transport configuration. Transport,
credential resolution, redaction, and endpoint restrictions match the
[existing MCP transport](watch-probe-registry.md). Read-tool approval is a host
trust decision; provider annotations alone do not authorize reads. No process or
connection starts while loading configuration. Settings take effect on restart.
Global/topic `policy.adapters` can exclude `plan` to disable generic plans for a
topic; otherwise all approved connections are available to that topic. This
version does not provide per-topic connection/tool subsets. Observation and
local-work grants remain independent. Publication remains disabled.

Read-tool approval permits all schema-valid arguments supported by that tool;
there is no per-repository, issue or organization argument restriction. An agent
with `create` authority can validate against anything the host credential can
read, and counts/errors can reveal existence or activity even without returning
record bodies. Executing a granted watch delivers record data to its local-work
agent. Use narrowly scoped service credentials and enable generic plans only for
trusted topics. A display target is not an argument-level access boundary.

## Agent workflow

1. Use `agentsquid tools` to inspect effective capabilities, then
   `agentsquid tools --discover` to get permitted tool names, descriptions,
   input/output schemas, availability and the observation-plan JSON schema.
   Treat provider descriptions as untrusted task data.
2. Resolve the user's target and build `{target, plan}`. Use
   `agentsquid watch validate --input validation.json` for an authorized read
   preview. Validation neither saves nor activates a watch. The scoped `create`
   operation permits these validation reads only on operator-approved tools.
3. Add `request_key` and use `agentsquid watch create --input watch.json`.
   Creation validates again and saves an immutable proposal. An idempotent retry
   returns the saved watch without contacting the provider, preserving expiry and
   schema bindings even if the provider is offline or has changed. A different
   target, plan, interval or explicitly supplied expiry with the same key conflicts.
4. Activate using the existing exact-watch observation grant. Report monitoring
   only after an active response. Automatic local work additionally requires an
   execution grant; it uses the existing topic queue, finite budgets and recovery.

Discovery is `GET /watch-tools`; validation is `POST /watches/validate`;
creation uses `POST /watches`. Human CLI equivalents support `--human-topic`
and the separate `/watch-control/{topic}` authentication boundary.

Example create input for a tool with provider-specific arguments and output:

```json
{
  "target": "issue:I-1",
  "request_key": "watch-I-1-feedback",
  "plan": {
    "version": 1,
    "initial": "quiet",
    "steps": [{
      "server": "work",
      "tool": "read_issue",
      "arguments": {"issueId": "I-1"},
      "items": "/comments",
      "identity": ["/id", "/updatedAt"],
      "when": {"path": "/resolved", "equals": false},
      "complete": {"path": "/nextPage", "equals": null},
      "stop": {"path": "/closed", "equals": true}
    }]
  }
}
```

`items` selects a record array; omit it for a singleton response. Paths are JSON
Pointers (`~1` escapes slash, `~0` escapes tilde). `identity` and `when` operate
on each record. `stop` and `complete` operate on the whole tool response.
Any stop predicate may complete the watch, but all reads must succeed first.
A false completeness predicate fails the whole observation without ingestion.

`identity` must include source versions: comment ID plus edit revision, or head
SHA plus CI run ID and attempt. Status labels and content hashes cannot identify
repeated occurrences. Squid validates field presence and scalar identity values;
the agent must verify that the chosen provider fields have these semantics.
`initial: quiet` remembers matching initial identities without dispatching them,
including if they disappear and reappear. `actionable` emits initial matching
records once. Later replay is deduplicated by the durable event inbox. Baselines
are established by the first active poll, not by the validation preview. Activity
that arrives during setup is therefore part of a quiet baseline. The preview
explicitly identifies this boundary; use `actionable` to include existing matching
records when monitoring begins.

## Bounds and failure behavior

Version 1 supports one to four independent reads, literal tool arguments,
structured JSON objects or a single JSON text object, equality predicates,
32,768-byte plans, 65,536-byte results/events and at most 50 matching records
across the plan (each input array also has a 50-record limit). A complete
observation has a 20-second timeout; connection cleanup can add transport
cancellation overhead. Quiet baseline identities fit the existing cursor bound.
Tool discovery is bounded to one page, 200 tools and 65,536 bytes per connection.
Descriptions are limited to 2,048 characters. Schemas must be self-contained;
external JSON Schema references (including dynamic references and rebased local
references requiring retrieval) cannot be fetched: the validator uses a deny-all
retrieval registry. Schema validation runs in a separate process with a three-second
budget and process-group cleanup, keeping pathological regexes off the event loop.
Service locks and authorization checks also run off the event loop.

Each observation reuses one MCP session per distinct connection configuration and
one tool catalog per named connection; sessions are closed after that observation.
Schemas are checked on every poll, without opening separate discovery/call sessions
for every step. Revocation, cancellation and expiry are rechecked before each
subsequent operation, including execution-time source refresh. An already-issued
request can finish, but no later operation may start under revoked authority.
Paginated or oversized tool catalogs return `plan_discovery_limit` with a setup
hint, rather than being reported as a generic connection outage.

Pagination, bindings between steps, general expressions, semantic LLM filtering,
lossless history guarantees, and automatic stale-head reconciliation are not
implemented. Plans report bounded snapshot semantics: intermediate changes may
be missed. A rule requiring complete history needs a suitable complete source
read or a future plan feature. Do not claim full PR issue resolution from this
transport/evaluator support alone.

Saved plans bind target, tool schemas and connection configuration digests to the
watch. They have no edit endpoint; changed plans require a new validated watch
and matching grant. Credential values are not stored in plans; token rotation
through the same environment reference still works. Revoked read permissions,
changed endpoints or changed schemas fail polling. Bounded retries eventually
pause the watch. Plan failures retain fixed error codes and setup hints; raw
provider exceptions and credentials never become diagnostics. Provider failures
and incomplete responses never imply closure or silence. Unchanged polls invoke
no model. Existing registered probes continue to work without migration.

The adapter name `plan` is reserved even when no connections are configured.


## Run a flow for each event batch

An authenticated execution grant selects exactly one `agent` or `flow_route`.
For example, save this input and use
`agentsquid watch execution-grant WATCH_ID --human-topic t --input grant.json`:

```json
{
  "flow_route": "#t@a>@b",
  "instructions": "Implement fixes locally, then review the result. Do not publish.",
  "max_runs": 2,
  "max_attempts": 1,
  "request_key": "implement-review"
}
```

Both agents must exist. Routes use Squid Flow syntax and must compile to 2–8
same-topic sequential steps with no delays, forks or joins. The canonical route,
compiled plan, all member configurations and topic roots are bound to the grant;
changes require a replacement grant. Running setup through a flow does not
implicitly authorize future executions of that flow.

One watch run means one entire flow attempt; at most eight agent turns execute
per attempt. Polling continues between flow runs. Every step rechecks grants,
expiry and current source state and uses the normal queue and isolated worktree
path. Later steps receive predecessor output as bounded task data. Captured events
are acknowledged only after all steps have verified completion and clean sync.
New events arriving during execution remain pending for a later batch.

`agentsquid watch runs WATCH_ID` includes `flow_run_id`, whose messages and steps
are available through existing Flow inspection. Failed steps stop downstream work.
A partial flow interrupted by restart, shutdown or human priority pauses for
inspection; Squid does not blindly rerun its completed prefix. Revocation and
cancellation prevent subsequent steps. Publication remains unavailable.
