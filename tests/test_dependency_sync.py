import json
import os
import sys

import pytest

from agent import continuations as c, dependency_sync as deps, stats_db as db


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'test.db')
    db.init_db()
    c.initialize()
    c.configure(True)
    repo = tmp_path / 'backing'
    repo.mkdir()
    monkeypatch.setattr(c, 'current_scope', lambda topic, agent: c.capture_scope([str(repo)], None))
    user = db.insert_user_message('test', 'codex', 'Update dependencies after fixing the code')
    parent = db.insert_assistant_message('test', 'codex', user)
    token = c.mint(parent)
    yield repo, parent, token
    c.configure(False)


def body(repo, remaining=True):
    steps = [dict(mode='dependency_sync', prompt=json.dumps(dict(repository=str(repo), operation='uv_sync')))]
    if remaining:
        steps.append(dict(mode='isolated', prompt='Check the updated environment'))
    return dict(request_key='deps', steps=steps)


def ready(setup):
    repo, parent, token = setup
    c.create(token, body(repo))
    assert c.claim(set()) is None
    db.update_assistant_message(parent, 'Edited manifests', None, 'done')
    c.complete(parent, True, 'completed_and_synced')
    return c.claim(set())


def test_exact_commands():
    assert deps.COMMANDS['uv_sync'] == ('uv', 'sync', '--locked', '--reinstall')
    assert deps.COMMANDS['npm_ci'] == ('npm', 'ci')
    assert deps.COMMANDS['pnpm_install'] == ('pnpm', 'install', '--frozen-lockfile')


@pytest.mark.parametrize('code', [0, 7])
@pytest.mark.asyncio
async def test_host_runs_in_backing_and_gates_next_step(setup, monkeypatch, code):
    repo, parent, token = setup
    # Execute a real child process without modifying the developer's environment.
    script = repo / 'installer.py'
    script.write_text('import os, pathlib, sys\n'
                      'assert "UV_PROJECT_ENVIRONMENT" not in os.environ\n'
                      'assert "SQUID_CONTINUE_TOKEN" not in os.environ\n'
                      'pathlib.Path("launcher").write_text(str(pathlib.Path.cwd()))\n'
                      f'print("installer output"); sys.exit({code})\n')
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', '/bad/ephemeral/environment')
    monkeypatch.setenv('SQUID_CONTINUE_TOKEN', 'secret')
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, str(script)))
    step = ready(setup)
    await c.dispatch(step, None)  # No model dispatcher is needed.
    assert (repo / 'launcher').read_text() == str(repo)
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == ('successful' if code == 0 else 'failed')
    message = db.get_message(result['child_id'])
    assert f'Exit status: {code}' in message['content']
    assert 'installer output' in message['content']
    from agent.turn_context import recorded_context
    assert recorded_context(message['id'])['instructions'] == []
    next_step = c.claim(set())
    assert (next_step is not None) == (code == 0)


def test_failed_parent_never_runs(setup):
    repo, parent, token = setup
    c.create(token, body(repo))
    db.update_assistant_message(parent, 'Conflict', None, 'done')
    c.complete(parent, False, 'sync_conflict')
    assert c.claim(set()) is None
    assert c.inspect(token)[0]['state'] == 'blocked'


@pytest.mark.parametrize('change', ['scope', 'owner', 'cancel', 'restart'])
@pytest.mark.asyncio
async def test_no_effect_after_authority_change(setup, monkeypatch, change):
    repo, parent, token = setup
    step = ready(setup)
    if change == 'scope':
        monkeypatch.setattr(c, 'current_scope', lambda topic, agent: c.capture_scope([], None))
    elif change == 'owner':
        with db._connection() as conn:
            conn.execute('UPDATE chat_messages SET content=? WHERE id=?', ('Other request', db.get_message(parent)['reply_to']))
    elif change == 'cancel':
        c.inspect(token, step['id'], True)
    else:
        c.recover()
    monkeypatch.setattr(deps, '_run', lambda *args: pytest.fail('Unauthorized host execution'))
    await c.dispatch(step, None)
    assert c.claim(set()) is None


@pytest.mark.parametrize('prompt', ['uv sync; echo unsafe', '{}', '{"repository":"relative","operation":"uv_sync"}'])
def test_invalid_command_rejected(setup, prompt):
    with pytest.raises(ValueError):
        c.create(setup[2], dict(request_key='bad', mode='dependency_sync', prompt=prompt))


def test_outside_scope_rejected(setup, tmp_path):
    with pytest.raises(ValueError):
        c.create(setup[2], body(tmp_path))


@pytest.mark.asyncio
async def test_timeout_stops_process_and_sequence(setup, monkeypatch):
    monkeypatch.setattr(deps, 'TIMEOUT', .05)
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, '-c', 'import time; time.sleep(30)'))
    step = ready(setup)
    await c.dispatch(step, None)
    result = next(row for row in c.inspect(setup[2]) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_timeout'
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_cancel_running_command_stops_it(setup, monkeypatch):
    import asyncio
    repo, parent, token = setup
    started = repo / 'started'
    script = f'import pathlib,time; pathlib.Path({str(started)!r}).touch(); time.sleep(30)'
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, '-c', script))
    step = ready(setup)
    task = asyncio.create_task(c.dispatch(step, None))
    try:
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(.02)
        assert started.exists()
        c.inspect(token, step['id'], True)
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            await task
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_authorization_changed'
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_real_uv_generates_stable_launcher(setup, monkeypatch):
    import shutil
    import subprocess
    uv = shutil.which('uv')
    if not uv:
        pytest.skip('uv is not installed')
    repo, _, token = setup
    # Offline smoke test: needs only the same cached hatchling used by Squid.
    (repo / 'pyproject.toml').write_text(
        '[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n'
        '[project]\nname="dependency-launcher-smoke"\nversion="0.0.1"\n'
        '[project.scripts]\ndependency-launcher-smoke="dependency_launcher_smoke:main"\n')
    package = repo / 'dependency_launcher_smoke'
    package.mkdir()
    (package / '__init__.py').write_text('def main():\n    print("stable launcher")\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('UV_', 'SQUID_')) and key != 'VIRTUAL_ENV'}
    locked = subprocess.run([uv, 'lock', '--offline'], cwd=repo, env=env, capture_output=True, timeout=30)
    if locked.returncode:
        pytest.skip('Offline uv resolution unavailable')
    # Avoid network even when the build backend is not cached on a test host.
    built = subprocess.run([uv, 'sync', '--offline', '--locked'], cwd=repo, env=env, capture_output=True, timeout=30)
    if built.returncode:
        pytest.skip('Offline hatchling build unavailable')
    launcher = repo / '.venv/bin/dependency-launcher-smoke'
    launcher.write_text('#!/deleted/turn/.venv/bin/python\n')
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (uv, 'sync', '--offline', '--locked', '--reinstall'))
    step = ready(setup)
    await c.dispatch(step, None)
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == 'successful', db.get_message(result['child_id'])['content']
    assert str(repo / '.venv/bin/python') in launcher.read_text()
    checked = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=10)
    assert checked.returncode == 0
    assert checked.stdout.strip() == 'stable launcher'


@pytest.mark.asyncio
async def test_cancellation_does_not_wait_for_repository_lock(setup, monkeypatch):
    import asyncio
    from agent.worktree import _lock_for
    repo, _, token = setup
    step = ready(setup)
    lock = _lock_for(repo)
    lock.acquire()
    task = asyncio.create_task(c.dispatch(step, None))
    try:
        for _ in range(100):
            row = next(r for r in c.inspect(token) if r['id'] == step['id'])
            if row['state'] == 'running':
                break
            await asyncio.sleep(.01)
        assert row['state'] == 'running'
        c.inspect(token, step['id'], True)
        await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        lock.release()
        await task
    assert c.claim(set()) is None
    assert not (repo / '.venv').exists()


@pytest.mark.asyncio
async def test_repository_lock_wait_is_bounded(setup, monkeypatch):
    from agent.worktree import _lock_for
    monkeypatch.setattr(deps, 'TIMEOUT', .05)
    repo, _, token = setup
    step = ready(setup)
    lock = _lock_for(repo)
    lock.acquire()
    try:
        await c.dispatch(step, None)
    finally:
        lock.release()
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_lock_timeout'
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_package_manager_exit_stops_remaining_scripts(setup, monkeypatch):
    import asyncio
    repo, _, _ = setup
    marker = repo / 'unsupervised-mutation'
    started = repo / 'child-started'
    child_script = (f'import pathlib,time; pathlib.Path({str(started)!r}).touch(); '
                    f'time.sleep(.7); pathlib.Path({str(marker)!r}).touch()')
    parent_script = (f'import pathlib,subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child_script!r}]); '
                     f'exec("while not pathlib.Path({str(started)!r}).exists(): time.sleep(.01)")')
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, '-c', parent_script))
    await c.dispatch(ready(setup), None)
    assert started.exists()
    await asyncio.sleep(.9)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_output_limit_fails_even_after_fast_exit(setup, monkeypatch):
    monkeypatch.setattr(deps, 'OUTPUT_LIMIT', 100)
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, '-c', 'print("x"*200)'))
    step = ready(setup)
    await c.dispatch(step, None)
    result = next(row for row in c.inspect(setup[2]) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_output_limit'
    assert c.claim(set()) is None


def test_symlink_loop_is_invalid_request(setup):
    repo, _, token = setup
    cycle = repo / 'cycle'
    cycle.symlink_to(cycle)
    with pytest.raises(ValueError, match='invalid_continuation_request'):
        c.create(token, body(cycle))


@pytest.mark.asyncio
async def test_cancel_immediately_after_lock_acquisition_reports_reason(setup, monkeypatch):
    from contextlib import contextmanager
    repo, _, token = setup
    step = ready(setup)

    @contextmanager
    def acquired(*args):
        c.inspect(token, step['id'], True)
        yield

    monkeypatch.setattr(deps, '_repository_lock', acquired)
    monkeypatch.setattr(deps, '_run', lambda *args: pytest.fail('Cancelled step must not launch'))
    await c.dispatch(step, None)
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_authorization_changed'
    assert 'dependency_authorization_changed' in db.get_message(result['child_id'])['content']
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_missing_package_manager_records_failure(setup, monkeypatch):
    repo, _, token = setup
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (str(repo / 'nonexistent-command'),))
    step = ready(setup)
    await c.dispatch(step, None)
    result = next(row for row in c.inspect(token) if row['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_failed'
    message = db.get_message(result['child_id'])['content']
    assert 'dependency_failed (FileNotFoundError)' in message
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_chat_stop_cancels_host_process_and_preserves_message(setup, monkeypatch):
    import asyncio
    repo, _, token = setup
    started = repo / 'started'
    script = f'import pathlib,time; pathlib.Path({str(started)!r}).touch(); time.sleep(30)'
    monkeypatch.setitem(deps.COMMANDS, 'uv_sync', (sys.executable, '-c', script))
    step = ready(setup)
    task = asyncio.create_task(c.dispatch(step, None))
    try:
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(.02)
        assert started.exists()
        row = next(r for r in c.inspect(token) if r['id'] == step['id'])
        assert db.mark_assistant_cancelled(row['child_id'], 'Stopped from chat')
        await asyncio.wait_for(asyncio.shield(task), 3)
    finally:
        if not task.done():
            c.inspect(token, step['id'], True)
            await task
    result = next(r for r in c.inspect(token) if r['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['reason'] == 'dependency_authorization_changed'
    assert db.get_message(result['child_id'])['status'] == 'cancelled'
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_chat_stop_between_command_exit_and_outcome_save_blocks_next_step(setup, monkeypatch):
    repo, _, token = setup
    step = ready(setup)

    def command_completed(row, value):
        current = next(r for r in c.inspect(token) if r['id'] == row['id'])
        db.mark_assistant_cancelled(current['child_id'], 'Stopped from chat')
        return 0, None, 'Command exited successfully'

    monkeypatch.setattr(deps, '_run', command_completed)
    await c.dispatch(step, None)
    result = next(r for r in c.inspect(token) if r['id'] == step['id'])
    assert result['state'] == 'failed'
    assert result['result_success'] == 0
    assert result['reason'] == 'dependency_authorization_changed'
    assert db.get_message(result['child_id'])['status'] == 'cancelled'
    assert c.claim(set()) is None
