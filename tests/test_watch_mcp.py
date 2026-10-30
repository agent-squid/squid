"""Real SDK transport tests; no external services, credentials, or production DB."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import socket
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from agent import stats_db, watches
from agent.watch_probes import Probe, ProbeAdapter

SERVER = Path(__file__).parent / 'fixtures' / 'watch_mcp_server.py'


def write_state(path, **task):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'tasks': {'TASK-1': task}}))
    temporary.replace(path)


@asynccontextmanager
async def http_endpoint(path, *, token=None, port=0):
    # Keep the ephemeral port bound until Uvicorn owns it; no port race.
    spec = importlib.util.spec_from_file_location('watch_mcp_fixture', SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.make_server(path).streamable_http_app()
    if token:
        from starlette.responses import Response

        class BearerMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope['type'] == 'http' and dict(scope['headers']).get(b'authorization') != f'Bearer {token}'.encode():
                    await Response(status_code=401)(scope, receive, send)
                    return
                await self.app(scope, receive, send)
        app = BearerMiddleware(app)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error',
                                              timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async def ready():
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError('HTTP fixture stopped before startup')
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 5)
            yield f'http://127.0.0.1:{port}/mcp'
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def http_transport(path):
    async with http_endpoint(path) as url:
        async with streamable_http_client(url) as streams:
            yield streams[:2]


@asynccontextmanager
async def connect(path, transport):
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(SERVER), '--state', str(path)])
    client = stdio_client(params) if transport == 'stdio' else http_transport(path)
    async with client as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def adapter(session, timeout=2):
    async def call(server, tool, arguments):
        assert server == 'fixture'
        result = await session.call_tool(tool, arguments)
        if result.isError:
            raise RuntimeError('MCP probe failed')
        if not isinstance(result.structuredContent, dict):
            raise ValueError('missing structured MCP result')
        return result.structuredContent
    return ProbeAdapter(Probe(server='fixture', tool='task_snapshot', timeout=timeout),
                        call_tool=call)


@pytest.mark.parametrize('transport', ['stdio', 'streamable-http'])
def test_discovery_baseline_comment_edit_and_close(tmp_path, transport):
    path = tmp_path / 'state.json'
    write_state(path, revision='task:1', summary='original')

    async def run():
        async with connect(path, transport) as session:
            tools = (await session.list_tools()).tools
            assert [t.name for t in tools] == ['task_snapshot']
            assert tools[0].inputSchema['required'] == ['target']
            probe = adapter(session)
            watch = {'target': 'TASK-1', 'cursor': None}
            baseline = await probe.poll(watch)
            assert baseline.cursor == 'task:1' and not baseline.events
            watch['cursor'] = baseline.cursor
            write_state(path, revision='comment:1:edit:0', summary='please fix')
            comment = await probe.poll(watch)
            assert comment.events[0][1]['summary'] == 'please fix'
            watch['cursor'] = comment.cursor
            assert not (await probe.poll(watch)).events
            write_state(path, revision='comment:1:edit:1', summary='updated request')
            edited = await probe.poll(watch)
            assert edited.events[0][0] != comment.events[0][0]
            write_state(path, revision='task:closed', closed=True, changed=False)
            assert (await probe.poll(watch)).closed
    asyncio.run(run())


@pytest.mark.parametrize('transport', ['stdio', 'streamable-http'])
def test_errors_timeout_and_recovery(tmp_path, transport):
    path = tmp_path / 'state.json'
    write_state(path, revision='task:1', error=True)

    async def run():
        async with connect(path, transport) as session:
            probe = adapter(session, timeout=.2)
            watch = {'target': 'TASK-1', 'cursor': None}
            with pytest.raises(RuntimeError, match='MCP probe failed'):
                await probe.poll(watch)
            with pytest.raises(RuntimeError):
                await probe.poll(watch | {'target': 'missing'})
            write_state(path, revision='task:1', delay=1)
            with pytest.raises(asyncio.TimeoutError):
                await probe.poll(watch)
            write_state(path, revision='task:2')
            assert (await probe.poll(watch)).cursor == 'task:2'
    asyncio.run(run())


@pytest.mark.parametrize('transport', ['stdio', 'streamable-http'])
def test_reconnect_preserves_durable_cursor_and_pending_events(tmp_path, monkeypatch, transport):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    path = tmp_path / 'state.json'
    write_state(path, revision='comment:1:edit:0', changed=True, summary='handle comment')
    row = watches.create_watch(topic='test', adapter='fixture', target='TASK-1',
                               request_key='test', poll_interval=30, now=100)
    watches.transition_watch(row['id'], topic='test', version=0, action='activate', now=100)

    async def tick(now):
        async with connect(path, transport) as session:
            loop = watches.WatchLoop({'fixture': adapter(session)}, clock=lambda: now)
            assert await loop.tick() == 1

    asyncio.run(tick(100))
    stats_db.init_db()
    asyncio.run(tick(131))  # New server/client; same source event.
    conn = stats_db._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0] == 1
        assert conn.execute('SELECT disposition FROM watch_events').fetchone()[0] == 'pending'
        assert conn.execute('SELECT cursor FROM watches').fetchone()[0] == 'comment:1:edit:0'
    finally:
        conn.close()


@pytest.mark.parametrize('transport', ['stdio', 'streamable-http'])
def test_registered_client_roundtrip_errors_timeout_and_recovery(tmp_path, transport):
    from agent.watch_mcp import load_probe_registry, MCPProbeError
    path = tmp_path / 'state.json'
    write_state(path, revision='v1')

    async def exercise(server):
        probe = load_probe_registry({'servers': {'fixture': server}, 'probes': {
            'task': {'server': 'fixture', 'tool': 'task_snapshot', 'timeout': 2}
        }})['task']
        watch = {'target': 'TASK-1', 'cursor': None}
        assert (await probe.poll(watch)).cursor == 'v1'
        write_state(path, revision='v2', error=True)
        with pytest.raises(MCPProbeError, match='^MCP probe failed$'):
            await probe.poll(watch)
        write_state(path, revision='v2', delay=5)
        with pytest.raises(asyncio.TimeoutError):
            await probe.poll(watch)
        write_state(path, revision='v3')
        assert (await probe.poll(watch)).cursor == 'v3'
        with pytest.raises(ValueError, match='unregistered'):
            await probe.call_tool('fixture', 'unregistered', {})

    async def run():
        if transport == 'stdio':
            await exercise({'transport': 'stdio', 'argv': [sys.executable, str(SERVER), '--state', str(path)]})
        else:
            async with http_endpoint(path) as url:
                await exercise({'transport': transport, 'url': url})
    asyncio.run(run())


def test_http_credentials_and_broken_connection_recovery(tmp_path, monkeypatch):
    from agent.watch_mcp import load_probe_registry, MCPProbeError, MCPCredentialError
    path = tmp_path / 'state.json'
    write_state(path, revision='v1')
    watch = {'target': 'TASK-1', 'cursor': None}

    async def run():
        async with http_endpoint(path, token='fixture-secret') as url:
            probe = load_probe_registry({'servers': {'fixture': {
                'transport': 'streamable-http', 'url': url, 'bearer_env': 'WATCH_TEST_TOKEN'
            }}, 'probes': {'task': {'server': 'fixture', 'tool': 'task_snapshot', 'timeout': 2}}})['task']
            for value in (None, 'wrong-secret'):
                if value is None:
                    monkeypatch.delenv('WATCH_TEST_TOKEN', raising=False)
                else:
                    monkeypatch.setenv('WATCH_TEST_TOKEN', value)
                with pytest.raises(MCPProbeError) as error:
                    await probe.poll(watch)
                if value is None:
                    assert type(error.value) is MCPCredentialError
                    assert str(error.value) == 'MCP credential unavailable'
                else:
                    assert type(error.value) is MCPProbeError
                    assert str(error.value) == 'MCP probe failed'
                    assert error.value.__suppress_context__
            monkeypatch.setenv('WATCH_TEST_TOKEN', 'fixture-secret')
            assert (await probe.poll(watch)).cursor == 'v1'
        # The endpoint is now gone. A failed poll must not poison later calls.
        with pytest.raises(MCPProbeError):
            await probe.poll(watch)
        from urllib.parse import urlsplit
        async with http_endpoint(path, token='fixture-secret', port=urlsplit(url).port):
            write_state(path, revision='v2')
            assert (await probe.poll(watch)).cursor == 'v2'
    asyncio.run(run())


def test_malformed_stdio_output_does_not_leak_through_sdk_logs(caplog):
    import logging
    from agent.watch_mcp import MCPProbeError, load_probe_registry

    secret = 'MCP_REVIEW_SECRET_SENTINEL'
    script = (
        'import sys; sys.stdin.readline(); '
        f'print({secret!r}, flush=True)'
    )
    probe = load_probe_registry({'servers': {'fixture': {
        'transport': 'stdio', 'argv': [sys.executable, '-c', script]
    }}, 'probes': {'task': {'server': 'fixture', 'tool': 'task_snapshot', 'timeout': 2}}})['task']
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MCPProbeError, match='^MCP probe failed$'):
            asyncio.run(probe.poll({'target': 'TASK-1', 'cursor': None}))
    assert secret not in caplog.text
    records = [r for r in caplog.records if r.name == 'mcp.client.stdio']
    assert records  # Exercise the SDK's parse-error logging path.
    assert all(r.exc_info is None and r.exc_text is None for r in records)


@pytest.mark.parametrize('value', [None, '', 'bad token', 'bad\ntoken', 'nonascii-é', 'bad\x7ftoken'])
def test_missing_or_invalid_credentials_keep_distinct_error_type(monkeypatch, value):
    from agent.watch_mcp import load_probe_registry, MCPCredentialError
    if value is None:
        monkeypatch.delenv('WATCH_TEST_TOKEN', raising=False)
    else:
        monkeypatch.setenv('WATCH_TEST_TOKEN', value)
    probe = load_probe_registry({'servers': {'fixture': {
        'transport': 'streamable-http', 'url': 'http://127.0.0.1:1/mcp',
        'bearer_env': 'WATCH_TEST_TOKEN',
    }}, 'probes': {'task': {'server': 'fixture', 'tool': 'task_snapshot'}}})['task']
    with pytest.raises(MCPCredentialError, match='^MCP credential unavailable$'):
        asyncio.run(probe.poll({'target': 'TASK-1', 'cursor': None}))


def test_http_timeout_follows_probe_registration(monkeypatch):
    import httpx
    from agent.watch_mcp import load_probe_registry

    timeouts = []

    def response(request):
        timeouts.append(request.extensions['timeout'])
        return httpx.Response(503)

    original = httpx.AsyncClient

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(response))

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    from agent.watch_mcp import MCPProbeError
    probe = load_probe_registry({'servers': {'fixture': {
        'transport': 'streamable-http', 'url': 'https://example.test/mcp',
    }}, 'probes': {'task': {'server': 'fixture', 'tool': 'task_snapshot', 'timeout': 45}}})['task']
    with pytest.raises(MCPProbeError):
        asyncio.run(probe.poll({'target': 'TASK-1', 'cursor': None}))
    assert timeouts
    assert all(all(value == 45 for value in timeout.values()) for timeout in timeouts)
