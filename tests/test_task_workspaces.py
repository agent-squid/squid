"""Real Git coverage for persistent task destinations and branchless turns."""
import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse

from agent import task_workspaces as tasks, stats_db as db, worktree, server


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'test.db')
    db.init_db()
    monkeypatch.setattr(tasks, 'TASKS_HOME', tmp_path / 'tasks')
    monkeypatch.setattr(worktree, '_WORKTREES_HOME', tmp_path / 'turns')
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'file.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    db.upsert_agent('codex', 'codex', 'openai', None, cwd=str(repo))
    memory = lambda topic: {'code_roots': [str(repo)]}
    monkeypatch.setattr('agent.memory.topic_memory_squid_config', memory)
    monkeypatch.setattr(server, 'topic_memory_squid_config', memory)
    monkeypatch.setattr(server, 'WORKTREE_ISOLATION_ENABLED', True)
    monkeypatch.setattr('agent.config.WORKTREE_ISOLATION_ENABLED', True)
    user = db.insert_user_message('test', 'codex', 'Fix in a task workspace')
    task = tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='refs/heads/main', request_id=user)
    return repo, task


def turn(task):
    user = db.insert_user_message('test', 'codex', 'Edit this task')
    msg = db.insert_assistant_message('test', 'codex', user, adhoc=True)
    tasks.bind(msg, task['id'])
    root = Path(task['worktree_path'])
    isolated = worktree.ensure_worktree(root, 'test', str(msg))
    db.save_worktree('test', str(msg), str(root), str(isolated), '',
        worktree.base_commit_for(root, 'test', str(msg)), str(worktree.integration_worktree_path(root, 'test', str(msg))), 'active')
    return msg, root, isolated


def test_committed_base_excludes_dirty_backing_and_keeps_main_branch(setup):
    repo, task = setup
    (repo / 'file.txt').write_text('human dirty\n')
    (repo / 'human.txt').write_text('private\n')
    user = db.insert_user_message('test', 'codex', 'Another task')
    second = tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='main', request_id=user)
    assert (Path(second['worktree_path']) / 'file.txt').read_text() == 'base\n'
    assert not (Path(second['worktree_path']) / 'human.txt').exists()
    assert Path(second['worktree_path'], '.git').is_file()
    assert not second['branch'].startswith('sqd-')
    assert task['branch'] != second['branch']
    assert git(repo, 'symbolic-ref', '--short', 'HEAD') == 'main'
    assert git(repo, 'rev-parse', 'HEAD') == second['base_sha']
    assert tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='main', request_id=user)['id'] == second['id']


def test_sync_accumulates_only_in_task_and_followup_uses_dirty_task(setup, monkeypatch):
    repo, task = setup
    monkeypatch.setattr('agent.config.WORKTREE_TRACK_DIRTY_CHANGES', False)
    msg, root, isolated = turn(task)
    assert not (isolated / '.git').exists()
    (isolated / 'file.txt').write_text('task edit\n')
    (repo / 'file.txt').write_text('human edit\n')
    (repo / 'new.txt').write_text('human untracked\n')
    assert worktree.sync_after_turn(root, 'test', str(msg)) == []
    assert (root / 'file.txt').read_text() == 'task edit\n'
    assert (repo / 'file.txt').read_text() == 'human edit\n'
    assert not (root / 'new.txt').exists()
    db.mark_worktree_synced('test', str(msg), str(root))
    _, _, next_turn = turn(task)
    assert (next_turn / 'file.txt').read_text() == 'task edit\n'
    assert git(root, 'rev-parse', 'HEAD') == task['base_sha']
    db.init_db()  # Restart keeps the immutable binding and accumulated edits.
    assert tasks.for_turn(msg)['id'] == task['id']


@pytest.mark.parametrize('drift', ['missing', 'branch', 'scope', 'cwd', 'binding', 'row'])
def test_destination_drift_blocks_sync_and_keeps_turn(setup, monkeypatch, drift):
    repo, task = setup
    msg, root, isolated = turn(task)
    (isolated / 'file.txt').write_text('must remain quarantined\n')
    if drift == 'missing':
        root.rename(root.with_name(root.name + '-retained'))
    elif drift == 'branch':
        git(root, 'symbolic-ref', 'HEAD', 'refs/heads/wrong')
    elif drift == 'scope':
        monkeypatch.setattr('agent.memory.topic_memory_squid_config', lambda topic: {'code_roots': []})
    elif drift == 'cwd':
        db.upsert_agent('codex', 'codex', 'openai', None, cwd='/different')
    else:
        with db._connection() as conn:
            if drift == 'binding':
                conn.execute('DELETE FROM turn_task_workspaces WHERE msg_id=?', (msg,))
            else:
                conn.execute('DELETE FROM task_workspaces WHERE id=?', (task['id'],))
    with pytest.raises(RuntimeError):
        worktree.sync_after_turn(root, 'test', str(msg))
    assert isolated.exists()
    assert (repo / 'file.txt').read_text() == 'base\n'


def test_sync_cannot_use_backing_root_instead_of_saved_task(setup):
    repo, task = setup
    msg, root, isolated = turn(task)
    with pytest.raises(RuntimeError, match='destination mismatch'):
        worktree.sync_after_turn(repo, 'test', str(msg))
    with pytest.raises(RuntimeError, match='destination cannot change'):
        other_user = db.insert_user_message('test', 'codex', 'other')
        other = tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='main', request_id=other_user)
        tasks.bind(msg, other['id'])


def conflict(task):
    msg, root, isolated = turn(task)
    (isolated / 'file.txt').write_text('turn version\n')
    (root / 'file.txt').write_text('concurrent task version\n')
    assert worktree.sync_after_turn(root, 'test', str(msg)) == ['file.txt']
    db.mark_worktree_status('test', str(msg), str(root), 'conflict')
    integration = worktree.integration_worktree_path(root, 'test', str(msg))
    return msg, root, isolated, integration


def test_conflict_recovery_after_restart_promotes_only_task(setup):
    repo, task = setup
    msg, root, isolated, integration = conflict(task)
    db.init_db()
    (repo / 'file.txt').write_text('unrelated human edit\n')
    (integration / 'file.txt').write_text('resolved and reviewed\n')
    worktree.promote_resolved_integration_worktree(root, 'test', str(msg))
    assert (root / 'file.txt').read_text() == 'resolved and reviewed\n'
    assert (repo / 'file.txt').read_text() == 'unrelated human edit\n'
    assert tasks.get(task['id'])['review_required'] == 1
    assert root.exists()
    assert not isolated.exists()


@pytest.mark.asyncio
async def test_cleanup_retains_conflicted_task_and_resolution_blocks_missing_destination(setup, monkeypatch):
    _, task = setup
    msg, root, isolated, integration = conflict(task)
    monkeypatch.setattr(worktree, '_CLEANUP_GRACE_SECONDS', 0)
    await worktree.cleanup_worktrees('test')
    assert isolated.exists() and integration.exists()
    root.rename(root.with_name(root.name + '-retained'))
    (integration / 'file.txt').write_text('resolution\n')
    with pytest.raises(RuntimeError):
        worktree.promote_resolved_integration_worktree(root, 'test', str(msg))
    assert isolated.exists() and integration.exists()


@pytest.mark.asyncio
async def test_cleanup_and_admission_sync_task_after_restart(setup, monkeypatch):
    repo, task = setup
    msg, root, isolated = turn(task)
    (isolated / 'file.txt').write_text('recovered turn\n')
    db.update_assistant_message(msg, 'done', None, 'done')
    monkeypatch.setattr(worktree, '_CLEANUP_GRACE_SECONDS', 0)
    db.init_db()
    assert await worktree.settle_worktrees_before_turn('test', [root]) == []
    assert (root / 'file.txt').read_text() == 'recovered turn\n'
    assert (repo / 'file.txt').read_text() == 'base\n'
    assert root.exists() and not isolated.exists()


@pytest.mark.asyncio
async def test_prepare_and_worker_use_task_without_regular_session_changes(setup, monkeypatch):
    from agent.topic_queue import TopicWorker, QueueItem
    repo, task = setup
    db.set_topic_session('test', 'codex', 'main-session', str(repo), None)
    prepared = await server._prepare_chat_turn(message='Fix the task', topic='test', agent='codex', task_id=task['id'])
    assert not isinstance(prepared, JSONResponse)
    assert prepared['adhoc'] and prepared['resume_session_id'] is None
    assert prepared['code_roots'] == [str(repo)]  # Declared scope stays canonical.
    async def runner(prompt, **kwargs):
        current = Path(kwargs['cwd'])
        assert not (current / '.git').exists()
        assert current != Path(task['worktree_path'])
        (current / 'file.txt').write_text('worker task edit\n')
        yield 'Done'
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='fix', context_history=[],
        backend='codex', model=None, cwd=prepared['cwd'], code_roots=prepared['code_roots'],
        configured_cwd=prepared['configured_cwd'], adhoc=True, msg_id=prepared['asst_msg_id'])
    with patch('agent.runners.run_codex', runner):
        await TopicWorker('test')._process(item)
    assert db.get_message(item.msg_id)['status'] == 'done'
    assert (Path(task['worktree_path']) / 'file.txt').read_text() == 'worker task edit\n'
    assert (repo / 'file.txt').read_text() == 'base\n'
    assert db.get_topic_session('test', 'codex')['session_id'] == 'main-session'


@pytest.mark.asyncio
async def test_direct_task_request_creation_is_explicit_and_main_stays_default(setup):
    repo, _ = setup
    prepared = await server._prepare_chat_turn(message='New task please', topic='test', agent='codex',
        task_workspace=dict(repository=str(repo), base_ref='main'))
    assert not isinstance(prepared, JSONResponse)
    task = tasks.for_turn(prepared['asst_msg_id'])
    assert task['origin_request_id'] == prepared['user_msg_id']
    unrelated = await server._prepare_chat_turn(message='Unrelated', topic='test', agent='codex')
    assert not isinstance(unrelated, JSONResponse)
    assert tasks.binding(unrelated['asst_msg_id']) is None
    assert unrelated['cwd'] == str(repo)


@pytest.mark.asyncio
async def test_continuation_keeps_task_destination(setup, monkeypatch):
    from agent import continuations
    repo, task = setup
    parent = await server._prepare_chat_turn(message='Edit twice', topic='test', agent='codex', task_id=task['id'])
    continuations.configure(True)
    try:
        token = continuations.mint(parent['asst_msg_id'])
        accepted = continuations.create(token, dict(request_key='next-edit', steps=[dict(mode='isolated', prompt='Second edit')]))
        db.update_assistant_message(parent['asst_msg_id'], 'First edit done', None, 'done')
        continuations.complete(parent['asst_msg_id'], True, 'completed_and_synced')
        row = continuations.claim(set())
        seen = []
        class Dispatcher:
            async def dispatch(self, **kwargs):
                child = kwargs['msg_id']
                assert tasks.for_turn(child)['id'] == task['id']
                assert kwargs['adhoc'] and kwargs['resume_session_id'] is None
                seen.append(child)
                continuations.result(continuations.mint(child), {'success': True})
                db.update_assistant_message(child, 'Second edit done', None, 'done')
                continuations.complete(child, True, 'completed_and_synced')
                out = asyncio.Queue()
                await out.put(None)
                return out, None, None
        await continuations.dispatch(row, Dispatcher())
        assert len(seen) == 1
        assert continuations.inspect(token, accepted['id'])[0]['state'] == 'successful'
    finally:
        continuations.configure(False)


@pytest.mark.asyncio
async def test_flow_preparation_inherits_original_task(setup):
    _, task = setup
    origin = await server._prepare_chat_turn(message='Fix and review', topic='test', agent='codex', task_id=task['id'])
    db.create_flow_run('flow', '#test@codex>@codex', origin['user_msg_id'], [
        dict(step_id='first', topic='test', agent='codex', dependencies=[]),
        dict(step_id='review', topic='test', agent='codex', dependencies=['first']),
    ], execution_mode='durable')
    reviewed = await server._prepare_chat_turn(message='Review predecessor', topic='test', agent='codex',
        source='workflow', flow_run_id='flow', flow_step_id='review')
    assert not isinstance(reviewed, JSONResponse)
    assert tasks.for_turn(reviewed['asst_msg_id'])['id'] == task['id']
    assert reviewed['cwd'] == task['worktree_path']


@pytest.mark.asyncio
async def test_review_agent_with_different_cwd_uses_same_task(setup):
    repo, task = setup
    db.upsert_agent('review', 'codex', 'openai', None, cwd=str(repo.parent))
    reviewed = await server._prepare_chat_turn(message='Review task', topic='test', agent='review', task_id=task['id'])
    assert not isinstance(reviewed, JSONResponse)
    assert tasks.for_turn(reviewed['asst_msg_id'])['id'] == task['id']
    assert tasks.execution_scope(reviewed['asst_msg_id'], [], None)['configured_cwd'] == str(repo.parent)


@pytest.mark.asyncio
async def test_auto_resolution_records_destination_and_blocks_missing_task_before_edits(setup):
    _, task = setup
    msg, root, isolated, integration = conflict(task)
    response = await server.auto_resolve_worktree_conflict(msg, server.WorktreeDiscardRequest(topic='test', repo=str(root)))
    assert response.status_code == 200
    with db._connection() as conn:
        recovery = dict(conn.execute('SELECT * FROM task_recovery_turns').fetchone())
    tasks.check_recovery(recovery['msg_id'], str(integration))
    root.rename(root.with_name(root.name + '-retained'))
    with pytest.raises(RuntimeError):
        tasks.check_recovery(recovery['msg_id'], str(integration))
    blocked = await server.auto_resolve_worktree_conflict(msg, server.WorktreeDiscardRequest(topic='test', repo=str(root)))
    assert blocked.status_code in {400, 409}
    assert isolated.exists() and integration.exists()


@pytest.mark.asyncio
async def test_queued_task_with_missing_destination_never_starts_runner(setup):
    from agent.topic_queue import TopicWorker, QueueItem
    repo, task = setup
    prepared = await server._prepare_chat_turn(message='Queued task', topic='test', agent='codex', task_id=task['id'])
    Path(task['worktree_path']).rename(Path(task['worktree_path'] + '-retained'))
    async def unexpected(*args, **kwargs):
        pytest.fail('Missing destination must not start a runner')
        yield ''
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='fix', context_history=[], backend='codex',
        cwd=str(repo), configured_cwd=str(repo), code_roots=[str(repo)], msg_id=prepared['asst_msg_id'])
    with patch('agent.runners.run_codex', unexpected):
        await TopicWorker('test')._process(item)
    assert db.get_message(item.msg_id)['status'] == 'error'
    assert (repo / 'file.txt').read_text() == 'base\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('turn_state', ['edited', 'reverted', 'missing'])
async def test_interrupted_conflict_keeps_integration_before_status_callback(setup, monkeypatch, turn_state):
    repo, task = setup
    msg, root, isolated, integration = conflict(task)
    (integration / 'recovery-note.txt').write_text('retain this resolution work\n')
    if turn_state == 'reverted':
        (isolated / 'file.txt').write_text('base\n')
    elif turn_state == 'missing':
        isolated.rename(isolated.with_name(isolated.name + '-retained'))
    db.mark_worktree_status('test', str(msg), str(root), 'active')  # Crash before recording conflict status.
    monkeypatch.setattr(worktree, '_CLEANUP_GRACE_SECONDS', 0)
    db.init_db()
    await worktree.cleanup_worktrees('test')
    assert (integration / 'recovery-note.txt').read_text() == 'retain this resolution work\n'
    assert isolated.exists() == (turn_state != 'missing')
    assert db.get_worktrees('test', str(msg))[0]['status'] == 'promotion_failed'
    assert (repo / 'file.txt').read_text() == 'base\n'


def test_changed_integration_head_blocks_promotion(setup):
    _, task = setup
    msg, root, isolated, integration = conflict(task)
    (integration / 'file.txt').write_text('resolved\n')
    git(integration, 'add', '.')
    git(integration, 'commit', '-m', 'unexpected integration commit')
    with pytest.raises(RuntimeError, match='integration workspace identity changed'):
        worktree.promote_resolved_integration_worktree(root, 'test', str(msg))
    assert isolated.exists() and integration.exists()
    assert (root / 'file.txt').read_text() == 'concurrent task version\n'


def test_new_edits_invalidate_previously_recorded_review(setup):
    _, task = setup
    msg, root, isolated = turn(task)
    with db._connection() as conn:
        conn.execute('UPDATE task_workspaces SET review_required=0 WHERE id=?', (task['id'],))
    (isolated / 'file.txt').write_text('new edit after review\n')
    assert worktree.sync_after_turn(root, 'test', str(msg)) == []
    assert tasks.get(task['id'])['review_required'] == 1


def test_interrupted_task_creation_retains_intent_and_blocks_recreation(setup, monkeypatch):
    repo, _ = setup
    user = db.insert_user_message('test', 'codex', 'New task interrupted during creation')
    original_git = tasks._git
    def fail_creation(path, *args):
        if args[:2] == ('worktree', 'add'):
            raise RuntimeError('creation interrupted')
        return original_git(path, *args)
    monkeypatch.setattr(tasks, '_git', fail_creation)
    with pytest.raises(RuntimeError, match='creation interrupted'):
        tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='main', request_id=user)
    monkeypatch.setattr(tasks, '_git', original_git)
    db.init_db()
    with pytest.raises(RuntimeError, match='missing or unavailable'):
        tasks.create(topic='test', agent='codex', repository=str(repo), base_ref='main', request_id=user)
    with db._connection() as conn:
        rows = conn.execute('SELECT state FROM task_workspaces WHERE origin_request_id=?', (user,)).fetchall()
        assert [r[0] for r in rows] == ['preparing']


@pytest.mark.asyncio
async def test_empty_task_reference_blocks_instead_of_defaulting(setup):
    result = await server._prepare_chat_turn(message='Task edit', topic='test', agent='codex', task_id='')
    assert isinstance(result, JSONResponse) and result.status_code == 409


@pytest.mark.asyncio
async def test_scope_change_during_setup_finishes_turn_with_error(setup, monkeypatch):
    from agent import topic_queue
    repo, task = setup
    prepared = await server._prepare_chat_turn(message='Task edit', topic='test', agent='codex', task_id=task['id'])
    original_setup = topic_queue._setup_worktrees
    async def changing_setup(*args, **kwargs):
        result = await original_setup(*args, **kwargs)
        monkeypatch.setattr('agent.memory.topic_memory_squid_config', lambda topic: {'code_roots': []})
        return result
    monkeypatch.setattr(topic_queue, '_setup_worktrees', changing_setup)
    async def unexpected(*args, **kwargs):
        pytest.fail('Scope drift must prevent execution')
        yield ''
    item = topic_queue.QueueItem(seq=0, topic='test', agent='codex', prompt='fix', context_history=[],
        backend='codex', cwd=prepared['cwd'], configured_cwd=str(repo), code_roots=[str(repo)],
        adhoc=True, msg_id=prepared['asst_msg_id'])
    with patch('agent.runners.run_codex', unexpected):
        await topic_queue.TopicWorker('test')._process(item)
    assert db.get_message(item.msg_id)['status'] == 'error'
    chunks = []
    while not item.out_q.empty():
        chunks.append(item.out_q.get_nowait())
    assert chunks[-1] is None
    assert any(isinstance(c, dict) and '_error' in c for c in chunks)
    assert (repo / 'file.txt').read_text() == 'base\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('new_task', [False, True])
async def test_chat_returns_task_identity_without_publication_grant(setup, monkeypatch, new_task):
    from unittest.mock import AsyncMock
    repo, task = setup
    monkeypatch.setattr(server, 'maybe_sync', AsyncMock())
    target = (dict(task_workspace=dict(repository=str(repo), base_ref='main'))
              if new_task else dict(task_id=task['id']))
    response = await server.chat(server.ChatRequest(message='Edit locally', topic='test', agent='codex', **target))
    assert response.status_code == 200
    msg_id = int(response.headers['X-Squid-Msg-Id'])
    bound = tasks.for_turn(msg_id)
    assert response.headers['X-Squid-Task-Id'] == bound['id']
    await response.body_iterator.aclose()
