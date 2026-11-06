"""GitHub test reader bounds and real stdio protocol; no GitHub writes or credentials."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'tools' / 'gh_watch_mcp.py'
spec = importlib.util.spec_from_file_location('gh_watch_mcp', SERVER)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def comment(number=1, updated='2026-11-05T00:00:00Z'):
    return dict(id=number, updated_at=updated, body='hello', html_url='https://github.com/o/r/issues/1#issuecomment-1')


def fake_gh(monkeypatch, payload, *, returncode=0):
    calls = []
    monkeypatch.setattr(reader.shutil, 'which', lambda name: '/test/gh')

    def run(argv, **kwargs):
        calls.append(argv)
        kwargs['stdout'].write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['timeout'] == 12
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(reader.subprocess, 'run', run)
    return calls


def test_complete_snapshot_and_command(monkeypatch):
    calls = fake_gh(monkeypatch, [comment()])
    assert reader.read_issue_comments('o', 'r', 1) == {'comments': [comment()], 'complete': True}
    assert calls == [['/test/gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                      'repos/o/r/issues/1/comments?per_page=51']]


@pytest.mark.parametrize('payload', [[], [comment(i) for i in range(50)]])
def test_complete_boundary(monkeypatch, payload):
    fake_gh(monkeypatch, payload)
    assert reader.read_issue_comments('o', 'r', 1)['complete'] is True


@pytest.mark.parametrize('payload', [[comment(i) for i in range(51)], b'x' * (reader.RAW_LIMIT + 1),
                                    b'not json', {}, [{'id': 1}], [comment() | {'updated_at': None}]])
def test_rejects_incomplete_or_invalid_output(monkeypatch, payload):
    fake_gh(monkeypatch, payload)
    with pytest.raises(ValueError):
        reader.read_issue_comments('o', 'r', 1)


@pytest.mark.parametrize('coordinates', [('o/x', 'r', 1), ('o', '../r', 1), ('o', 'r', 0), ('o', 'r', True)])
def test_rejects_coordinates_before_execution(monkeypatch, coordinates):
    calls = fake_gh(monkeypatch, [])
    with pytest.raises(ValueError):
        reader.read_issue_comments(*coordinates)
    assert calls == []


def test_provider_errors_are_sanitized(monkeypatch):
    fake_gh(monkeypatch, b'secret provider detail', returncode=1)
    with pytest.raises(ValueError, match='check access'):
        reader.read_issue_comments('o', 'r', 1)


def test_timeout(monkeypatch):
    fake_gh(monkeypatch, [])
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('secret', 12)
    monkeypatch.setattr(reader.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='timed out'):
        reader.read_issue_comments('o', 'r', 1)


@pytest.mark.asyncio
async def test_real_stdio_tool(tmp_path):
    pytest.importorskip('mcp')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os

    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({[comment()]!r}))\n')
    gh.chmod(0o755)
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                  env={'PATH': str(tmp_path) + os.pathsep + os.environ.get('PATH', '')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            catalog = await session.list_tools()
            assert [tool.name for tool in catalog.tools] == ['read_issue_comments']
            result = await session.call_tool('read_issue_comments', dict(owner='o', repo='r', issue_number=1))
            assert not result.isError
            assert result.structuredContent == {'comments': [comment()], 'complete': True}


def test_metadata_does_not_consume_watch_output_budget(monkeypatch):
    payload = comment() | {'user': {'metadata': 'x' * 100_000}}
    fake_gh(monkeypatch, [payload])
    assert reader.read_issue_comments('o', 'r', 1) == {'comments': [comment()], 'complete': True}


def test_normalized_snapshot_still_has_output_limit(monkeypatch):
    fake_gh(monkeypatch, [comment() | {'body': 'x' * reader.LIMIT}])
    with pytest.raises(ValueError, match='watch output limit'):
        reader.read_issue_comments('o', 'r', 1)


@pytest.mark.parametrize('glyph', ['漢', '😀'])
def test_unicode_snapshot_at_transport_size_boundary(monkeypatch, glyph):
    record = comment() | {'body': ''}
    snapshot = {'comments': [record], 'complete': True}
    overhead = len(json.dumps(snapshot, allow_nan=False).encode())
    glyph_size = len(json.dumps(glyph).encode()) - 2
    count, remainder = divmod(reader.LIMIT - overhead, glyph_size)
    record['body'] = glyph * count + 'x' * remainder
    assert len(json.dumps(snapshot, allow_nan=False).encode()) == reader.LIMIT
    fake_gh(monkeypatch, [record])
    assert reader.read_issue_comments('o', 'r', 1) == snapshot

    record['body'] += 'x'
    assert len(json.dumps(snapshot, allow_nan=False).encode()) == reader.LIMIT + 1
    # UTF-8 still fits, reproducing the old disagreement with transport accounting.
    assert len(json.dumps(snapshot, ensure_ascii=False).encode()) < reader.LIMIT
    fake_gh(monkeypatch, [record])
    with pytest.raises(ValueError, match='watch output limit'):
        reader.read_issue_comments('o', 'r', 1)


@pytest.mark.asyncio
async def test_reader_through_durable_watch_polling(tmp_path, monkeypatch):
    pytest.importorskip('mcp')
    import os
    from agent import stats_db, watches
    from agent.watch_config import load_watch_configuration
    from agent.watch_policy import OPERATIONS
    from agent.watch_service import WatchControl, WatchService

    state_file = tmp_path / 'comments.json'
    state_file.write_text(json.dumps([comment()]))
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(state_file)!r}).read_text())\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    configuration = load_watch_configuration({'watches': {'enabled': True, 'connections': {
        'github-test': {'connection': {'transport': 'stdio', 'argv': [sys.executable, str(SERVER)]},
                        'read_tools': ['read_issue_comments']}}}})
    service = WatchService(configuration, clock=lambda: 100)
    control = WatchControl(service)
    token = control.mint(turn='setup', topic='test', operations=OPERATIONS, expires_at=900)
    plan = dict(version=1, initial='quiet', steps=[dict(
        server='github-test', tool='read_issue_comments',
        arguments=dict(owner='o', repo='r', issue_number=1), items='/comments',
        identity=['/id', '/updated_at'], complete={'path': '/complete', 'equals': True})])
    proposal = await service.create_plan(token, target='https://github.com/o/r/issues/1',
                                         plan=plan, request_key='gh-integration')
    grant = control.record_grant(topic='test', watch_id=proposal['id'], user_action='test',
                                 expires_at=proposal['expires_at'])
    active_token = control.mint(turn='activate', topic='test', operations=OPERATIONS,
                                expires_at=900, grants=[grant])
    service.transition(active_token, proposal['id'], version=0, action='activate', grant_id=grant)
    control.revoke_turn('setup')
    # Reconstruct the service: the saved watch is independent of its setup session.
    service = WatchService(configuration, clock=lambda: 100)
    adapter = configuration.adapters['plan']

    async def poll(now):
        row = service.claim_due(now=now, lease_seconds=30, limit=1)[0]
        observed = await adapter.poll(row)
        service.finish_poll(row, now=now, observation=observed)
        return observed

    assert not (await poll(100)).events
    state_file.write_text(json.dumps([comment(), comment(2)]))
    assert len((await poll(130)).events) == 1
    await poll(160)
    state_file.write_text(json.dumps([comment(), comment(2, '2026-11-05T00:01:00Z')]))
    assert len((await poll(190)).events) == 1
    await poll(220)
    with watches._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0] == 2
