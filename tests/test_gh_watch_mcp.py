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
@pytest.mark.parametrize('new_issues', [False, True])
async def test_real_stdio_tool(tmp_path, new_issues):
    pytest.importorskip('mcp')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os

    record = issue() if new_issues else comment()
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({[record]!r}))\n')
    gh.chmod(0o755)
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                  env={'PATH': str(tmp_path) + os.pathsep + os.environ.get('PATH', '')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            catalog = await session.list_tools()
            assert [tool.name for tool in catalog.tools] == ['read_issue_comments', 'read_issues', 'read_bugs']
            result = await session.call_tool(
                'read_issues' if new_issues else 'read_issue_comments',
                dict(owner='o', repo='r') if new_issues else dict(owner='o', repo='r', issue_number=1))
            assert not result.isError
            assert result.structuredContent == {
                'issues' if new_issues else 'comments': [record], 'complete': True}


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
@pytest.mark.parametrize('new_issues', [False, True, 'bugs'])
async def test_reader_through_durable_watch_polling(tmp_path, monkeypatch, new_issues):
    pytest.importorskip('mcp')
    import os
    from agent import stats_db, watches
    from agent.watch_config import load_watch_configuration
    from agent.watch_policy import OPERATIONS
    from agent.watch_service import WatchControl, WatchService

    filtered = new_issues == 'bugs'
    record = bug if filtered else issue if new_issues else comment
    tool = 'read_bugs' if filtered else 'read_issues' if new_issues else 'read_issue_comments'
    baseline_count = 101 if filtered else 1
    baseline = [record(i + 1) for i in range(baseline_count)]
    new_id = baseline_count + 1
    state_file = tmp_path / 'comments.json'
    state_file.write_text(json.dumps(baseline))
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nprint(Path({str(state_file)!r}).read_text())\n')
    if filtered:
        gh.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n'
            'from urllib.parse import parse_qs,urlsplit\n'
            'if sys.argv[-1].startswith("users/"): print(json.dumps({"id":7,"login":"alice"}))\n'
            'else:\n'
            ' page=int(parse_qs(urlsplit(sys.argv[-1]).query)["page"][0])\n'
            f' records=json.loads(Path({str(state_file)!r}).read_text())\n'
            ' print(json.dumps(records[(page-1)*100:page*100]))\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    configuration = load_watch_configuration({'watches': {'enabled': True, 'connections': {
        'github-test': {'connection': {'transport': 'stdio', 'argv': [sys.executable, str(SERVER)]},
                        'read_tools': [tool]}}}})
    service = WatchService(configuration, clock=lambda: 100)
    control = WatchControl(service)
    token = control.mint(turn='setup', topic='test', operations=OPERATIONS, expires_at=900)
    plan = dict(version=1, initial='quiet', steps=[dict(
        server='github-test', tool=tool,
        arguments=dict(owner='o', repo='r', author='alice', author_id=7) if filtered else
            dict(owner='o', repo='r') if new_issues else dict(owner='o', repo='r', issue_number=1),
        items='/issues' if new_issues else '/comments',
        identity=['/id'] if new_issues else ['/id', '/updated_at'], complete={'path': '/complete', 'equals': True})])
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
        with watches._connect() as conn:
            due = conn.execute('SELECT next_poll FROM watches WHERE id=?', (proposal['id'],)).fetchone()[0]
        now = max(now, due)
        row = service.claim_due(now=now, lease_seconds=30, limit=1)[0]
        observed = await adapter.poll(row)
        service.finish_poll(row, now=now, observation=observed)
        return observed

    assert not (await poll(100)).events
    if filtered:
        with watches._connect() as conn:
            saved = conn.execute('SELECT cursor,status FROM watches WHERE id=?', (proposal['id'],)).fetchone()
        assert len(saved['cursor']) > 4096
        assert saved['status'] == 'active'
        service = WatchService(configuration, clock=lambda: 100)
    state_file.write_text(json.dumps(baseline + [record(new_id)]))
    assert len((await poll(130)).events) == 1
    await poll(160)
    edited = record(new_id) | {'updated_at': '2026-11-05T00:01:00Z'}
    state_file.write_text(json.dumps(baseline + [edited]))
    assert len((await poll(190)).events) == 1
    # Polls replay non-baseline identities; the durable inbox deduplicates them.
    await poll(220)
    with watches._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0] == (1 if new_issues else 2)


def issue(number=1, updated='2026-11-05T00:00:00Z'):
    return dict(id=number, number=number, title='Test issue', body=None,
                html_url=f'https://github.com/o/r/issues/{number}',
                created_at='2026-11-05T00:00:00Z', updated_at=updated, state='open')


def test_issue_snapshot_and_command(monkeypatch):
    calls = fake_gh(monkeypatch, [issue(), issue(2) | {'pull_request': {}}])
    assert reader.read_issues('o', 'r') == {'issues': [issue()], 'complete': True}
    assert calls[0][-1] == 'repos/o/r/issues?state=all&sort=created&direction=asc&per_page=51'


@pytest.mark.parametrize('payload', [[], [issue(i + 1) for i in range(50)],
                                    [issue() | {'state': 'closed'}]])
def test_issue_complete_boundary(monkeypatch, payload):
    fake_gh(monkeypatch, payload)
    assert reader.read_issues('o', 'r')['complete'] is True


@pytest.mark.parametrize('payload', [[issue(i + 1) for i in range(51)], {}, [None],
                                    [{'id': 1}], [issue() | {'number': True}],
                                    [issue() | {'body': 42}],
                                    [issue() | {'body': 'x' * reader.LIMIT}]])
def test_issue_invalid_or_oversized_snapshot(monkeypatch, payload):
    fake_gh(monkeypatch, payload)
    with pytest.raises(ValueError):
        reader.read_issues('o', 'r')


@pytest.mark.parametrize('owner,repo', [('o/x', 'r'), ('o', '../r'), ('o', '..')])
def test_issue_invalid_coordinates(monkeypatch, owner, repo):
    calls = fake_gh(monkeypatch, [])
    with pytest.raises(ValueError):
        reader.read_issues(owner, repo)
    assert not calls



def bug(number=1, **changes):
    return issue(number) | {'user': {'id': 7, 'login': 'alice'},
                            'labels': [{'name': 'bug'}]} | changes


def bug_pages(monkeypatch, pages, account=None):
    calls = []
    def read(endpoint):
        calls.append(endpoint)
        if endpoint.startswith('users/'):
            return account if account is not None else {'id': 7, 'login': 'alice'}
        return pages.pop(0)
    monkeypatch.setattr(reader, '_read', read)
    return calls


def test_bugs_verify_account_and_filter_locally(monkeypatch):
    calls = bug_pages(monkeypatch, [[bug(), bug(2, user={'id': 8, 'login': 'bob'}),
        bug(3, labels=[{'name': 'feature'}]), bug(4, pull_request={}),
        bug(5, user={'id': 8, 'login': 'alice'})]])
    snapshot = reader.read_bugs('o', 'r', 'ALICE', 7)
    assert snapshot == {'issues': [issue() | {'author': {'id': 7, 'login': 'alice'},
                                            'labels': ['bug']}], 'complete': True}
    assert calls[0] == 'users/ALICE'
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(calls[1]).query) == dict(state=['all'], creator=['alice'],
        labels=['bug'], sort=['created'], direction=['asc'], per_page=['100'], page=['1'])


def test_bugs_paginate_and_encode_label(monkeypatch):
    pages = [[bug(i + 1, labels=[{'name': 'type: bug'}]) for i in range(100)], []]
    calls = bug_pages(monkeypatch, pages)
    snapshot = reader.read_bugs('o', 'r', 'alice', 7, 'type: bug')
    assert len(snapshot['issues']) == 100 and snapshot['complete']
    assert 'labels=type%3A+bug' in calls[1]
    assert calls[2].endswith('page=2')


@pytest.mark.parametrize('account', [{}, {'id': True, 'login': 'alice'},
    {'id': 7, 'login': 'bob'}, {'id': 0, 'login': 'alice'}, {'id': 8, 'login': 'alice'}])
def test_bugs_reject_unverified_author(monkeypatch, account):
    calls = bug_pages(monkeypatch, [], account)
    with pytest.raises(ValueError, match='identity'):
        reader.read_bugs('o', 'r', 'alice', 7)
    assert len(calls) == 1


@pytest.mark.parametrize('changes', [{'author': '../alice'}, {'owner': 'o/x'},
    {'repo': '..'}, {'label': 'bug,feature'}, {'label': ''}, {'label': 'bug\n'},
    {'author': None}, {'author_id': True}, {'author_id': 0}, {'label': 'x' * 51}])
def test_bugs_invalid_filter_never_reads(monkeypatch, changes):
    calls = bug_pages(monkeypatch, [])
    with pytest.raises(ValueError):
        reader.read_bugs(**(dict(owner='o', repo='r', author='alice', author_id=7) | changes))
    assert not calls


@pytest.mark.parametrize('pages', [[{}], [[bug(user=None)]], [[bug(labels=['bug'])]],
    [[bug(number=True)]], [[bug(body=42)]], [[bug(body='x' * reader.LIMIT)]],
    [[bug(), bug()]]])
def test_bugs_reject_invalid_or_oversized_pages(monkeypatch, pages):
    bug_pages(monkeypatch, pages)
    with pytest.raises(ValueError):
        reader.read_bugs('o', 'r', 'alice', 7)


def test_bugs_fail_instead_of_truncating_or_replaying_pages(monkeypatch):
    # PRs consume the page bound too, while not consuming the output budget.
    pages = [[bug(page * 100 + i + 1, pull_request={}) for i in range(100)]
             for page in range(5)]
    calls = bug_pages(monkeypatch, pages)
    with pytest.raises(ValueError, match='five-page limit'):
        reader.read_bugs('o', 'r', 'alice', 7)
    assert len(calls) == 6
    page = [bug(i + 1, pull_request={}) for i in range(100)]
    bug_pages(monkeypatch, [page, page])
    with pytest.raises(ValueError, match='pagination changed'):
        reader.read_bugs('o', 'r', 'alice', 7)


@pytest.mark.asyncio
async def test_real_stdio_filtered_bug_tool(tmp_path):
    pytest.importorskip('mcp')
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\nimport json,sys\n'
                  f'print(json.dumps({{"id": 7, "login": "alice"}} if sys.argv[-1].startswith("users/") '
                  f'else {[bug(), bug(2, user={"id": 9, "login": "eve"})]!r}))\n')
    gh.chmod(0o755)
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
        env={'PATH': str(tmp_path) + os.pathsep + os.environ.get('PATH', '')})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool('read_bugs', dict(owner='o', repo='r', author='alice', author_id=7))
            assert not result.isError
            assert [record['id'] for record in result.structuredContent['issues']] == [1]
            assert result.structuredContent['complete'] is True
