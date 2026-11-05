import asyncio

import httpx
import pytest
from fastapi import FastAPI

from agent import continuations as store, stats_db as db, watch_launch
from agent.continuation_api import router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'test.db')
    db.init_db()
    store.initialize()
    store.configure(True)
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: store.capture_scope([], None))
    user = db.insert_user_message('test', 'codex', 'Fix the bug and commit it')
    parent = db.insert_assistant_message('test', 'codex', user)
    token = store.mint(parent)
    yield parent, token
    store.configure(False)
    watch_launch.configure(None)


BODY = dict(request_key='commit', mode='backing_git', prompt='Commit the requested fix')


def test_scope_idempotency_and_single_successor(setup):
    parent, token = setup
    row = store.create(token, BODY)
    assert row['state'] == 'waiting'
    assert store.create(token, BODY) == row
    with pytest.raises(ValueError, match='conflict'):
        store.create(token, BODY | {'prompt': 'different'})
    with pytest.raises(ValueError):
        store.create(token, BODY | {'topic': 'other'})
    assert store.claim(set()) is None
    other_user = db.insert_user_message('other', 'codex', 'hello')
    other = db.insert_assistant_message('other', 'codex', other_user)
    assert store.inspect(store.mint(other)) == []
    with pytest.raises(ValueError):
        store.inspect(store.mint(other), row['id'])


@pytest.mark.parametrize('success', [False, True])
def test_gate_claim_busy_topic_and_child_budget(setup, success):
    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'fixed', None, 'done')
    store.complete(parent, success, 'synced' if success else 'conflict')
    assert store.claim({'test'}) is None
    claimed = store.claim(set())
    if not success:
        assert claimed is None
        assert store.inspect(token)[0]['state'] == 'blocked'
        return
    assert claimed['id'] == row['id']
    assert store.claim(set()) is None
    user = db.insert_user_message('test', 'codex', 'continuation')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    store.link(row['id'], child)
    assert store.mint(child) is None
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'committed')
    assert store.inspect(token)[0]['state'] == 'successful'


def test_recovery_never_replays_started_work(setup):
    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'fixed', None, 'done')
    store.complete(parent, True, 'synced')
    store.recover()
    assert store.claim(set())['id'] == row['id']
    store.recover()
    with db._connection() as conn:
        assert conn.execute('SELECT state FROM continuations').fetchone()[0] == 'ambiguous'
    assert store.claim(set()) is None
    with pytest.raises(PermissionError):
        store.inspect(token)


def test_cancel_and_unknown_parent_recovery(setup):
    parent, token = setup
    row = store.create(token, BODY)
    assert store.inspect(token, row['id'], True)[0]['state'] == 'cancelled'
    store.complete(parent, True, 'synced')
    assert store.claim(set()) is None


def test_watch_and_flow_no_authority(setup):
    parent, _ = setup
    with db._connection() as conn:
        conn.execute("UPDATE chat_messages SET source='watch' WHERE id=?", (parent,))
    assert store.mint(parent) is None
    with db._connection() as conn:
        conn.execute("UPDATE chat_messages SET source='human',flow_run_id='flow' WHERE id=?", (parent,))
    assert store.mint(parent) is None


@pytest.mark.asyncio
async def test_api_auth_validation_and_topic_visibility(setup):
    _, token = setup
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
        assert (await c.post('/continuations', json=BODY)).status_code == 403
        headers = {'Authorization': 'Bearer ' + token}
        response = await c.post('/continuations', json=BODY, headers=headers)
        assert response.status_code == 200
        row = response.json()
        assert (await c.post('/continuations', json=BODY | {'mode': 'publish'}, headers=headers)).status_code == 400
        assert (await c.post('/continuations', json=BODY | {'prompt': 'different'}, headers=headers)).status_code == 409
        assert (await c.get('/continuations/' + row['id'], headers=headers)).json()[0]['id'] == row['id']
        assert (await c.post('/continuations/' + row['id'] + '/cancel', headers=headers)).json()[0]['state'] == 'cancelled'


@pytest.mark.asyncio
async def test_launch_delivers_and_revokes_without_watches(setup, tmp_path):
    parent, _ = setup
    watch_launch.configure(None, 'http://127.0.0.1:1234')
    observed = []
    expected_scope = store.capture_scope([str(tmp_path / 'original')], None)
    async def runner(prompt, **kwargs):
        token = kwargs['backend_env']['SQUID_CONTINUE_TOKEN']
        observed.append(token)
        import json
        row = store.create(token, BODY)
        assert row['parent_id'] == parent
        assert json.loads(row['scope_json']) == expected_scope
        assert 'continuation_scope' not in kwargs
        assert 'agentsquid continue --help' in prompt
        yield 'ok'
    assert [x async for x in watch_launch.run(runner, 'fix', topic='test', msg_id=parent, continuation_scope=expected_scope)] == ['ok']
    with pytest.raises(PermissionError):
        store.inspect(observed[0])


def test_completion_requires_durable_done_status(setup):
    parent, token = setup
    store.create(token, BODY)
    store.complete(parent, True, 'completed_and_synced')
    assert store.inspect(token)[0]['state'] == 'blocked'


def test_recover_waiting_without_gate_is_blocked(setup):
    _, token = setup
    store.create(token, BODY)
    store.recover()
    with db._connection() as conn:
        row = conn.execute('SELECT * FROM continuations').fetchone()
        assert (row['state'], row['reason']) == ('blocked', 'parent_completion_unknown')


@pytest.mark.asyncio
async def test_dispatch_same_agent_topic_and_linked_result(setup, monkeypatch):
    from agent import server
    parent, token = setup
    accepted = store.create(token, BODY)
    db.update_assistant_message(parent, 'Fixed and tested', None, 'done')
    store.complete(parent, True, 'synced')
    row = store.claim(set())
    prepared = {}
    async def prepare(**kwargs):
        assert kwargs['source'] == 'continuation'
        assert kwargs['adhoc'] is False
        assert kwargs['agent'] == 'codex' and kwargs['topic'] == 'test'
        assert 'Fix the bug and commit it' in kwargs['message']
        user = db.insert_user_message('test', 'codex', kwargs['message'])
        child = db.insert_assistant_message('test', 'codex', user, source='continuation')
        keys = ('context_history', 'harness', 'provider', 'backend', 'model', 'cwd',
                'source_cwd', 'configured_cwd', 'resume_session_id', 'code_roots',
                'worktree_setup_elapsed_ms')
        prepared.update({k: None for k in keys})
        prepared.update(topic='test', agent='codex', asst_msg_id=child, adhoc=True,
                        lookback=0, display_prompt=kwargs['message'], worktree_isolated=False,
                        response_timeout=3600, effective_message=kwargs['message'])
        return prepared
    class Dispatcher:
        async def dispatch(self, **kwargs):
            child = kwargs['msg_id']
            assert store.git_only(child)
            assert store.mint(child) is None
            assert kwargs['response_timeout'] == 600
            db.update_assistant_message(child, 'Committed as abc123', None, 'done')
            store.complete(child, True, 'completed_and_synced')
            out = asyncio.Queue()
            await out.put(None)
            return out, None, None
    monkeypatch.setattr(server, '_prepare_chat_turn', prepare)
    await store.dispatch(row, Dispatcher())
    result = store.inspect(token)[0]
    assert result['id'] == accepted['id'] and result['state'] == 'successful'
    assert db.get_message(result['child_id'])['content'] == 'Committed as abc123'


@pytest.mark.asyncio
@pytest.mark.parametrize('sync_result', [[], ['app.txt']])
@pytest.mark.parametrize('stream_failure', [False, True])
async def test_worker_gates_successor_after_actual_sync(setup, monkeypatch, tmp_path, sync_result, stream_failure):
    from unittest.mock import patch
    from agent.topic_queue import TopicWorker, QueueItem
    parent, token = setup
    store.create(token, BODY)
    async def runner(*args, **kwargs):
        yield 'Fixed and tested'
        if stream_failure:
            yield {'_error': 'Harness failed'}
    sync_calls = []
    def sync(*args, **kwargs):
        assert store.claim(set()) is None
        sync_calls.append(args)
        return sync_result
    isolated = tmp_path / 'isolated'
    isolated.mkdir()
    rec = dict(repo_root=str(tmp_path), worktree_path=str(isolated))
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='fix', context_history=[],
                     backend='codex', model=None, cwd=str(isolated), code_roots=[str(isolated)],
                     adhoc=True, msg_id=parent, worktree_isolated=True)
    with patch('agent.runners.run_codex', runner), \
         patch('agent.stats_db.get_worktrees', return_value=[rec]), \
         patch('agent.git_changes.prepare_trackers', return_value=[]), \
         patch('agent.worktree.sync_after_turn', sync), \
         patch('agent.stats_db.mark_worktree_status'), \
         patch('agent.stats_db.mark_worktree_synced'):
        await TopicWorker('test')._process(item)
    assert len(sync_calls) == 1
    assert store.inspect(token)[0]['state'] == ('blocked' if sync_result or stream_failure else 'eligible')


def test_following_human_turn_can_inspect_topic_outcomes(setup):
    _, token = setup
    row = store.create(token, BODY)
    store.revoke(token)
    user = db.insert_user_message('test', 'codex', 'Check continuation status')
    following = db.insert_assistant_message('test', 'codex', user)
    assert store.inspect(store.mint(following), row['id'])[0]['id'] == row['id']


def test_cli_routing_and_secrets(monkeypatch, capsys, tmp_path):
    import json
    from agent import continuation_cli
    requests = []
    original_client = httpx.Client
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'state': 'waiting'})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setenv('SQUID_API_URL', 'http://127.0.0.1:1234')
    monkeypatch.setenv('SQUID_CONTINUE_TOKEN', 'secret')
    payload = tmp_path / 'input.json'
    payload.write_text(json.dumps(BODY))
    assert continuation_cli.main(['create', '--input', str(payload)]) == 0
    assert requests[0].url.path == '/continuations'
    assert requests[0].headers['authorization'] == 'Bearer secret'
    monkeypatch.setenv('SQUID_API_URL', 'http://remote.invalid')
    assert continuation_cli.main(['list']) == 1
    assert len(requests) == 1
    assert 'secret' not in str(capsys.readouterr())


@pytest.mark.asyncio
async def test_edit_sync_then_real_local_commit(setup, monkeypatch, tmp_path):
    """Deterministic agent, real isolation/promotion/Git; no live model or remote."""
    import subprocess
    from unittest.mock import patch
    from agent import worktree, server, config
    from agent.topic_queue import TopicWorker, QueueItem
    parent, token = setup
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'app.txt').write_text('before\n')
    git('add', 'app.txt')
    git('commit', '-m', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: store.capture_scope([str(repo)], str(repo)))
    token = store.mint(parent)
    monkeypatch.setattr(worktree, '_WORKTREES_HOME', tmp_path / 'turns')
    monkeypatch.setattr(config, 'WORKTREE_ISOLATION_ENABLED', False)
    isolated = worktree.ensure_worktree(repo, 'test', str(parent))
    db.save_worktree('test', str(parent), str(repo), str(isolated), '',
                     worktree.base_commit_for(repo, 'test', str(parent)))
    async def edit_runner(*args, **kwargs):
        (isolated / 'app.txt').write_text('after\n')
        store.create(token, BODY)
        assert (repo / 'app.txt').read_text() == 'before\n'
        assert store.claim(set()) is None
        yield 'Fixed and tested; commit scheduled'
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='fix and commit',
                     context_history=[], backend='codex', model=None, cwd=str(isolated),
                     code_roots=[str(isolated)], adhoc=True, msg_id=parent, worktree_isolated=True)
    with patch('agent.runners.run_codex', edit_runner):
        await TopicWorker('test')._process(item)
    assert (repo / 'app.txt').read_text() == 'after\n'
    assert git('rev-parse', 'HEAD') == baseline
    row = store.claim(set())
    assert row is not None
    async def prepare(**kwargs):
        user = db.insert_user_message('test', 'codex', kwargs['message'], source='continuation')
        child = db.insert_assistant_message('test', 'codex', user, source='continuation')
        return dict(topic='test', agent='codex', context_history=[], harness='codex',
                    provider=None, backend='codex', model=None, cwd=str(repo),
                    source_cwd=str(repo), configured_cwd=str(repo), response_timeout=600,
                    resume_session_id=None, adhoc=False, lookback=0, code_roots=[str(repo)],
                    display_prompt=kwargs['message'], worktree_setup_elapsed_ms=None,
                    worktree_isolated=False, asst_msg_id=child, effective_message=kwargs['message'])
    async def commit_runner(*args, **kwargs):
        assert (repo / 'app.txt').read_text() == 'after\n'
        git('add', 'app.txt')
        git('commit', '-m', 'Fix requested bug')
        yield 'Committed as ' + git('rev-parse', '--short', 'HEAD')
    class Dispatcher:
        async def dispatch(self, **kwargs):
            child = QueueItem(seq=0, topic='test', agent='codex', prompt=kwargs['prompt'],
                              context_history=[], backend='codex', model=None, cwd=str(repo),
                              code_roots=[str(repo)], configured_cwd=str(repo), adhoc=True, msg_id=kwargs['msg_id'])
            with patch('agent.runners.run_codex', commit_runner):
                await TopicWorker('test')._process(child)
            return child.out_q, None, None
    monkeypatch.setattr(server, '_prepare_chat_turn', prepare)
    await store.dispatch(row, Dispatcher())
    assert store.inspect(token)[0]['state'] == 'successful'
    assert git('show', 'HEAD:app.txt') == 'after'
    assert git('rev-list', '--count', 'HEAD') == '2'
    assert git('status', '--porcelain') == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('settlement', ['cleanup', 'admission', 'direct'])
@pytest.mark.parametrize('ending', ['finished', 'timeout', 'cancelled', 'interrupted'])
@pytest.mark.parametrize('file_kind', ['tracked', 'ignored'])
async def test_git_only_rejected_edits_stay_quarantined(setup, monkeypatch, tmp_path, settlement, ending, file_kind):
    import subprocess
    from unittest.mock import patch
    from agent import worktree, config
    from agent.topic_queue import TopicWorker, QueueItem
    parent, _ = setup
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'app.txt').write_text('base\n')
    (repo / '.gitignore').write_text('.env\n')
    monkeypatch.setattr(config, 'WORKTREE_LINK_IGNORED_FILES', ['.env'])
    git('add', '.')
    git('commit', '-m', 'baseline')
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: store.capture_scope([str(repo)], None))
    token = store.mint(parent)
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'synced')
    store.claim(set())
    user = db.insert_user_message('test', 'codex', 'Commit only', source='continuation')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    store.link(row['id'], child)
    monkeypatch.setattr(worktree, '_WORKTREES_HOME', tmp_path / 'turns')
    monkeypatch.setattr(worktree, '_CLEANUP_GRACE_SECONDS', 0)
    monkeypatch.setattr(config, 'WORKTREE_ISOLATION_ENABLED', False)
    isolated = worktree.ensure_worktree(repo, 'test', str(child))
    db.save_worktree('test', str(child), str(repo), str(isolated), '',
                     worktree.base_commit_for(repo, 'test', str(child)), status='active')
    assert worktree.sync_after_turn(repo, 'test', str(child)) == []
    edited_file = isolated / ('app.txt' if file_kind == 'tracked' else '.env')
    async def runner(*args, **kwargs):
        edited_file.write_text('forbidden edit\n')
        yield 'Working'
        if ending == 'timeout':
            from agent.runners import CLIError
            raise CLIError('Response timeout exceeded')
        if ending == 'cancelled':
            raise asyncio.CancelledError()
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='commit only', context_history=[],
                     backend='codex', model=None, cwd=str(isolated), code_roots=[str(isolated)],
                     adhoc=True, msg_id=child, worktree_isolated=True)
    if ending == 'interrupted':
        edited_file.write_text('forbidden edit\n')
        store.recover()
        store.configure(False)  # Recovery must enforce persisted mode even when disabled.
    else:
        with patch('agent.runners.run_codex', runner):
            if ending == 'cancelled':
                with pytest.raises(asyncio.CancelledError):
                    await TopicWorker('test')._process(item)
            else:
                await TopicWorker('test')._process(item)
        assert store.inspect(token)[0]['state'] == 'failed'
    if settlement == 'cleanup':
        await worktree.cleanup_worktrees('test')
    elif settlement == 'direct':
        with pytest.raises(RuntimeError, match='promotion blocked'):
            # A cached no-op snapshot must not hide writes after interruption.
            worktree.sync_after_turn(repo, 'test', str(child),
                turn_tree=worktree._run_git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip())
    else:
        blockers = await worktree.settle_worktrees_before_turn('test', [repo])
        assert blockers[0]['status'] == 'promotion_failed'
    assert (repo / 'app.txt').read_text() == 'base\n'
    assert not (repo / '.env').exists()
    assert edited_file.read_text() == 'forbidden edit\n'
    assert db.get_worktrees('test', str(child))[0]['status'] == 'promotion_failed'


@pytest.mark.asyncio
async def test_scope_drift_after_accept_blocks_before_preparation(setup, monkeypatch, tmp_path):
    from agent import server
    parent, _ = setup
    original = store.capture_scope([str(tmp_path / 'original')], None)
    current = [original]
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: current[0])
    token = store.mint(parent)
    # Scope belongs to the executing parent, even if config changes before create.
    current[0] = store.capture_scope([str(tmp_path / 'other')], None)
    row = store.create(token, BODY)
    import json
    assert json.loads(row['scope_json']) == original
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'synced')
    claimed = store.claim(set())
    async def unexpected_prepare(**kwargs):
        pytest.fail('Changed repository scope must not prepare a child')
    monkeypatch.setattr(server, '_prepare_chat_turn', unexpected_prepare)
    await store.dispatch(claimed, None)
    assert store.inspect(token)[0]['reason'] == 'repository_scope_changed'
    assert store.inspect(token)[0]['state'] == 'blocked'


@pytest.mark.asyncio
async def test_scope_drift_while_queued_blocks_before_runner(setup, monkeypatch, tmp_path):
    from unittest.mock import patch
    from agent.topic_queue import TopicWorker, QueueItem
    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'synced')
    store.claim(set())
    user = db.insert_user_message('test', 'codex', 'Commit', source='continuation')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    store.link(row['id'], child)
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: store.capture_scope([str(tmp_path)], None))
    async def unexpected_runner(*args, **kwargs):
        pytest.fail('Changed scope must not start the agent')
        yield ''
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='commit', context_history=[],
                     backend='codex', model=None, cwd=str(tmp_path), code_roots=[], adhoc=True, msg_id=child)
    with patch('agent.runners.run_codex', unexpected_runner):
        await TopicWorker('test')._process(item)
    assert store.inspect(token)[0]['state'] == 'blocked'
    assert store.inspect(token)[0]['reason'] == 'repository_scope_changed'


def test_migration_preserves_missing_scope_as_unknown(setup):
    parent, token = setup
    row = store.create(token, BODY)
    with db._connection() as conn:
        conn.execute('ALTER TABLE continuations DROP COLUMN scope_json')
        conn.execute('ALTER TABLE continuation_credentials DROP COLUMN scope_json')
    store.initialize()
    store.initialize()
    with db._connection() as conn:
        legacy = dict(conn.execute('SELECT * FROM continuations').fetchone())
    assert legacy['id'] == row['id']
    assert not store.scope_matches(legacy)
    with db._connection() as conn:
        assert conn.execute('SELECT scope_json FROM continuation_credentials').fetchone()[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,original_request,resume', [
    ('/compact', None, None), ('  /help', None, None),
    ('runtime context\n/compact', '/compact', None),
    ('normal request', None, 'existing-session'),
])
async def test_continuation_notice_preserves_commands_and_resumes(setup, prompt, original_request, resume):
    parent, _ = setup
    watch_launch.configure(None, 'http://127.0.0.1:1234')
    async def runner(actual, **kwargs):
        assert actual == prompt
        assert kwargs['backend_env']['SQUID_CONTINUE_TOKEN']
        yield 'ok'
    assert [x async for x in watch_launch.run(runner, prompt, request_text=original_request,
        resume_session_id=resume, topic='test', msg_id=parent)] == ['ok']


@pytest.mark.asyncio
async def test_later_human_turn_can_cancel_same_topic_but_not_other_topic(setup):
    parent, token = setup
    row = store.create(token, BODY)
    store.revoke(token)
    user = db.insert_user_message('test', 'codex', 'Cancel the pending continuation')
    later = db.insert_assistant_message('test', 'codex', user)
    later_token = store.mint(later)
    other_user = db.insert_user_message('other', 'codex', 'Cancel')
    other = db.insert_assistant_message('other', 'codex', other_user)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        path = '/continuations/' + row['id'] + '/cancel'
        assert (await client.post(path, headers={'Authorization': 'Bearer ' + store.mint(other)})).status_code == 404
        response = await client.post(path, headers={'Authorization': 'Bearer ' + later_token})
        assert response.status_code == 200
        assert response.json()[0]['state'] == 'cancelled'
        assert response.json()[0]['parent_id'] == parent


@pytest.mark.asyncio
async def test_api_deep_json_and_specific_lifecycle_errors(setup):
    parent, token = setup
    app = FastAPI()
    app.include_router(router)
    headers = {'Authorization': 'Bearer ' + token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', headers=headers) as client:
        response = await client.post('/continuations', content='[' * 2000 + '0' + ']' * 2000)
        assert response.status_code == 400
        assert response.json() == {'error': 'invalid_continuation_request'}
        with db._connection() as conn:
            conn.execute('UPDATE continuation_credentials SET scope_json=NULL')
        response = await client.post('/continuations', json=BODY)
        assert response.status_code == 409
        assert response.json() == {'error': 'original_scope_unavailable'}
        db.update_assistant_message(parent, 'Finished', None, 'done')
        response = await client.post('/continuations', json=BODY)
        assert response.status_code == 409
        assert response.json() == {'error': 'parent_not_running'}


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['successful', 'failed', 'cancelled', 'blocked', 'claimed', 'running'])
async def test_dispatch_exception_preserves_terminal_outcomes(setup, monkeypatch, outcome):
    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'synced')
    original_claim = store.claim
    count = 0
    def claim(busy):
        nonlocal count
        count += 1
        if count > 1:
            raise asyncio.CancelledError()
        return original_claim(busy)
    async def dispatch(claimed, dispatcher):
        if outcome in {'successful', 'failed'}:
            user = db.insert_user_message('test', 'codex', 'Commit', source='continuation')
            child = db.insert_assistant_message('test', 'codex', user, source='continuation')
            store.link(row['id'], child)
            db.update_assistant_message(child, 'Result', None, 'done' if outcome == 'successful' else 'error')
            store.complete(child, outcome == 'successful', 'recorded_result')
        else:
            with db._connection() as conn:
                conn.execute('UPDATE continuations SET state=?,reason=? WHERE id=?',
                             (outcome, 'recorded_result', row['id']))
        raise RuntimeError('Failure after persisting outcome')
    class Dispatcher:
        def busy_topics(self):
            return set()
    monkeypatch.setattr(store, 'claim', claim)
    monkeypatch.setattr(store, 'dispatch', dispatch)
    with pytest.raises(asyncio.CancelledError):
        await store.maintain(Dispatcher())
    result = store.inspect(token)[0]
    assert result['state'] == ('ambiguous' if outcome in {'claimed', 'running'} else outcome)
    assert result['reason'] == ('dispatch_failed' if outcome in {'claimed', 'running'} else 'recorded_result')


@pytest.mark.parametrize('worktree_enabled', [False, True])
@pytest.mark.parametrize('legacy_setting', [None, {'enabled': False}, {'enabled': True}])
def test_startup_continuations_follow_worktree_setting(setup, monkeypatch, worktree_enabled, legacy_setting):
    from fastapi.testclient import TestClient
    from agent import server
    monkeypatch.setattr(server, 'WORKTREE_ISOLATION_ENABLED', worktree_enabled)
    monkeypatch.setitem(server._cfg, 'continuations', legacy_setting)
    monkeypatch.setitem(server._cfg, 'watches', {})
    monkeypatch.setitem(server._cfg, 'shore', {'enabled': False})
    monkeypatch.setattr(server, '_sync_pi_agents_models_store', lambda: None)
    with TestClient(server.app) as client:
        assert client.get('/health').status_code == 200
        assert store._enabled is worktree_enabled


@pytest.mark.asyncio
async def test_startup_auth_failure_keeps_continuations_disabled(setup, monkeypatch):
    from agent import server, watch_auth
    monkeypatch.setattr(server, 'WORKTREE_ISOLATION_ENABLED', True)
    monkeypatch.setitem(server._cfg, 'watches', {})
    def reject_auth(*args, **kwargs):
        assert store._enabled is False
        raise ValueError('invalid control credential')
    monkeypatch.setattr(watch_auth, 'load_digest', reject_auth)
    with pytest.raises(ValueError, match='invalid control credential'):
        async with server._lifespan(server.app):
            pytest.fail('Invalid credentials must prevent startup')
    assert store._enabled is False


def test_common_database_setup_initializes_continuations(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'common.db')
    db.init_db()
    db.init_db()
    with db._connection() as conn:
        for table in ('continuations', 'continuation_credentials'):
            assert 'scope_json' in db._table_columns(conn, table)
        assert 'success' in db._table_columns(conn, 'turn_completion_gates')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_stage', ['flow_recovery', 'monitoring_start'])
async def test_startup_failure_cancels_worker_before_retry(setup, monkeypatch, failure_stage):
    from agent import server, flow
    from agent.watch_service import WatchService
    monkeypatch.setattr(server, 'WORKTREE_ISOLATION_ENABLED', True)
    monkeypatch.setitem(server._cfg, 'watches', {})
    monkeypatch.setitem(server._cfg, 'shore', {'enabled': False})
    monkeypatch.setattr(server, '_sync_pi_agents_models_store', lambda: None)
    workers = []
    durable_workers = []
    async def worker(dispatcher):
        workers.append(asyncio.current_task())
        await asyncio.Event().wait()
    async def durable_worker():
        durable_workers.append(asyncio.current_task())
        await asyncio.Event().wait()
    fail = [True]
    async def recovery():
        # Let the continuation worker start before injecting the failure.
        await asyncio.sleep(0)
        if fail[0] and failure_stage == 'flow_recovery':
            raise RuntimeError('injected startup failure')
    original_start = WatchService.start_monitoring
    def start_monitoring(service):
        if fail[0] and failure_stage == 'monitoring_start':
            raise RuntimeError('injected startup failure')
        return original_start(service)
    monkeypatch.setattr(store, 'maintain', worker)
    monkeypatch.setattr(flow, 'maintain_durable_flows', durable_worker)
    monkeypatch.setattr(server, '_resume_stalled_flows_on_startup', recovery)
    monkeypatch.setattr(WatchService, 'start_monitoring', start_monitoring)
    try:
        with pytest.raises(RuntimeError, match='injected startup failure'):
            async with server._lifespan(server.app):
                pytest.fail('Startup failure must not serve requests')
        assert workers and all(task.done() for task in workers + durable_workers)
        assert store._enabled is False
        assert watch_launch._service is None
        fail[0] = False
        async with server._lifespan(server.app):
            await asyncio.sleep(0)
            assert sum(not task.done() for task in workers) == 1
            assert sum(not task.done() for task in durable_workers) == 1
            assert store._enabled is True
        assert all(task.done() for task in workers + durable_workers)
        assert store._enabled is False
    finally:
        # Keep the test from leaking workers if its regression assertions fail.
        for task in workers + durable_workers:
            task.cancel()
        await asyncio.gather(*workers, *durable_workers, return_exceptions=True)
