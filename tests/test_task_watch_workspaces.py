import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from agent import stats_db as db, task_workspaces as tasks, watch_runs, worktree, server
from tests.test_continuation_owners import setup, publication_repo, grant_publication


@pytest.fixture
def task_watch(setup, publication_repo, tmp_path, monkeypatch):
    body, git = publication_repo
    git('update-ref', 'refs/remotes/origin/main', git('rev-parse', 'HEAD'))
    grant_publication(setup, body)
    monkeypatch.setattr(tasks, 'TASKS_HOME', tmp_path / 'tasks')
    monkeypatch.setattr(worktree, '_WORKTREES_HOME', tmp_path / 'turns')
    monkeypatch.setattr(server, 'WORKTREE_ISOLATION_ENABLED', True)
    monkeypatch.setattr(server, 'topic_memory_squid_config', lambda topic: {'code_roots': [body['repository']]})
    return body, git


@pytest.mark.asyncio
async def test_watch_runs_choose_one_issue_and_reuse_its_workspace(setup, task_watch):
    service = setup[0]
    watch_id = setup[3]['id']
    with db._connection() as conn:
        conn.execute('INSERT INTO watch_events(watch_id,event_key,payload,observed_at) VALUES(?,?,?,101)',
            (watch_id, 'second-issue', json.dumps({'issue': {'number': 2}})))
    seen = []
    for number in (1, 2, 1):
        run_id = watch_runs.claim_batch(service, lambda topic: False)
        events = watch_runs.details(run_id)[3]
        assert {tasks.issue_identity(e, setup[3]) for e in events} == {str(number)}
        prepared = await server._prepare_chat_turn(message='Fix issue', topic='squid', agent='watch-agent',
            source='watch', watch_run_id=run_id)
        assert not isinstance(prepared, JSONResponse)
        task = tasks.for_turn(prepared['asst_msg_id'])
        assert task['issue_key'] == str(number)
        assert task['origin_request_id'] is None
        assert task['watch_id'] == watch_id
        seen.append(task['id'])
        watch_runs.set_state(run_id, 'queued')
        assert watch_runs.begin(service, run_id)
        db.update_assistant_message(prepared['asst_msg_id'], 'done', None, 'done')
        db.insert_run_event(prepared['asst_msg_id'], 4, 'done', None)
        watch_runs.settle(service, run_id)
        assert watch_runs.details(run_id)[0]['state'] == 'succeeded'
        if number == 2:
            with db._connection() as conn:
                conn.execute('INSERT INTO watch_events(watch_id,event_key,payload,observed_at) VALUES(?,?,?,102)',
                    (watch_id, 'first-issue-update', json.dumps({'issue': {'number': 1}})))
    assert seen[0] != seen[1] and seen[2] == seen[0]
    assert (Path(task_watch[0]['repository']) / 'app.txt').read_text() == 'base\n'


def test_missing_issue_identity_blocks_pr_watch_without_acknowledgement(setup, task_watch):
    with db._connection() as conn:
        conn.execute("UPDATE watch_events SET payload='{}'")
    assert watch_runs.claim_batch(setup[0], lambda topic: False) is None
    with db._connection() as conn:
        assert conn.execute('SELECT pause_reason FROM watches').fetchone()[0] == 'issue_identity_missing'
        assert conn.execute('SELECT disposition FROM watch_events').fetchone()[0] == 'pending'
        assert conn.execute('SELECT COUNT(*) FROM task_workspaces').fetchone()[0] == 0


def test_revocation_before_preparation_prevents_task_allocation(setup, task_watch):
    run_id = watch_runs.claim_batch(setup[0], lambda topic: False)
    grant_id = watch_runs.details(run_id)[2]['id']
    watch_runs.revoke_execution(setup[0], topic='squid', watch_id=setup[3]['id'], grant_id=grant_id)
    with pytest.raises(PermissionError):
        tasks.for_watch(run_id, 'watch-agent', service=setup[0])
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM task_workspaces').fetchone()[0] == 0


@pytest.mark.parametrize('local_base', ['missing', 'stale'])
def test_watch_pins_remote_base_instead_of_local_branch(setup, task_watch, local_base):
    body, git = task_watch
    initial = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/heads/feature', initial)
    git('symbolic-ref', 'HEAD', 'refs/heads/feature')
    (Path(body['repository']) / 'app.txt').write_text('remote base\n')
    git('add', '.')
    git('commit', '-m', 'Advance remote base')
    expected = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', expected)
    if local_base == 'missing':
        git('update-ref', '-d', 'refs/heads/main')
    run_id = watch_runs.claim_batch(setup[0], lambda topic: False)
    task = tasks.for_watch(run_id, 'watch-agent', service=setup[0])
    assert task['base_sha'] == expected
    assert (Path(task['worktree_path']) / 'app.txt').read_text() == 'remote base\n'
    assert git('symbolic-ref', 'HEAD') == 'refs/heads/feature'


def test_missing_remote_base_blocks_instead_of_falling_back_to_local(setup, task_watch):
    _, git = task_watch
    git('update-ref', '-d', 'refs/remotes/origin/main')
    run_id = watch_runs.claim_batch(setup[0], lambda topic: False)
    with pytest.raises(RuntimeError):
        tasks.for_watch(run_id, 'watch-agent', service=setup[0])
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM task_workspaces').fetchone()[0] == 0


def test_watch_resolves_the_configured_remote(setup, publication_repo, tmp_path, monkeypatch):
    body, git = publication_repo
    git('remote', 'rename', 'origin', 'upstream')
    expected = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/upstream/main', expected)
    grant_publication(setup, body | {'remote': 'upstream'})
    monkeypatch.setattr(tasks, 'TASKS_HOME', tmp_path / 'tasks')
    run_id = watch_runs.claim_batch(setup[0], lambda topic: False)
    task = tasks.for_watch(run_id, 'watch-agent', service=setup[0])
    assert task['base_sha'] == expected
