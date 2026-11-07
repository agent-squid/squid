# Bounded agent-callable routes (ADR-0054 milestone 5)

Routes reuse the existing Flow parser, durable `flow_runs`/`flow_steps` store,
and normal topic workers. They schedule future local work; the issuing CLI
process does not remain alive. A launch returns a durable ID and `waiting`,
not a completion claim. Successful parent completion **and every repository's
successful sync** must be recorded before any route turn can be claimed.

## Explicit rollout

Both `worktree.enabled: true` and `routes.enabled: true` are required. Routes
are disabled by default. No runtime configuration, harness registration, or
publication setting is changed automatically. Restart the server after editing
host configuration.

```yaml
routes:
  enabled: false
  max_turns: 16
  max_depth: 4
  max_seconds: 1800
  turn_seconds: 300
  concurrency: 4
  topic_concurrency: 8
  global_concurrency: 32
```

Limits are positive integers. Hard configuration ceilings, respectively, are
64, 8, 86400, 3600, 16, 32 and 128. Existing roots retain their saved limits;
a lower current limit takes precedence. Raising configuration never expands an
accepted root's allowance.

## Supported subset

Validation uses the existing Flow grammar. Immediate `>` handoffs and
single-round `<>` routes support origin broadcast/join, target fan-out and
round-trip target joins where the parser supports them. Every participant must
be a configured agent in the issuing topic with the **same captured code roots
and configured working directory** as the issuer. Participant runtime revisions
are frozen and rechecked at admission and before execution.

This first exposure rejects cross-topic routes, delays, repeats, multiple
handoff edges, task-bound turns, watch origins, ordinary Flow origins, and
continuation origins. A direct human one-shot turn can launch one route. A
managed route member can launch one nested route **or** one predeclared local
continuation sequence. A route cannot be combined with a continuation on the
same parent turn. Unsupported input is rejected, not rewritten into another
route.

Managed turns retain the existing Flow fresh-session (`!`) semantics.
Route instructions preserve the original human request
and distinguish it from generated work instructions. Managed routes and their
continuations support local inspection, edits and checks only. They cannot
schedule `backing_git`, `task_review` or `task_publication`. Existing recorded
watch publication remains a separate feature; this exposure adds no watch
execution or publication authority. These checks constrain Squid scheduling;
they are not an OS sandbox for arbitrary agent shell effects.

## CLI and discovery

Credentials are inherited in the issuing one-shot launch environment as
`SQUID_API_URL` and `SQUID_CONTINUE_TOKEN`, never supplied in JSON or prompts.
The client uses only that instance URL and refuses redirects and ambient proxy
configuration.

```sh
agentsquid route capabilities
agentsquid route validate --input - <<'JSON'
{"route":"#project@codex>@review,@test"}
JSON
agentsquid route launch --input - <<'JSON'
{"request_key":"review","route":"#project@codex>@review,@test","prompt":"Review and test the requested fix"}
JSON
agentsquid route list
agentsquid route show <id>
agentsquid route cancel <id>
```

Discovery returns schema version 1, implemented operation schemas, captured
scope, rollout state, executable state and blockers, effective limits, and
shared root budget information for managed members. `agentsquid tools` also
uses this discovery when a continuation credential is present. Existing watch
observation discovery stays available with `agentsquid tools --discover`.

The POST validation response includes the canonical route and compiled plan;
validation alone creates no execution or reservation. Launch has an idempotent
request key and payload scoped to the issuing turn. Identical retries return
the same ID; a different payload conflicts. IDs, topics, budgets and provenance
are derived by the server and cannot be supplied by the caller. Topic-scoped
list/show responses exclude prompts, credentials, grant proofs and scope JSON.

## Shared budgets and lifecycle

The complete Flow plan reserves its turn count atomically with acceptance.
Nested routes and declared continuation steps reserve from the **same root**.
Cancelled or failed plans retain these turn reservations. Depth increases for
nested routes and each successive continuation step; fan-out members share
their route's nesting depth. Each started claim retains a unique charge key.

Admission atomically reserves a root/topic/global concurrency slot and at most
`turn_seconds` from the root's remaining cumulative time allowance. The
reservation clock includes preparation and queue wait. The worker's timeout
and fresh-session retries use the remaining reservation, not a new timeout.
Verified worker completion refunds unused reserved time and releases its slot.
Closing a dispatch stream or delivering SIGTERM does not release a running
worker's slot before its completion/cleanup gate. A topic worker that exits
after a pre-launch rejection releases its reservation without manufacturing
successful completion evidence.
A proven pre-start dispatch failure releases the time charge; an unknown start
retains it. Time reservations cannot collectively exceed `max_seconds`, even
when turns fan out. These are execution-time limits, not token or price claims.

The managed-route scheduler is the single dispatch owner for its Flow plans;
ordinary Flow dispatch skips them. The shared Flow store still owns atomic
step claims, dependency gates and message linkage. Queued human work defers new
route claims; it does not revoke an already admitted turn. Parent Flow steps wait for
nested routes or continuations before releasing their own dependencies. Each
managed member also needs its recorded worker completion/sync gate; a transcript
`done` event alone cannot release dependencies, including after restart. Worker
execution rechecks captured scope and current authority, including on retries.

Restart blocks parents without a saved success/sync gate. Unstarted plans with
proven gates remain eligible; claimed or potentially started turns are never
automatically replayed. Reservations and prior outcomes survive recovery.
Existing Flow transcript/realtime events show member turns; the route API/CLI
shows route status. Cancellation is persisted before Flow cancellation, queued
turn draining and process stopping. Nested routes and continuation children
are frozen too. History deletion triggers the same freeze. Periodic maintenance
retries interrupted stops, isolating individual stop failures so other workers
can still be stopped. Shutdown cancels and joins the dispatch tasks it owns.

## Optional native MCP transport

`agentsquid mcp` is a stdio adapter for the same versioned route/continuation
registry and issuing-instance HTTP calls. It requires the existing optional
`mcp` dependency extra. CLI and HTTP do not import that SDK. Launch it explicitly
inside a supported turn environment; no persistent user harness configuration
is written. Tool arguments do not contain credentials. Watch registration and
human grant controls retain their separate existing transports.

Tests in `tests/test_routes.py` use disposable databases, deterministic worker
streams, concurrency races and fresh recovery processes. They do not invoke
paid agents or publish to real remotes.
