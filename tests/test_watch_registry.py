import asyncio
import sys

import pytest

from agent.watch_mcp import load_probe_registry


@pytest.mark.parametrize('config', [
    None, {'unknown': {}}, {'servers': []}, {'probes': []},
    {'probes': {'bad name': {}}},
    {'servers': {'s': {'transport': 'sse'}}},
    {'servers': {'s': {'transport': 'stdio', 'argv': 'echo'}}},
    {'servers': {'s': {'transport': 'stdio', 'argv': ['echo'], 'url': 'https://example.com'}}},
    *[{'servers': {'s': {'transport': 'streamable-http', 'url': url}}}
      for url in ['http://example.com/mcp', 'https://user:secret@example.com',
                  'https://example.com/?token=secret', 'https://example.com/#secret',
                  'file:///tmp/mcp', None, 'https://example.com:bad', 'https://exa mple.com']],
    {'servers': {'s': {'transport': 'streamable-http', 'url': 'https://example.com', 'bearer_env': 'bad name'}}},
    {'probes': {'p': {'server': 'missing', 'tool': 't'}}},
    {'probes': {'p': {'argv': ['echo'], 'timeout': 0}}},
    {'probes': {'p': {'argv': ['echo'], 'output_limit': True}}},
    {'probes': {'p': {'argv': ['echo'], 'output_limit': 65537}}},
    {'servers': {'s': {'transport': 'stdio', 'argv': ['echo']}},
     'probes': {'p': {'server': 's', 'tool': 't', 'output_limit': 1048576}}},
    {'probes': {'p': {'argv': ['echo'], 'extra': True}}},
    {'probes': {'p': {'argv': ['echo'], 'server': 's', 'tool': 't'}}},
])
def test_reject_invalid_trusted_configuration(config):
    with pytest.raises(ValueError):
        load_probe_registry(config)


def test_local_registry_is_lazy_and_copies_configuration():
    config = {'probes': {'p': {'argv': [sys.executable, '-c', 'print(\'{"revision":"v1"}\')']}}}
    registry = load_probe_registry(config)
    config['probes']['p']['argv'].clear()
    assert asyncio.run(registry['p'].poll({'target': 'literal', 'cursor': None})).cursor == 'v1'
    assert load_probe_registry({}) == {}


@pytest.mark.parametrize('error,data,limit', [
    (True, {'revision': 'v1'}, 100),
    (False, None, 100),
    (False, ['unexpected'], 100),
    (False, {'revision': 'too large'}, 4),
    (False, {'revision': float('nan')}, 100),
])
def test_mcp_result_bridge_rejects_errors_and_invalid_results(monkeypatch, error, data, limit):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from agent import watch_mcp

    class Session:
        async def call_tool(self, tool, arguments):
            return SimpleNamespace(isError=error, structuredContent=data)

    @asynccontextmanager
    async def connect(config, timeout=20):
        yield Session()

    monkeypatch.setattr(watch_mcp, '_connect', connect)
    with pytest.raises(watch_mcp.MCPProbeError, match='^MCP probe failed$'):
        asyncio.run(watch_mcp._call({}, 't', {}, limit))


@pytest.mark.parametrize('cancel', [False, True])
def test_transport_log_redaction_scoped_to_probe_tasks(monkeypatch, caplog, cancel):
    from contextlib import asynccontextmanager
    import logging
    from agent import watch_mcp

    entered = asyncio.Event()
    release = asyncio.Event()

    def log_provider_data():
        for name in ('mcp.client.streamable_http', 'httpx', 'httpcore.connection'):
            try:
                raise ValueError('private exception')
            except ValueError:
                logging.getLogger(name).exception('private payload %s', 'private argument', stack_info=True)

    @asynccontextmanager
    async def connect(config, timeout=20):
        try:
            # The SDK creates its own tasks; they must inherit redaction.
            await asyncio.create_task(asyncio.to_thread(log_provider_data))
            entered.set()
            await release.wait()
            raise RuntimeError('provider failure')
            yield
        finally:
            logging.getLogger('mcp.client.session').warning('private cleanup')

    monkeypatch.setattr(watch_mcp, '_connect', connect)

    async def run():
        task = asyncio.create_task(watch_mcp._call({}, 't', {}, 100))
        await asyncio.wait_for(entered.wait(), 2)
        logging.getLogger('mcp.client.streamable_http').warning('unrelated concurrent diagnostic')
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(watch_mcp.MCPProbeError):
                await task
        logging.getLogger('httpx').warning('after probe diagnostic')

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert 'private' not in caplog.text
    assert 'unrelated concurrent diagnostic' in caplog.text
    assert 'after probe diagnostic' in caplog.text
    redacted = [r for r in caplog.records if r.msg == 'MCP probe transport diagnostic redacted']
    assert len(redacted) == 4
    assert all(not r.args and r.exc_info is None and r.stack_info is None for r in redacted)


def test_output_limit_accepts_storage_cap():
    probe = load_probe_registry({'probes': {'p': {'argv': ['echo'], 'output_limit': 65536}}})['p']
    assert probe.probe.output_limit == 65536
