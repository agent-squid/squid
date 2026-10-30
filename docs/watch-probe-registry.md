# Trusted watch probe registry (internal)

`agent.watch_mcp.load_probe_registry(mapping)` builds named `ProbeAdapter`s.
Loading does not connect, spawn processes, start polling, or activate watches.
The host must supply this mapping from trusted configuration. It is not yet
loaded from `squid.yaml`, exposed through an API/CLI, or wired into startup.
Read-only tool declarations require operator trust; the registry cannot prove
that a remote tool has no side effects.

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
fixtures; production polling and authorized dispatch remain disabled.
