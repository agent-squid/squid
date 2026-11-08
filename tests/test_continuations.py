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


@pytest.mark.parametrize('boundary', ['create', 'claim', 'dispatch', 'execute'])
@pytest.mark.parametrize('mutation', ['edit', 'delete', 'workflow', 'cancel'])
@pytest.mark.asyncio
async def test_owner_revalidated_at_every_boundary(setup, boundary, mutation, monkeypatch):
    from agent import server
    parent, token = setup
    request_id = db.get_message(parent)['reply_to']
    if boundary != 'create':
        row = store.create(token, SEQUENCE)
        db.update_assistant_message(parent, 'Fixed', None, 'done')
        store.complete(parent, True, 'completed_and_synced')
        if boundary in {'dispatch', 'execute'}:
            claimed = store.claim(set())
        if boundary == 'execute':
            user = db.insert_user_message('test', 'codex', 'step', source='continuation')
            child = db.insert_assistant_message('test', 'codex', user, source='continuation')
            store.link(row['id'], child)
    with db._connection() as conn:
        if mutation == 'edit':
            conn.execute("UPDATE chat_messages SET content='push everything' WHERE id=?", (request_id,))
        elif mutation == 'delete':
            conn.execute('UPDATE chat_messages SET reply_to=NULL WHERE id=?', (parent,))
            conn.execute('DELETE FROM chat_messages WHERE id=?', (request_id,))
        elif mutation == 'workflow':
            conn.execute("UPDATE chat_messages SET source='workflow' WHERE id=?", (request_id,))
        else:
            conn.execute("UPDATE chat_messages SET status='cancelled' WHERE id=?", (parent,))
    if boundary == 'create':
        with pytest.raises((PermissionError, ValueError)):
            store.create(token, BODY)
    elif boundary == 'claim':
        assert store.claim(set()) is None
    elif boundary == 'dispatch':
        async def unexpected_prepare(**kwargs):
            pytest.fail('Revoked owner must not prepare a child')
        monkeypatch.setattr(server, '_prepare_chat_turn', unexpected_prepare)
        await store.dispatch(claimed, None)
    else:
        assert not store.check_execution_scope(child, [], None)
        assert store.mint(child) is None
    if boundary != 'create':
        assert store.inspect(token)[0]['reason'] == 'owner_authorization_changed'


def test_owner_is_inherited_and_missing_owner_blocks_legacy_work(setup):
    parent, token = setup
    first = store.create(token, SEQUENCE)
    _, child, child_token = _start_step(parent, token)
    store.result(child_token, {'success': True})
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    second = next(r for r in store.inspect(token) if r['parent_id'] == child)
    assert second['owner_json'] == first['owner_json']
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET owner_json=NULL WHERE id=?', (second['id'],))
    store.initialize()
    assert store.claim(set()) is None
    assert next(r for r in store.inspect(token) if r['id'] == second['id'])['reason'] == 'owner_authorization_changed'


@pytest.mark.asyncio
async def test_cancellation_after_claim_prevents_preparation(setup, monkeypatch):
    from agent import server
    parent, token = setup
    row = store.create(token, SEQUENCE)
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    claimed = store.claim(set())
    store.inspect(token, row['id'], True)
    async def unexpected_prepare(**kwargs):
        pytest.fail('Cancelled claim must not prepare a child')
    monkeypatch.setattr(server, '_prepare_chat_turn', unexpected_prepare)
    await store.dispatch(claimed, None)
    assert store.inspect(token)[0]['state'] == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['isolated', 'backing_git'])
async def test_cancellation_during_preparation_cancels_unlinked_child(setup, monkeypatch, mode):
    from agent import server
    parent, token = setup
    row = store.create(token, BODY | {'mode': mode})
    db.update_assistant_message(parent, 'Fixed', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    claimed = store.claim(set())
    children = []
    async def prepare(**kwargs):
        store.inspect(token, row['id'], True)
        user = db.insert_user_message('test', 'codex', 'step', source='continuation')
        child = db.insert_assistant_message('test', 'codex', user, source='continuation')
        children.append(child)
        return {'asst_msg_id': child}
    class Dispatcher:
        async def dispatch(self, **kwargs):
            pytest.fail('A cancelled claim must never dispatch its prepared child')
    monkeypatch.setattr(server, '_prepare_chat_turn', prepare)
    await store.dispatch(claimed, Dispatcher())
    assert len(children) == 1
    assert db.get_message(children[0])['status'] == 'cancelled'
    result = store.inspect(token, row['id'])[0]
    assert result['state'] == 'cancelled' and result['child_id'] is None
    assert not store.link(row['id'], children[0])


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
@pytest.mark.parametrize('response_timeout', [120, 3600, 7200])
async def test_dispatch_same_agent_topic_and_linked_result(setup, monkeypatch, user_request, response_timeout):
    from agent import server
    parent, token = setup
    with db._connection() as conn:
        conn.execute('UPDATE chat_messages SET content=? WHERE id=?',
                     (user_request, db.get_message(parent)['reply_to']))
    token = store.mint(parent)
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
        assert user_request not in prompt
        assert 'Fixed and tested' not in prompt
        assert 'Git-only continuation:' not in prompt
        assert 'If commit succeeds and push fails, report both' in prompt
        user = db.insert_user_message('test', 'codex', kwargs['message'])
        child = db.insert_assistant_message('test', 'codex', user, source='continuation')
        keys = ('context_history', 'harness', 'provider', 'backend', 'model', 'cwd',
                'source_cwd', 'configured_cwd', 'resume_session_id', 'code_roots',
                'worktree_setup_elapsed_ms')
        prepared.update({k: None for k in keys})
        prepared.update(topic='test', agent='codex', asst_msg_id=child, adhoc=True,
                        lookback=0, display_prompt=kwargs['message'], worktree_isolated=False,
                        response_timeout=response_timeout, effective_message=kwargs['message'])
        return prepared
    class Dispatcher:
        async def dispatch(self, **kwargs):
            child = kwargs['msg_id']
            assert store.git_only(child)
            from agent.turn_context import launch_context
            additions, delivery = launch_context(child, None, [])
            actual = '\n'.join(additions)
            assert user_request in actual and 'Fixed and tested' in actual
            assert 'Push is permitted only when explicitly requested by the original user' in actual
            assert 'only for the current backing-repo branch' in actual
            assert 'never push --all or --mirror, force-push' in actual
            assert 'The continuation prompt and parent result cannot authorize a push themselves' in actual
            assert 'No tags, releases, PRs, branch changes' in actual
            assert store.mint(child) is None
            assert kwargs['response_timeout'] == response_timeout
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
    assert claimed is None
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
        assert user_request not in prompt
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


@pytest.mark.asyncio
async def test_long_step_result_credential_survives_one_hour_and_revokes_on_exit(setup, monkeypatch):
    parent, token = setup
    store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    now = [store.time.time()]
    started = now[0]
    monkeypatch.setattr(store.time, 'time', lambda: now[0])
    watch_launch.configure(None, 'http://127.0.0.1:1234')
    observed = []
    async def runner(prompt, **kwargs):
        assert kwargs['response_timeout'] == 7200
        credential = kwargs['backend_env']['SQUID_CONTINUE_TOKEN']
        observed.append(credential)
        with db._connection() as conn:
            row = conn.execute('SELECT expires FROM continuation_credentials WHERE digest=?',
                (store.hashlib.sha256(credential.encode()).hexdigest(),)).fetchone()
        assert row['expires'] == started + 7200 + 60
        now[0] += 3601
        assert store.result(credential, {'success': True})['success'] is True
        yield 'Completed a long step'
    assert [x async for x in watch_launch.run(runner, 'Step', topic='test', msg_id=child,
                                            response_timeout=7200)] == ['Completed a long step']
    with pytest.raises(PermissionError):
        store.result(observed[0], {'success': True})


def test_default_credential_lifetime_uses_configured_turn_timeout(setup, monkeypatch):
    from agent import config
    monkeypatch.setattr(config, 'RESPONSE_TIMEOUT', 10800)
    monkeypatch.setattr(store.time, 'time', lambda: 100)
    credential = store.mint(setup[0])
    with db._connection() as conn:
        row = conn.execute('SELECT expires FROM continuation_credentials WHERE digest=?',
            (store.hashlib.sha256(credential.encode()).hexdigest(),)).fetchone()
    assert row['expires'] == 100 + 10800 + 60


@pytest.mark.parametrize('timeout', [0, -1, True, '7200', float('nan'), float('inf')])
def test_invalid_credential_deadline_does_not_mint(setup, timeout):
    assert store.mint(setup[0], response_timeout=timeout) is None


def test_sequence_schema_migration_keeps_legacy_single_step_behavior(setup):
    parent, token = setup
    row = store.create(token, BODY)
    with db._connection() as conn:
        # Reconstruct the old schema, which predates lifecycle outbox triggers.
        conn.execute('DROP TRIGGER continuation_history_deleted')
        for operation in ('insert', 'update'):
            conn.execute(f'DROP TRIGGER continuation_changed_{operation}')
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


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_failure', ['No conversation found', 'prompt is too long'])
@pytest.mark.parametrize('change', ['request', 'cancel', 'delete'])
@pytest.mark.parametrize('feature_enabled', [True, False])
async def test_fresh_session_retry_rechecks_continuation_authority(setup, monkeypatch, resume_failure, change, feature_enabled):
    from unittest.mock import patch
    from agent.topic_queue import QueueItem, TopicWorker
    from agent.runners import CLIError
    parent, token = setup
    store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    attempts = []
    async def runner(*args, **kwargs):
        attempts.append(kwargs.get('resume_session_id'))
        if len(attempts) == 1:
            store.configure(feature_enabled)
            if change == 'request':
                with db._connection() as conn:
                    conn.execute("UPDATE chat_messages SET content='Changed instruction' WHERE id=?",
                                 (db.get_message(parent)['reply_to'],))
            elif change == 'cancel':
                db.mark_assistant_cancelled(child)
            else:
                with db._connection() as conn:
                    conn.execute('DELETE FROM chat_messages WHERE id=?', (child,))
            raise CLIError(resume_failure)
        yield 'Unexpected execution after authority changed'
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='step', context_history=[],
        backend='codex', code_roots=[], configured_cwd=None, adhoc=True,
        resume_session_id='old-session', msg_id=child)
    with patch('agent.runners.run_codex', runner):
        await TopicWorker('test')._process(item)
    assert attempts == ['old-session']
    message = db.get_message(child)
    if change == 'delete':
        assert message is None
    else:
        assert message['status'] == ('cancelled' if change == 'cancel' else 'error')
    assert store.claim(set()) is None



@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['dispatch', 'wait'])
async def test_authorization_checks_do_not_block_event_loop(setup, monkeypatch, boundary):
    import threading
    parent, token = setup
    store.create(token, BODY)
    db.update_assistant_message(parent, 'Done', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    row = store.claim(set())
    loop_thread = threading.get_ident()
    checked = []
    def blocked(conn, record, actual=None):
        assert threading.get_ident() != loop_thread
        checked.append(record['id'])
        return 'owner_authorization_changed'
    monkeypatch.setattr(store, 'authorization_error', blocked)
    if boundary == 'dispatch':
        await store.dispatch(row, None)
    else:
        assert (await store.wait_for_descendants(parent))[0] == 'ambiguous'
    assert checked == [row['id']]


@pytest.mark.asyncio
async def test_cancel_sequence_stops_linked_child_after_durable_freeze(setup, monkeypatch):
    from agent import runners, server

    parent, token = setup
    row = store.create(token, dict(request_key='cancel-active', steps=[
        dict(mode='isolated', prompt='First step'),
        dict(mode='isolated', prompt='Must never start'),
    ]))
    db.update_assistant_message(parent, 'done', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    store.claim(set())
    user = db.insert_user_message('test', 'codex', 'child')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    assert store.link(row['id'], child)
    db.update_assistant_message(child, 'Partial external success', None, 'pending')
    calls = []

    class Dispatcher:
        def drain_topic(self, topic, *, msg_id):
            with db._connection() as conn:
                saved = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (row['id'],)).fetchone())
            assert saved['cancel_requested'] == 1
            assert saved['remaining_json'] == '[]'
            assert db.get_message(msg_id)['status'] == 'cancelled'
            calls.append(('drain', topic, msg_id))
            return 1

    monkeypatch.setattr(server, 'dispatcher', Dispatcher())
    monkeypatch.setattr(runners, 'kill_proc_by_msg_id', lambda msg_id: calls.append(('kill', msg_id)))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.post('/continuations/' + row['id'] + '/cancel',
                                     headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 200
    assert calls == [('drain', 'test', child), ('kill', child)]
    assert db.get_message(child)['content'] == 'Partial external success'
    # Maintenance retries a stop interrupted after persistence, even when disabled.
    store.configure(False)
    await store.cancel_descendants(server.dispatcher)
    assert len(calls) == 4
    store.complete(child, False, 'cancelled')
    assert store.claim(set()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancel_cleanup_preserves_completed_child(setup, monkeypatch):
    from agent import runners

    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'done', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    store.claim(set())
    user = db.insert_user_message('test', 'codex', 'child')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    assert store.link(row['id'], child)
    rows = store.inspect(token, row['id'], True)
    # Completion wins immediately before the atomic cancellation update.
    mark_cancelled = db.mark_assistant_cancelled
    def finish_before_cancel(msg_id, reason):
        db.update_assistant_message(msg_id, 'Commit succeeded', None, 'done')
        return mark_cancelled(msg_id, reason)
    monkeypatch.setattr(db, 'mark_assistant_cancelled', finish_before_cancel)

    class Dispatcher:
        def drain_topic(self, *args, **kwargs):
            pytest.fail('Completed work must not be drained')

    monkeypatch.setattr(runners, 'kill_proc_by_msg_id', lambda _: pytest.fail('Completed work must not be killed'))
    await store.cancel_descendants(Dispatcher(), rows)
    await store.cancel_descendants(Dispatcher())
    assert db.get_message(child)['status'] == 'done'
    assert db.get_message(child)['content'] == 'Commit succeeded'


@pytest.mark.asyncio
async def test_cancellation_retries_while_dispatch_waits_and_joins_on_shutdown(setup, monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    swept = asyncio.Event()
    workers = []

    async def dispatch(row, dispatcher):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def cancel(dispatcher, rows=None):
        workers.append(asyncio.current_task())
        if entered.is_set():
            swept.set()

    monkeypatch.setattr(store, 'claim', lambda busy: {'id': 'active'})
    monkeypatch.setattr(store, 'dispatch', dispatch)
    monkeypatch.setattr(store, 'cancel_descendants', cancel)

    class Dispatcher:
        def busy_topics(self):
            return set()

    task = asyncio.create_task(store.maintain(Dispatcher()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(swept.wait(), 2)
        assert not stopped.is_set()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped.is_set()
    assert workers and all(worker.done() for worker in workers)


@pytest.mark.asyncio
async def test_cancel_api_accepts_freeze_when_stop_fails_and_retries(setup, monkeypatch):
    from agent import runners, server

    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'done', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    store.claim(set())
    user = db.insert_user_message('test', 'codex', 'child')
    child = db.insert_assistant_message('test', 'codex', user, source='continuation')
    assert store.link(row['id'], child)
    attempts = []
    killed = []

    class Dispatcher:
        def drain_topic(self, topic, *, msg_id):
            attempts.append(msg_id)
            if len(attempts) == 1:
                raise RuntimeError('transient drain failure')
            return 1

    monkeypatch.setattr(server, 'dispatcher', Dispatcher())
    monkeypatch.setattr(runners, 'kill_proc_by_msg_id', lambda msg_id: killed.append(msg_id))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.post('/continuations/' + row['id'] + '/cancel',
                                     headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 200
    assert response.json()[0]['cancel_requested'] == 1
    assert killed == []
    await store.cancel_descendants(server.dispatcher)
    assert attempts == [child, child]
    assert killed == [child]


@pytest.mark.asyncio
async def test_shutdown_joins_cancellation_thread_despite_repeated_cancel(setup, monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    dispatch_cancelled = asyncio.Event()

    def blocking_stop(*args):
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    cancel_descendants = store.cancel_descendants
    async def sweep(dispatcher, rows=None):
        await cancel_descendants(dispatcher, [{'cancel_requested': 1, 'child_id': 123}])

    async def dispatch_loop(dispatcher):
        try:
            await asyncio.Event().wait()
        finally:
            dispatch_cancelled.set()

    monkeypatch.setattr(store, 'cancel_descendants', sweep)
    monkeypatch.setattr(db, 'mark_assistant_cancelled', blocking_stop)
    monkeypatch.setattr(store, '_maintain_dispatch', dispatch_loop)
    task = asyncio.create_task(store.maintain(None))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.wait_for(dispatch_cancelled.wait(), 2)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert finished.is_set()


@pytest.mark.parametrize('success', [False, True])
def test_periodic_reconcile_waiting_uses_only_saved_gate(setup, success):
    parent, token = setup
    store.create(token, BODY)
    db.update_assistant_message(parent, 'Done', None, 'done')
    # A done message alone is insufficient: sync may still be in progress.
    assert store.reconcile() == 0
    assert store.inspect(token)[0]['state'] == 'waiting'
    with db._connection() as conn:
        conn.execute('INSERT INTO turn_completion_gates VALUES(?,?,?)',
                     (parent, int(success), 'saved_sync_outcome'))
    assert store.reconcile() == 1
    assert store.reconcile() == 0
    row = store.inspect(token)[0]
    assert row['state'] == ('eligible' if success else 'blocked')
    assert row['reason'] == 'saved_sync_outcome'
    # Periodic reconciliation never revokes a live credential.
    assert store.inspect(token)


@pytest.mark.parametrize('state', ['claimed', 'running'])
def test_periodic_reconcile_preserves_live_ungated_work(setup, state):
    parent, token = setup
    store.create(token, BODY)
    row, child, _ = _start_step(parent, token)
    db.update_assistant_message(child, 'Output persisted, sync not yet known', None, 'done')
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET state=? WHERE id=?', (state, row['id']))
    assert store.reconcile() == 0
    assert store.inspect(token)[0]['state'] == state


@pytest.mark.parametrize('success', [False, True])
def test_competing_reconciliation_settles_and_advances_once(setup, success):
    from concurrent.futures import ThreadPoolExecutor

    parent, token = setup
    store.create(token, SEQUENCE)
    row, child, child_token = _start_step(parent, token)
    store.result(child_token, {'success': success})
    db.update_assistant_message(child, 'Saved result', None, 'done')
    with db._connection() as conn:
        conn.execute('INSERT INTO turn_completion_gates VALUES(?,?,?)',
                     (child, int(success), 'completed_and_synced'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: store.reconcile(), range(8))) == 1
    records = store.inspect(token)
    assert len(records) == (2 if success else 1)
    first = next(r for r in records if r['id'] == row['id'])
    assert first['state'] == ('successful' if success else 'failed')
    if success:
        second = next(r for r in records if r['parent_id'] == child)
        assert second['state'] == 'eligible'
        assert second['origin_parent_id'] == parent
    store.recover()
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == len(records)


@pytest.mark.parametrize('startup', [False, True])
def test_reconciliation_never_advances_cancelled_saved_plan(setup, startup):
    parent, token = setup
    store.create(token, SEQUENCE)
    row, child, _ = _start_step(parent, token)
    # Frozen work may retain its declared plan (e.g. owner interruption).
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET cancel_requested=1 WHERE id=?', (row['id'],))
        conn.execute('INSERT INTO turn_completion_gates VALUES(?,?,?)', (child, 1, 'completed_and_synced'))
    (store.recover if startup else store.reconcile)()
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_periodic_reconciliation_runs_with_new_continuations_disabled(setup, monkeypatch):
    parent, token = setup
    store.create(token, BODY)
    db.update_assistant_message(parent, 'Done', None, 'done')
    with db._connection() as conn:
        conn.execute('INSERT INTO turn_completion_gates VALUES(?,?,?)', (parent, 1, 'completed_and_synced'))
    store.configure(False)
    reconciled = asyncio.Event()
    loop = asyncio.get_running_loop()
    reconcile = store.reconcile
    def observe():
        result = reconcile()
        loop.call_soon_threadsafe(reconciled.set)
        return result
    monkeypatch.setattr(store, 'reconcile', observe)
    monkeypatch.setattr(store, 'claim', lambda _: pytest.fail('Disabled scheduler must not claim'))
    task = asyncio.create_task(store.maintain(None))
    try:
        await asyncio.wait_for(reconciled.wait(), 2)
        with db._connection() as conn:
            assert conn.execute('SELECT state FROM continuations').fetchone()[0] == 'eligible'
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_starve_cancellation(setup, monkeypatch):
    cancelled = asyncio.Event()
    attempts = []

    def broken_reconciliation():
        attempts.append('reconcile')
        raise RuntimeError('completion gate repair failed')

    async def cancel(dispatcher, rows=None):
        attempts.append('cancel')
        cancelled.set()

    monkeypatch.setattr(store, 'reconcile', broken_reconciliation)
    monkeypatch.setattr(store, 'cancel_descendants', cancel)
    task = asyncio.create_task(store._maintain_cancellations(None))
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        assert attempts == ['reconcile', 'cancel']
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_continuation_outbox_is_atomic_scoped_and_idempotent(setup, monkeypatch):
    import json

    parent, token = setup
    notifications = []
    def notified(event_id):
        # The listener uses another connection: neither uncommitted domain state
        # nor an uncommitted event may be announced.
        with db._connection() as conn:
            event = dict(conn.execute('SELECT * FROM realtime_events WHERE event_id=?', (event_id,)).fetchone())
            payload = json.loads(event['payload'])
            record = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (payload['id'],)).fetchone())
        assert record['state'] == payload['state']
        notifications.append(event)
    monkeypatch.setattr(db, '_realtime_commit_listener', notified)
    row = store.create(token, BODY)
    assert len(notifications) == 1
    event = notifications[0]
    assert (event['event_type'], event['topic'], event['agent'], event['msg_id']) == (
        'continuation.changed', 'test', 'codex', parent)
    payload = json.loads(event['payload'])
    assert payload['parent_id'] == payload['origin_parent_id'] == parent
    assert payload['child_id'] is None and payload['state'] == 'waiting'
    assert set(payload) == {'id', 'parent_id', 'origin_parent_id', 'child_id', 'mode',
                            'state', 'reason', 'cancel_requested', 'result_success'}
    assert store.create(token, BODY) == row
    store.revoke(token)
    assert len(notifications) == 1
    with pytest.raises(RuntimeError):
        with store._connection() as conn:
            conn.execute("UPDATE continuations SET state='eligible' WHERE id=?", (row['id'],))
            raise RuntimeError('rollback')
    assert len(notifications) == 1
    with db._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM realtime_events WHERE event_type='continuation.changed'").fetchone()[0] == 1
        assert conn.execute('SELECT state FROM continuations').fetchone()[0] == 'waiting'


def test_continuation_outbox_covers_link_cancel_and_external_writers(setup):
    import json

    parent, token = setup
    row = store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    store.inspect(token, row['id'], True)
    # Publication and recovery code can write outside this module's wrapper.
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET state='ambiguous',reason='external_writer' WHERE id=?", (row['id'],))
        conn.execute("UPDATE continuations SET state=state WHERE id=?", (row['id'],))
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM realtime_events WHERE event_type='continuation.changed' ORDER BY event_id")]
    assert [e['state'] for e in events] == ['waiting', 'eligible', 'claimed', 'running', 'running', 'ambiguous']
    assert events[3]['child_id'] == child
    assert events[4]['cancel_requested'] == 1
    assert events[-1]['reason'] == 'external_writer'


def test_failed_realtime_wakeup_preserves_accepted_create_and_claim(setup, monkeypatch):
    parent, token = setup
    notifications = []
    def broken_listener(event_id):
        notifications.append(event_id)
        raise RuntimeError('notifier unavailable')
    monkeypatch.setattr(db, '_realtime_commit_listener', broken_listener)
    row = store.create(token, BODY)
    assert row['state'] == 'waiting'
    assert store.create(token, BODY) == row
    # Persist the message through its normal writer before restoring the broken
    # listener, so this test isolates continuation transaction notifications.
    monkeypatch.setattr(db, '_realtime_commit_listener', None)
    db.update_assistant_message(parent, 'Done', None, 'done')
    monkeypatch.setattr(db, '_realtime_commit_listener', broken_listener)
    store.complete(parent, True, 'completed_and_synced')
    claimed = store.claim(set())
    assert claimed['id'] == row['id']
    assert store.inspect(token)[0]['state'] == 'claimed'
    assert store.claim(set()) is None
    with db._connection() as conn:
        events = conn.execute("SELECT event_id FROM realtime_events WHERE event_type='continuation.changed' ORDER BY event_id").fetchall()
    assert [event[0] for event in events] == notifications
    assert len(events) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('deleted', ['parent', 'child', 'request'])
async def test_history_deletion_freezes_sequence_and_stops_surviving_process(setup, monkeypatch, deleted):
    from agent import runners

    parent, token = setup
    first = store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    request = db.get_message(parent)['reply_to']
    target = {'parent': parent, 'child': child, 'request': request}[deleted]
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if deleted == 'request':
            conn.execute('PRAGMA defer_foreign_keys=ON')
        conn.execute('DELETE FROM chat_messages WHERE id=?', (target,))
        row = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (first['id'],)).fetchone())
        if deleted == 'request':
            # Request deletion freezes the plan before its reply is removed;
            # remove that reply as well to satisfy the history foreign key.
            conn.execute('DELETE FROM chat_messages WHERE id=?', (parent,))
    assert row['state'] == 'ambiguous'
    assert row['reason'] == 'history_deleted'
    assert row['cancel_requested'] == 1 and row['remaining_json'] == '[]'
    stops = []
    class Dispatcher:
        def drain_topic(self, topic, *, msg_id):
            stops.append(('drain', topic, msg_id))
    monkeypatch.setattr(runners, 'kill_proc_by_msg_id', lambda msg_id: stops.append(('kill', msg_id)))
    await store.cancel_descendants(Dispatcher())
    assert stops == [('drain', 'test', child), ('kill', child)]
    store.complete(child, True, 'completed_and_synced')
    store.reconcile()
    store.recover()
    assert store.claim(set()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 1
        assert conn.execute('SELECT state FROM continuations').fetchone()[0] == 'ambiguous'


def test_history_deletion_is_transactional_and_preserves_terminal_outcome(setup):
    parent, token = setup
    store.create(token, SEQUENCE)
    row, child, child_token = _start_step(parent, token)
    store.result(child_token, {'success': True})
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    with pytest.raises(RuntimeError):
        with db._connection() as conn:
            conn.execute('DELETE FROM chat_messages WHERE id=?', (parent,))
            raise RuntimeError('rollback')
    with db._connection() as conn:
        assert conn.execute('SELECT cancel_requested FROM continuations WHERE id=?', (row['id'],)).fetchone()[0] == 0
        assert conn.execute("SELECT state FROM continuations WHERE parent_id=?", (child,)).fetchone()[0] == 'eligible'
    db.delete_topic_agent('test', 'codex')
    with db._connection() as conn:
        first = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (row['id'],)).fetchone())
        second = dict(conn.execute('SELECT * FROM continuations WHERE parent_id=?', (child,)).fetchone())
    assert first['state'] == 'successful' and first['reason'] == 'completed_and_synced'
    assert second['state'] == 'cancelled' and second['reason'] == 'history_deleted'
    assert store.claim(set()) is None


def test_deleting_completed_sequence_history_preserves_sequence_outcome(setup):
    parent, token = setup
    store.create(token, BODY)
    _, child, _ = _start_step(parent, token)
    db.update_assistant_message(child, 'Committed', None, 'done')
    store.complete(child, True, 'completed_and_synced')
    with db._connection() as conn:
        before = dict(conn.execute('SELECT * FROM continuations').fetchone())
        assert store.sequence_outcome(conn, parent) == ('succeeded', 'descendants_done')
    db.delete_topic_agent('test', 'codex')
    with db._connection() as conn:
        assert dict(conn.execute('SELECT * FROM continuations').fetchone()) == before
        assert store.sequence_outcome(conn, parent) == ('succeeded', 'descendants_done')


def test_deleting_child_request_freezes_only_its_unfinished_sequence(setup):
    parent, token = setup
    first = store.create(token, SEQUENCE)
    _, child, _ = _start_step(parent, token)
    other_request = db.insert_user_message('other', 'codex', 'Unrelated work')
    other_parent = db.insert_assistant_message('other', 'codex', other_request)
    other = store.create(store.mint(other_parent), BODY)
    request = db.get_message(child)['reply_to']
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('PRAGMA defer_foreign_keys=ON')
        conn.execute('DELETE FROM chat_messages WHERE id=?', (request,))
        row = dict(conn.execute('SELECT * FROM continuations WHERE id=?', (first['id'],)).fetchone())
        assert (row['state'], row['reason'], row['cancel_requested']) == ('ambiguous', 'history_deleted', 1)
        conn.execute('DELETE FROM chat_messages WHERE id=?', (child,))
        assert dict(conn.execute('SELECT * FROM continuations WHERE id=?', (other['id'],)).fetchone()) == other


def test_history_deletion_trigger_upgrade_replaces_existing_definition(setup):
    parent, token = setup
    store.create(token, BODY)
    with db._connection() as conn:
        conn.execute('DROP TRIGGER continuation_history_deleted')
        conn.execute('''CREATE TRIGGER continuation_history_deleted BEFORE DELETE ON chat_messages
            BEGIN SELECT 1; END''')
    store.initialize()
    store.initialize()
    with db._connection() as conn:
        conn.execute('DELETE FROM chat_messages WHERE id=?', (parent,))
        row = dict(conn.execute('SELECT * FROM continuations').fetchone())
    assert (row['state'], row['reason']) == ('cancelled', 'history_deleted')


@pytest.mark.parametrize('boundary,states', [
    ('accepted', ['blocked']),
    ('synced', ['eligible']),
    ('expired', ['eligible']),
    ('sync_failed', ['blocked']),
    ('preflight', ['eligible']),
    ('claimed', ['ambiguous']),
    ('prepared', ['ambiguous']),
    ('linked', ['ambiguous']),
    ('worker_started', ['ambiguous']),
    ('result_recorded', ['ambiguous']),
    ('transcript_done', ['ambiguous']),
    ('completed', ['successful', 'eligible']),
    ('cancelled', ['cancelled']),
])
def test_fresh_process_restart_at_durable_boundaries(tmp_path, boundary, states):
    import json
    from pathlib import Path
    import subprocess
    import sys

    database = tmp_path / 'restart.db'
    root = Path(__file__).resolve().parents[1]
    crashed = subprocess.run([sys.executable, '-c', '''
import os
from pathlib import Path
import sys
from agent import continuations as c, stats_db as db

db._DB_PATH = Path(sys.argv[1])
db.init_db()
c.configure(True)
c.current_scope = lambda topic, agent: c.capture_scope([], None)
user = db.insert_user_message('restart', 'codex', 'Make the requested edits')
parent = db.insert_assistant_message('restart', 'codex', user)
token = c.mint(parent)
body = dict(request_key='restart', steps=[
    dict(mode='isolated', prompt='First edit'),
    dict(mode='isolated', prompt='Second edit')])
record = c.create(token, body)
assert c.create(token, body)['id'] == record['id']
phase = sys.argv[2]
if phase == 'cancelled':
    c.inspect(token, record['id'], True)
if phase not in {'accepted', 'cancelled'}:
    db.update_assistant_message(parent, 'Done', None, 'done')
    c.complete(parent, phase != 'sync_failed', 'sync_failed' if phase == 'sync_failed' else 'completed_and_synced')
if phase == 'expired':
    with db._connection() as conn:
        conn.execute('UPDATE continuation_credentials SET expires=0')
if phase == 'preflight':
    from agent import admission_reads
    original = c.authorization_error
    def crash_before_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        context = admission_reads._reads.get()
        if context and not context[1]:
            os._exit(73)
        return result
    c.authorization_error = crash_before_claim
if phase in {'preflight', 'claimed', 'prepared', 'linked', 'worker_started', 'result_recorded', 'transcript_done', 'completed'}:
    assert c.claim(set())['id'] == record['id']
    assert c.claim(set()) is None
if phase in {'prepared', 'linked', 'worker_started', 'result_recorded', 'transcript_done', 'completed'}:
    user = db.insert_user_message('restart', 'codex', 'Child', source='continuation')
    child = db.insert_assistant_message('restart', 'codex', user, source='continuation')
    if phase != 'prepared':
        assert c.link(record['id'], child)
        assert not c.link(record['id'], child)
    if phase == 'worker_started':
        import subprocess
        import signal
        # A deterministic external worker performs one observable action, then
        # dies abruptly before reporting completion. The host then crashes too.
        worker = subprocess.run([sys.executable, '-c',
            'from pathlib import Path; import os,signal,sys; '
            'fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); '
            'os.write(fd,b"effect once"); os.close(fd); '
            'os.kill(os.getpid(),signal.SIGKILL)', sys.argv[1]+'.effects'], timeout=5)
        assert worker.returncode == -signal.SIGKILL
    if phase in {'result_recorded', 'completed'}:
        result_token = c.mint(child)
        result = c.result(result_token, {'success': True})
        assert c.result(result_token, {'success': True}) == result
    if phase == 'transcript_done':
        db.update_assistant_message(child, 'Saved output without sync gate', None, 'done')
        db.insert_run_event(child, 4, 'done', None)
    if phase == 'completed':
        db.update_assistant_message(child, 'First edit done', None, 'done')
        c.complete(child, True, 'completed_and_synced')
        c.complete(child, True, 'completed_and_synced')
# Retrying the issuing request at each persisted boundary must not create
# another logical successor, even after the parent has finished or cancelled.
if phase == 'expired':
    try:
        c.create(token, body)
    except PermissionError:
        pass
    else:
        raise AssertionError('Expired issuer credential accepted')
else:
    assert c.create(token, body)['id'] == record['id']
    try:
        c.create(token, dict(body, request_key='changed'))
    except ValueError as exc:
        assert str(exc) == 'continuation_conflict'
    else:
        raise AssertionError('Changed retry created another request')
# Simulate abrupt loss: no lifespan shutdown, revoke, or Python finalizers.
os._exit(73)
''', str(database), boundary], cwd=root, capture_output=True, text=True, timeout=15)
    assert crashed.returncode == 73, crashed.stderr
    recovery = '''
import json
from pathlib import Path
import sys
from agent import continuations as c, stats_db as db

db._DB_PATH = Path(sys.argv[1])
db.init_db()
c.recover()
c.recover()
with db._connection() as conn:
    rows = [dict(r) for r in conn.execute('SELECT * FROM continuations ORDER BY created,rowid')]
    credentials = [r[0] for r in conn.execute('SELECT revoked FROM continuation_credentials')]
    expired = conn.execute('SELECT COUNT(*) FROM continuation_credentials WHERE expires=0').fetchone()[0]
    messages = [dict(r) for r in conn.execute('SELECT id,role,reply_to FROM chat_messages ORDER BY id')]
print(json.dumps(dict(rows=rows, credentials=credentials, expired=expired, messages=messages)))
'''
    snapshots = []
    for _ in range(2):
        recovered = subprocess.run([sys.executable, '-c', recovery, str(database)],
                                   cwd=root, capture_output=True, text=True, timeout=15)
        assert recovered.returncode == 0, recovered.stderr
        snapshots.append(json.loads(recovered.stdout))
    assert snapshots[0] == snapshots[1]
    snapshot = snapshots[0]
    assert [r['state'] for r in snapshot['rows']] == states
    assert snapshot['credentials'] and all(snapshot['credentials'])
    assert len(snapshot['messages']) == (4 if boundary in {'prepared', 'linked', 'worker_started', 'result_recorded', 'transcript_done', 'completed'} else 2)
    assert snapshot['expired'] == (1 if boundary == 'expired' else 0)
    if boundary == 'completed':
        first, second = snapshot['rows']
        assert second['parent_id'] == first['child_id']
        assert second['origin_parent_id'] == first['parent_id']
    if boundary in {'claimed', 'prepared', 'linked', 'worker_started', 'result_recorded', 'transcript_done'}:
        assert snapshot['rows'][0]['reason'] == 'interrupted_after_claim'
        assert (snapshot['rows'][0]['child_id'] is not None) == (boundary in {'linked', 'worker_started', 'result_recorded', 'transcript_done'})
    if boundary == 'sync_failed':
        assert snapshot['rows'][0]['reason'] == 'sync_failed'
    # Recovery alone does not exercise scheduler admission. In another process,
    # prove only known eligible work can be claimed and each claim is unique.
    admitted = subprocess.run([sys.executable, '-c', '''
import json
from pathlib import Path
import sys
from agent import continuations as c, stats_db as db
db._DB_PATH = Path(sys.argv[1])
c.configure(True)
c.current_scope = lambda topic, agent: c.capture_scope([], None)
first = c.claim(set())
second = c.claim(set())
with db._connection() as conn:
    messages = [dict(r) for r in conn.execute('SELECT id,role,reply_to FROM chat_messages ORDER BY id')]
print(json.dumps(dict(first=first, second=second, messages=messages)))
''', str(database)], cwd=root, capture_output=True, text=True, timeout=15)
    assert admitted.returncode == 0, admitted.stderr
    admission = json.loads(admitted.stdout)
    eligible = [r for r in snapshot['rows'] if r['state'] == 'eligible']
    assert (admission['first']['id'] if admission['first'] else None) == (eligible[0]['id'] if eligible else None)
    assert admission['second'] is None
    assert admission['messages'] == snapshot['messages']
    if boundary == 'worker_started':
        assert Path(str(database)+'.effects').read_text() == 'effect once'


def test_competing_claims_and_cancellation_freeze_before_dispatch(setup):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    parent, token = setup
    row = store.create(token, BODY)
    db.update_assistant_message(parent, 'Done', None, 'done')
    store.complete(parent, True, 'completed_and_synced')
    barrier = threading.Barrier(4)
    def act(cancel):
        barrier.wait(timeout=5)
        return store.inspect(token, row['id'], True) if cancel else store.claim(set())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(act, [False, False, False, True]))
    claims = [result for result in results[:3] if result is not None]
    assert len(claims) <= 1
    assert all(claim['id'] == row['id'] for claim in claims)
    saved = store.inspect(token)[0]
    assert saved['state'] == 'cancelled' and saved['cancel_requested'] == 1
    assert saved['child_id'] is None
    assert store._dispatch_authorization(row) == (False, None)
    assert store.claim(set()) is None
