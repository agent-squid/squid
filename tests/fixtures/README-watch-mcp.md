# Watch MCP fixture

This test-only MCP server exposes one read tool, `task_snapshot(target, cursor)`.
It runs over stdio or Streamable HTTP with the official Python MCP SDK v1,
pinned below v2 in the `dev` dependency group. HTTP binds only to loopback.
Neither transport uses credentials.
The fixture and SDK client exercise real protocol negotiation, discovery,
structured results, errors, cancellation, and reconnection without an LLM.

Run the tests:

```sh
uv run --with pytest python -m pytest tests/test_watch_mcp.py -q
```

Launch from an MCP stdio client using this command and an absolute state path:

```sh
uv run python tests/fixtures/watch_mcp_server.py --state /absolute/path/state.json
```

Launch the HTTP endpoint at `http://127.0.0.1:8000/mcp`:

```sh
uv run python tests/fixtures/watch_mcp_server.py --state /absolute/path/state.json --transport streamable-http --port 8000
```

The same tests run over both transports. HTTP tests host the fixture in-process
with Uvicorn on a reserved ephemeral loopback socket, wait for startup, and
stop the server on exit. Stdio tests launch a subprocess. Each reconnect opens
a fresh server and client; this tests durable cursor/event recovery rather than
transparent reconnection of a broken live session.

Example state:

```json
{"tasks":{"TASK-1":{"revision":"comment:1:edit:0","summary":"Please fix the test","changed":true}}}
```

The test owner replaces the state file atomically between calls. Tool calls
cannot mutate it. Each task accepts probe fields `revision`, `changed`,
`summary`, `url`, and `closed`, plus fixture-only `delay` (0–30 seconds) and
`error` (truthy to return an MCP tool error). Unknown targets fail. Use unique
source revision IDs for new comments/edits; repeated revisions are replays.
Omit `changed` to establish a quiet initial baseline. Logs use stderr; stdout
is reserved for the MCP protocol.

`tests/test_watch_mcp.py` also exercises the concrete client in
`agent/watch_mcp.py`, including bearer credential rejection and endpoint
outage/restart. The [internal registry documentation](../../docs/watch-probe-registry.md)
describes configuration and limitations. This is not production startup wiring,
authorization, agent dispatch, or full Squid server restart coverage. The reconnect test opens a new fixture server/client and reinitializes
the same temporary watch database, verifying cursor/event persistence only.
