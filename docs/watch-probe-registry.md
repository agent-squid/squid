# Trusted watch probe registry

For agent-built plans using ordinary tool arguments/results without per-tool
probe wrappers, see [observation plans](watch-observation-plans.md). This page
describes the existing fixed-probe compatibility path.

## Executable wrapper setup through the CLI

`agentsquid tools --help` and `agentsquid watch --help` advertise the local
setup commands. They require no instance URL or API credential:

```sh
agentsquid watch probe contract
agentsquid watch probe test --help
agentsquid watch probe register --help
```

`contract` prints the input/output contract, revision/error semantics,
registration schema, credential requirements and activation workflow as JSON.
The test command executes the supplied program once; use a trusted read-only
wrapper. Registration validates configuration without executing the wrapper.

Save a registration object as `probe.json`:

```json
{"argv":["/absolute/path/to/read-snapshot"],"timeout":10,"output_limit":65536}
```

The program reads one JSON object from stdin, for example
`{"target":"project/task/123","cursor":null}`, and writes exactly one JSON
object to stdout before exiting zero:

```json
{"revision":"task-123:edit-7","summary":"Task updated","url":"https://example.com/tasks/123","closed":false}
```

`revision` is required and identifies an immutable source version, including
edits or attempts. Do not use recurring status labels or content hashes. On the
first poll, omission of `changed` establishes a quiet baseline. Later different
revisions emit events; `changed:true` permits an initial event, and
`changed:false` advances the baseline quietly. An unchanged revision never
emits an event; already ingested revisions are deduplicated. `closed:true`
reports closure. Only `revision`, `changed`, `summary`, `url` and `closed` are
accepted. The two flags must be booleans; text fields must be strings, with a
nonempty revision of at most 4096 characters.

Nonzero exit, timeout, invalid JSON/schema or oversized stdout fails the poll
without advancing the stored cursor. A later scheduled poll may retry. Both raw
stdout and normalized JSON (sorted keys, compact separators and ASCII escapes) are bounded by `output_limit` (maximum/default 65536 bytes). Unicode
text can therefore exceed the normalized limit despite fitting on stdout.
Persisted event JSON is independently limited to 65536 bytes. Timeout defaults
to 20 seconds; the watch timeout must exceed probe timeout plus one second for
cleanup.
Stderr is discarded. The executable inherits the host environment and working
directory. Provision credentials on that host; use absolute paths and keep
secrets out of argv, targets and results. No shell is invoked.

Test both the initial baseline and a revision transition:

```sh
agentsquid watch probe test --input probe.json --target project/task/123
agentsquid watch probe test --input probe.json --target project/task/123 --cursor task-123:edit-6
```

Successful tests print the sample request and normalized observation, including
the resulting cursor and event payloads. Failures return nonzero with sanitized
stderr. No watch or grant is created and no cursor is persisted.

An operator can register the wrapper in an explicitly selected, existing host
configuration file:

```sh
agentsquid watch probe register --input probe.json --config /absolute/path/to/squid.yaml --name task_snapshot
```

This saves `watches.registry.probes.task_snapshot`, validates the complete watch
configuration and refuses to replace an existing registration. It preserves
file permissions and unrelated configuration values, but YAML formatting and
comments are rewritten. It does not enable watches or change policy. The
wrapper must remain at its durable absolute path on the host. Restart the host
to load registration changes; `watches.enabled` and the effective topic policy
must allow the adapter.

Then use the existing authenticated API workflow:

1. `agentsquid watch create --input proposal.json`, where the proposal contains
   `adapter: "task_snapshot"`, `target` and a unique `request_key`.
2. Obtain a separate exact-watch monitoring grant using `watch grant` with human
   control authentication, then `watch activate` with the current watch version.
3. Grant local work separately if desired. Task publication additionally needs
   an explicitly activated publication scope and a separate `task_review`.

See [watch authorization](watch-authorization.md) for instance credentials,
policy and complete grant/activation payloads. Registration is operator
configuration, never watch authority.

`agent.watch_mcp.load_probe_registry(mapping)` builds named `ProbeAdapter`s.
Loading does not connect, spawn processes, start polling, or activate watches.
The host loads this mapping from `watches.registry` in `squid.yaml` during
server startup and validates it in the config editor. `watches.enabled` defaults
to false; disabled registries are still validated but expose no adapters.
Enabled adapters are held in `app.state.watch_configuration`; loading starts
no polling by itself. Lifespan starts the authorized observation worker when
globally enabled. Creating a watch through the HTTP API or `watch create` saves
a proposal; activation needs an authenticated exact-watch grant. This differs
from `watch probe register`, which saves host adapter configuration. Local-work
dispatch is independently opt-in and needs a separate execution grant;
observation alone launches no model.
Internal policy, grants, and scoped operations are described in
[watch authorization](watch-authorization.md), including authenticated human
grant controls and scoped one-shot agent credential delivery.
Unknown watch settings and non-boolean enablement fail validation.
Malformed watch configuration prevents the server from starting, even when
`watches.enabled` is false. Omit `watches` to use defaults; an empty `watches:`
key is YAML null and also loads as disabled defaults. Nested registry and policy
values still require valid mappings. If a manual edit prevents startup, correct
`squid.yaml` on the host before restarting; the web editor is unavailable until
startup succeeds. Config-editor saves reject invalid settings before writing.

Trusted host configuration includes edits through `PUT /config/yaml`. Anyone
allowed to use that editor can register executable commands and MCP endpoints;
its same-origin check is not a separate probe-registration permission. Treat
configuration-editor access as operator access. Saved watch settings take
effect after restart.

Read-only tool declarations require operator trust; the registry cannot prove
that a remote tool has no side effects.

The following mapping belongs under `watches.registry` (alongside
`watches.enabled: false`):

```yaml
servers:
  corporate:
    transport: streamable-http
    url: https://mcp.example.com/mcp
    bearer_env: CORPORATE_MCP_TOKEN
  local:
    transport: stdio
    argv: [/absolute/path/to/mcp-server, --read-only]
probes:
  task:
    server: corporate
    tool: task_snapshot
    timeout: 20
    output_limit: 65536
  executable:
    argv: [/absolute/path/to/read-snapshot]
    timeout: 10
```

Unknown fields, missing server references, mixed command/URL transports, and
invalid timeout/output limits fail validation. Configured output limits cannot
exceed the 65,536-byte event storage cap. Commands are argv lists, never
shell strings. Watch payloads supply only target/cursor arguments. MCP calls
are restricted to the server/tool pair registered for that adapter. The existing
probe contract requires a structured revision snapshot; providers needing a
schema conversion still require a trusted wrapper.

Install `agentsquid[mcp]` for MCP probes (official Python SDK `>=1.30,<2`).
Executable probes do not import the SDK. The SDK transport APIs follow the
[official v1 documentation](https://py.sdk.modelcontextprotocol.io/v1/).

HTTP requires HTTPS except on loopback, rejects URL credentials/query/fragment,
and disables redirects, environment proxies, and ambient HTTP credentials.
Bearer tokens are resolved from the named environment variable on every poll;
missing/invalid values fail closed with a distinct `MCPCredentialError` type.
HTTP rejection and provider failures retain the generic sanitized `MCPProbeError`. OAuth discovery/refresh is not implemented.
Stdio uses the SDK's limited default process environment and the provider's own
credential store. Provider-specific environment mappings are not yet supported.
Provider stderr is discarded. During each probe call and transport cleanup,
MCP/HTTP SDK log messages, arguments, exception tracebacks, and stack traces are
replaced with a fixed diagnostic before handlers receive them. A context-scoped
log-record factory wrapper preserves the host factory and leaves concurrent
non-probe diagnostics unchanged. Client failures use a fixed error message with
suppressed exception chaining; do not log raw SDK responses.

Each poll opens and closes a fresh transport/session, initializes it, and calls
one registered tool. A failed observation is not retried within that poll. The
next scheduled poll reconnects, allowing recovery from stopped/restarted servers
and credential rotation. This trades connection overhead for simple lifecycle
ownership; it does not resume an interrupted stream or retain SDK sessions.
HTTP connect/read/write/pool timeouts follow the registered probe timeout.
The adapter timeout covers connection, initialization, call, and normal cleanup;
SDK cancellation cleanup can extend elapsed time. The watch loop still reserves
its existing cleanup allowance.

`isError`, absent/non-object structured content, and oversized structured results
fail the observation. `ProbeAdapter` validates the revision contract before
cursor/event ingestion. Output limits bound accepted structured JSON, not SDK
network buffering or text content. Untrusted wire-size limiting remains future
work. Configuration is never authority to activate a watch or dispatch an agent.

Tests cover real stdio/HTTP calls, provider errors, timeout/reconnection,
missing/rejected/valid bearer credentials, endpoint outage/restart, registration
validation, and malformed/oversized structured results. MCP transport tests skip
cleanly when the optional SDK is absent; executable and registry tests still run. These are isolated
fixtures. Production observation and local-work execution are independently
opt-in and require the corresponding durable grants. Tests enable them only in
disposable hosts; no production configuration is changed.
