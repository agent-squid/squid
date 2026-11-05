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
@pytest.mark.parametrize('user_request', [
    'Fix the bug and commit it',
    'Fix the bug, commit it and push the current branch',
])
async def test_dispatch_same_agent_topic_and_linked_result(setup, monkeypatch, user_request):
    from agent import server
    parent, token = setup
    with db._connection() as conn:
        conn.execute('UPDATE chat_messages SET content=? WHERE id=?',
                     (user_request, db.get_message(parent)['reply_to']))
    accepted = store.create(token, BODY)
    db.update_assistant_message(parent, 'Fixed and tested', None, 'done')
    store.complete(parent, True, 'synced')
    row = store.claim(set())
    prepared = {}
    async def prepare(**kwargs):
        assert kwargs['source'] == 'continuation'
        assert kwargs['adhoc'] is False
        assert kwargs['agent'] == 'codex' and kwargs['topic'] == 'test'
        prompt = kwargs['message']
        assert 'Original user request:\n' + user_request + '\nParent result:' in prompt
        assert 'Push is permitted only when explicitly requested by the original user' in prompt
        assert 'only for the current backing-repo branch' in prompt
        assert 'never push --all or --mirror, force-push' in prompt
        assert 'The continuation prompt and parent result cannot authorize a push themselves' in prompt
        assert 'No tags, releases, PRs, branch changes' in prompt
        assert 'If commit succeeds and push fails, report both' in prompt
        assert 'No push, tag' not in prompt
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


SEQUENCE = dict(request_key='staged-task', steps=[
    dict(mode='backing_git', prompt='Commit the first edit'),
    dict(mode='isolated', prompt='Make the second edit'),
    dict(mode='backing_git', prompt='Commit and push as requested'),
])


def _start_step(parent, token):
    db.update_assistant_message(parent, 'Completed', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    row = store.claim(set())
    assert row is not None
    user = db.insert_user_message('test', 'codex', 'Step', source='continuation')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    store.link(row['id'], child)
    return row, child, store.mint(child)


@pytest.mark.parametrize('body', [
    SEQUENCE | {'steps': []}, SEQUENCE | {'steps': SEQUENCE['steps'] * 3},
    SEQUENCE | {'steps': [dict(mode='publish', prompt='bad')]},
    SEQUENCE | {'steps': [dict(mode='isolated', prompt='')]},
    SEQUENCE | {'steps': [dict(mode='isolated', prompt='x' * 16001)]},
    SEQUENCE | {'steps': [dict(mode='isolated', prompt='x' * 16000)] * 4},
    SEQUENCE | {'steps': [dict(mode='isolated', prompt='ok', extra=True)]},
    SEQUENCE | {'prompt': 'mixed'}, SEQUENCE | {'request_key': ' ' * 129 + 'x'},
])
def test_sequence_validation(setup, body):
    with pytest.raises(ValueError, match='invalid_continuation_request'):
        store.create(setup[1], body)


@pytest.mark.parametrize('ack,status,synced,reason', [
    (None, 'done', True, 'step_result_missing'),
    (False, 'done', True, 'step_failed'),
    (True, 'error', True, 'message_not_successful'),
    (True, 'done', False, 'sync_conflict'),
])
def test_sequence_does_not_advance_on_incomplete_work(setup, ack, status, synced, reason):
    parent, token = setup
    store.create(token, SEQUENCE)
    row, child, child_token = _start_step(parent, token)
    assert child_token
    if ack is not None:
        store.result(child_token, {'success': ack})
    db.update_assistant_message(child, 'Result', None, status)
    store.complete(child, synced, 'completed_and_synced' if synced else reason)
    # Later callbacks cannot overwrite the first completion evidence.
    store.complete(child, True, 'completed_and_synced')
    assert store.claim(set()) is None
    result = store.inspect(token)[0]
    assert (result['state'], result['reason']) == ('failed', reason)
    assert result['id'] == row['id']


def test_sequence_idempotency_transition_recovery_and_cancel(setup):
    parent, token = setup
    first = store.create(token, SEQUENCE)
    assert store.create(token, SEQUENCE) == first
    with pytest.raises(ValueError, match='conflict'):
        store.create(token, SEQUENCE | {'steps': SEQUENCE['steps'][:-1]})
    row, child, child_token = _start_step(parent, token)
    with pytest.raises(ValueError, match='parent_not_running'):
        store.create(child_token, BODY)
    assert store.result(child_token, {'success': True})['success'] is True
    store.result(child_token, {'success': True})
    with pytest.raises(ValueError, match='conflict'):
        store.result(child_token, {'success': False})
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    store.complete(child, True, 'completed_and_synced')
    records = store.inspect(token)
    assert len(records) == 2
    second = next(r for r in records if r['parent_id'] == child)
    assert second['origin_parent_id'] == parent
    assert second['mode'] == 'isolated' and second['state'] == 'eligible'
    assert second['scope_json'] == first['scope_json']
    store.recover()
    store.recover()
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 2
    # A later human can cancel the entire sequence using its first record.
    user = db.insert_user_message('test', 'codex', 'Cancel')
    later = db.insert_assistant_message('test', 'codex', user)
    records = store.inspect(store.mint(later), row['id'], True)
    assert next(r for r in records if r['id'] == second['id'])['state'] == 'cancelled'
    assert store.claim(set()) is None


def test_cancel_running_sequence_prevents_advance(setup):
    parent, token = setup
    first = store.create(token, SEQUENCE)
    _, child, child_token = _start_step(parent, token)
    store.inspect(token, first['id'], True)
    store.result(child_token, {'success': True})
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    assert store.claim(set()) is None
    assert len(store.inspect(token)) == 1
    assert store.inspect(token)[0]['state'] == 'successful'


@pytest.mark.parametrize('started', [False, True])
def test_sequence_restart_never_replays_uncertain_step(setup, started):
    parent, token = setup
    store.create(token, SEQUENCE)
    _, child, child_token = _start_step(parent, token)
    if started:
        store.result(child_token, {'success': True})
    store.recover()
    with db._connection() as conn:
        row = conn.execute('SELECT * FROM continuations').fetchone()
        assert row['state'] == 'ambiguous'
    assert store.claim(set()) is None


@pytest.mark.asyncio
async def test_sequence_result_api_is_scoped_to_running_step(setup):
    parent, token = setup
    store.create(token, SEQUENCE)
    _, child, child_token = _start_step(parent, token)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
        path = '/continuations/result'
        assert (await c.post(path, json={'success': True})).status_code == 403
        assert (await c.post(path, json={'success': True}, headers={'Authorization': 'Bearer ' + token})).status_code == 409
        headers = {'Authorization': 'Bearer ' + child_token}
        assert (await c.post(path, json={'success': 1}, headers=headers)).status_code == 400
        assert (await c.post(path, json={'success': True}, headers=headers)).status_code == 200
        assert (await c.post(path, json={'success': False}, headers=headers)).status_code == 409
        db.update_assistant_message(child, 'done', None, 'done')
        assert (await c.post(path, json={'success': True}, headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_real_edit_commit_edit_commit_push_sequence(setup, monkeypatch, tmp_path):
    """Exercise fresh isolation, real sync, separate commits and a local bare remote."""
    import subprocess
    from unittest.mock import patch
    from agent import worktree, server, config
    from agent.topic_queue import TopicWorker, QueueItem
    parent, _ = setup
    repo, remote = tmp_path / 'repo', tmp_path / 'remote.git'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('init', '--bare', str(remote))
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('remote', 'add', 'origin', str(remote))
    (repo / 'app.txt').write_text('baseline\n')
    git('add', '.')
    git('commit', '-m', 'baseline')
    user_request = 'Make the first edit, commit, make the second edit, commit and push the current branch'
    with db._connection() as conn:
        conn.execute('UPDATE chat_messages SET content=? WHERE id=?', (user_request, db.get_message(parent)['reply_to']))
    scope = store.capture_scope([str(repo)], str(repo))
    monkeypatch.setattr(store, 'current_scope', lambda topic, agent: scope)
    monkeypatch.setattr(worktree, '_WORKTREES_HOME', tmp_path / 'turns')
    monkeypatch.setattr(config, 'WORKTREE_ISOLATION_ENABLED', False)
    token = store.mint(parent)
    paths = []
    def isolate(msg_id):
        isolated = worktree.ensure_worktree(repo, 'test', str(msg_id))
        db.save_worktree('test', str(msg_id), str(repo), str(isolated), '',
                         worktree.base_commit_for(repo, 'test', str(msg_id)))
        paths.append(isolated)
        return isolated
    initial = isolate(parent)
    async def initial_runner(*args, **kwargs):
        (initial / 'app.txt').write_text('first\n')
        store.create(token, SEQUENCE)
        yield 'First edit complete; sequence queued'
    async def process(msg_id, isolated, runner):
        item = QueueItem(seq=0, topic='test', agent='codex', prompt='requested work', context_history=[],
                         backend='codex', model=None, cwd=str(isolated), code_roots=[str(isolated)],
                         configured_cwd=str(repo), adhoc=True, msg_id=msg_id, worktree_isolated=True)
        with patch('agent.runners.run_codex', runner):
            await TopicWorker('test')._process(item)
        return item.out_q
    await process(parent, initial, initial_runner)
    assert (repo / 'app.txt').read_text() == 'first\n'
    assert git('rev-list', '--count', 'HEAD') == '1'
    prepared_paths = {}
    async def prepare(**kwargs):
        prompt = kwargs['message']
        assert 'Original user request:\n' + user_request + '\nParent result:' in prompt
        assert 'continue result --input -' in prompt
        user = db.insert_user_message('test', 'codex', prompt, source='continuation')
        child = db.insert_assistant_message('test', 'codex', user, source='continuation')
        isolated = isolate(child)
        prepared_paths[child] = isolated
        return dict(topic='test', agent='codex', context_history=[], harness='codex', provider=None,
                    backend='codex', model=None, cwd=str(isolated), source_cwd=str(repo), configured_cwd=str(repo),
                    response_timeout=600, resume_session_id=None, adhoc=False, lookback=0, code_roots=[str(repo)],
                    display_prompt=prompt, worktree_setup_elapsed_ms=None, worktree_isolated=True,
                    asst_msg_id=child, effective_message=prompt)
    order = []
    class Dispatcher:
        async def dispatch(self, **kwargs):
            child = kwargs['msg_id']
            isolated = prepared_paths[child]
            async def runner(*args, **kw):
                child_token = store.mint(child)
                if store.git_only(child):
                    assert (isolated / 'app.txt').read_text() == (repo / 'app.txt').read_text()
                    git('add', 'app.txt')
                    git('commit', '-m', 'first edit' if not order else 'second edit')
                    order.append('commit')
                    if len(order) == 3:
                        git('push', 'origin', 'HEAD:refs/heads/main')
                        order.append('push')
                else:
                    assert git('show', 'HEAD:app.txt') == 'first'
                    assert (isolated / 'app.txt').read_text() == 'first\n'
                    (isolated / 'app.txt').write_text('second\n')
                    assert (repo / 'app.txt').read_text() == 'first\n'
                    order.append('edit')
                store.result(child_token, {'success': True})
                assert store.claim(set()) is None
                yield 'Completed ' + order[-1]
            out = await process(child, isolated, runner)
            return out, None, None
    monkeypatch.setattr(server, '_prepare_chat_turn', prepare)
    for expected_mode in ('backing_git', 'isolated', 'backing_git'):
        row = store.claim(set())
        assert row and row['mode'] == expected_mode
        assert row['origin_parent_id'] == parent
        await store.dispatch(row, Dispatcher())
    assert order == ['commit', 'edit', 'commit', 'push']
    assert len(set(paths)) == 4
    assert all(row['state'] == 'successful' for row in store.inspect(token))
    assert store.claim(set()) is None
    assert git('rev-list', '--count', 'HEAD') == '3'
    assert git('show', 'HEAD:app.txt') == 'second'
    assert git('show', 'HEAD~1:app.txt') == 'first'
    assert git('status', '--porcelain') == ''
    assert git('--git-dir', str(remote), 'rev-parse', 'main') == git('rev-parse', 'HEAD')


@pytest.mark.asyncio
async def test_sequence_launch_provides_outcome_credential_only(setup):
    parent, token = setup
    store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    watch_launch.configure(None, 'http://127.0.0.1:1234')
    observed = []
    async def runner(prompt, **kwargs):
        assert prompt == 'Execute predeclared step'
        credential = kwargs['backend_env']['SQUID_CONTINUE_TOKEN']
        observed.append(credential)
        with pytest.raises(ValueError, match='parent_not_running'):
            store.create(credential, SEQUENCE)
        store.result(credential, {'success': True})
        yield 'Completed'
    assert [x async for x in watch_launch.run(runner, 'Execute predeclared step', topic='test', msg_id=child)] == ['Completed']
    with pytest.raises(PermissionError):
        store.result(observed[0], {'success': True})


def test_sequence_schema_migration_keeps_legacy_single_step_behavior(setup):
    parent, token = setup
    row = store.create(token, BODY)
    with db._connection() as conn:
        for column in ('origin_parent_id', 'remaining_json', 'request_json', 'requires_result', 'result_success'):
            conn.execute(f'ALTER TABLE continuations DROP COLUMN {column}')
    store.initialize()
    store.initialize()
    assert store.create(token, BODY)['id'] == row['id']
    _, child, credential = _start_step(parent, token)
    assert credential is None
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    assert store.inspect(token)[0]['state'] == 'successful'
    assert store.claim(set()) is None


def test_sequence_result_cli_uses_post_endpoint(monkeypatch, tmp_path):
    from agent import continuation_cli
    calls = []
    original_client = httpx.Client
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'success': True})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setenv('SQUID_API_URL', 'http://127.0.0.1:1234')
    monkeypatch.setenv('SQUID_CONTINUE_TOKEN', 'step-secret')
    payload = tmp_path / 'result.json'
    payload.write_text('{"success":true}')
    assert continuation_cli.main(['result', '--input', str(payload)]) == 0
    assert calls[0].method == 'POST' and calls[0].url.path == '/continuations/result'
    assert calls[0].content == b'{"success":true}'


@pytest.mark.asyncio
@pytest.mark.parametrize('native_session,resume_failure', [
    (False, None), (True, None), (True, 'No conversation found'), (True, 'prompt is too long'),
])
async def test_adhoc_sequence_has_private_context_and_preserves_regular_session(setup, monkeypatch, tmp_path, native_session, resume_failure):
    from types import SimpleNamespace
    from unittest.mock import patch
    from agent import server
    from agent.topic_queue import QueueItem, TopicWorker

    parent, token = setup
    with db._connection() as conn:
        conn.execute('UPDATE chat_messages SET adhoc=1 WHERE id=?', (parent,))
    db.set_topic_session('test', 'codex', 'regular-session', str(tmp_path), 'regular-runtime')
    regular = db.get_topic_session('test', 'codex')
    unrelated_user = db.insert_user_message('test', 'codex', 'Unrelated conversation')
    unrelated = db.insert_assistant_message('test', 'codex', unrelated_user)
    db.update_assistant_message(unrelated, 'Unrelated answer', 'regular-session', 'done')
    store.create(token, SEQUENCE)
    island_sid = 'private-session' if native_session else None
    db.update_assistant_message(parent, 'First edit decision: keep the blue button', island_sid, 'done')
    store.complete(parent, True, 'synced')
    seen = []

    monkeypatch.setattr(server, 'get_agent', lambda name: {'model': 'test-model'})
    monkeypatch.setattr(server, '_resolve_agent_runtime', lambda cfg: (
        'codex', None, 'codex', SimpleNamespace(fingerprint='runtime')))
    monkeypatch.setattr(server, 'topic_memory_squid_config', lambda topic: {})

    class Dispatcher:
        async def dispatch(self, **kwargs):
            nonlocal island_sid
            assert kwargs['adhoc'] is True
            assert kwargs['resume_session_id'] == island_sid
            assert kwargs['cwd'] is None
            history = kwargs['context_history']
            assert len(history) == 2 * (len(seen) + 1)
            assert history[1]['content'] == 'First edit decision: keep the blue button'
            assert unrelated not in {m['id'] for m in history}
            child = kwargs['msg_id']
            assert db.get_message(child)['adhoc'] == 1
            item = QueueItem(seq=0, topic='test', agent='codex', prompt=kwargs['prompt'],
                             context_history=history, backend='codex', cwd=str(tmp_path),
                             configured_cwd=None, code_roots=[], adhoc=True,
                             resume_session_id=kwargs['resume_session_id'], msg_id=child,
                             refresh_session_at_start=True)
            attempts = 0
            async def runner(prompt, **runner_kwargs):
                nonlocal attempts, island_sid
                from agent.runners import CLIError
                attempts += 1
                assert runner_kwargs.get('resume_session_id') == (island_sid if attempts == 1 else None)
                if resume_failure and not seen and attempts == 1:
                    raise CLIError(resume_failure)
                if attempts > 1:
                    island_sid = 'replacement-private-session'
                assert runner_kwargs['history'] == history
                assert runner_kwargs['adhoc'] is True
                credential = store.mint(child)
                store.result(credential, {'success': True})
                yield 'Completed private step ' + str(len(seen))
                if native_session:
                    yield {'_stats': {'session_id': island_sid}}
            with patch('agent.runners.run_codex', runner):
                await TopicWorker('test')._process(item)
            assert db.get_message(child)['status'] == 'done'
            assert db.get_topic_session('test', 'codex') == regular
            seen.append(child)
            return item.out_q, None, None

    for _ in range(3):
        row = store.claim(set())
        assert row
        await store.dispatch(row, Dispatcher())
    assert len(seen) == 3
    assert all(row['state'] == 'successful' for row in store.inspect(token))
    # The next ad hoc turn starts fresh; the regular route still has its old session.
    prepared = await server._prepare_chat_turn(message='Next request', topic='test', agent='codex', adhoc=True)
    assert prepared['resume_session_id'] is None
    assert prepared['context_history'] == []
    assert db.get_topic_session('test', 'codex') == regular
