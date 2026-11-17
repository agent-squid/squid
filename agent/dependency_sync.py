"""Fixed host dependency commands, gated by durable continuation authority.

Package-manager lifecycle scripts retain their normal host privileges; this is
routing and recovery, not an OS sandbox. No command text is executed as a shell.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from . import stats_db as db

COMMANDS = {
    'uv_sync': ('uv', 'sync', '--locked', '--reinstall'),
    'npm_ci': ('npm', 'ci'),
    'pnpm_install': ('pnpm', 'install', '--frozen-lockfile'),
}
TIMEOUT = 600
OUTPUT_LIMIT = 1024 * 1024


class _Stopped(RuntimeError):
    """An internal, fixed dependency-maintenance reason code."""


def specification(prompt):
    try:
        value = json.loads(prompt)
        if (not isinstance(value, dict) or set(value) != {'repository', 'operation'}
                or not isinstance(value['repository'], str)
                or not isinstance(value['operation'], str)
                or value['operation'] not in COMMANDS):
            raise ValueError()
        root = Path(value['repository'])
        if not root.is_absolute() or str(root.resolve()) != value['repository']:
            raise ValueError()
    except (TypeError, ValueError, OSError, RuntimeError):
        raise ValueError('invalid_continuation_request') from None
    return value


def validate_scope(step):
    from .task_workspaces import for_turn
    value = specification(step['prompt'])
    scope = json.loads(step['scope_json'])
    roots = scope['code_roots'] or [scope['configured_cwd']]
    if (json.loads(step['owner_json'])['kind'] != 'human'
            or value['repository'] not in roots
            or not Path(value['repository']).is_dir()
            or for_turn(step['parent_id'])):
        raise ValueError('dependency_scope_changed')
    return value


def _authorized(step):
    from . import continuations as c
    with c._connection() as conn:
        current = conn.execute('SELECT * FROM continuations WHERE id=?', (step['id'],)).fetchone()
        child = conn.execute('SELECT status FROM chat_messages WHERE id=?',
                             (current['child_id'],)).fetchone() if current else None
        return (current is not None and current['state'] == 'running'
                and child is not None and child['status'] == 'pending'
                and c.authorization_error(conn, current) is None)


@contextmanager
def _repository_lock(step, root):
    from .worktree import _lock_for
    lock = _lock_for(root)
    started = time.monotonic()
    while not lock.acquire(timeout=.1):
        if not _authorized(step):
            raise _Stopped('dependency_authorization_changed')
        if time.monotonic() - started >= TIMEOUT:
            raise _Stopped('dependency_lock_timeout')
    try:
        yield
    finally:
        lock.release()


def _run(step, value):
    # Ignore inherited virtualenv, Python and package-manager overrides. In
    # particular UV_PROJECT_ENVIRONMENT must never redirect this host action.
    env = {key: val for key, val in os.environ.items()
           if not key.startswith(('SQUID_', 'UV_', 'PIP_', 'PYTHON', 'npm_config_', 'NPM_CONFIG_'))
           and key not in {'VIRTUAL_ENV', 'CONDA_PREFIX', '__PYVENV_LAUNCHER__'}}
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(COMMANDS[value['operation']], cwd=value['repository'],
                                   env=env, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        reason = None
        started = time.monotonic()
        try:
            while process.poll() is None:
                if not _authorized(step):
                    reason = 'dependency_authorization_changed'
                elif time.monotonic() - started >= TIMEOUT:
                    reason = 'dependency_timeout'
                elif os.fstat(output.fileno()).st_size > OUTPUT_LIMIT:
                    reason = 'dependency_output_limit'
                if reason:
                    break
                time.sleep(.1)
        finally:
            # Reap lifecycle scripts even if the package-manager leader already
            # exited. Its exit must not leave unsupervised host mutations behind.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if os.fstat(output.fileno()).st_size > OUTPUT_LIMIT:
            reason = reason or 'dependency_output_limit'
        if not _authorized(step):
            reason = reason or 'dependency_authorization_changed'
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 16000))
        log = output.read(16000).decode('utf-8', errors='replace')
    return process.returncode, reason, log


def execute(step):
    from . import continuations as c
    user = db.insert_user_message(step['topic'], step['agent'], 'Run dependency maintenance', source='continuation')
    child = db.insert_assistant_message(step['topic'], step['agent'], user, source='continuation', adhoc=True)
    if not c.link(step['id'], child):
        db.mark_assistant_cancelled(child, 'Dependency maintenance no longer claimed')
        return
    success, reason, text = False, 'dependency_failed', ''
    try:
        value = validate_scope(step)
        with _repository_lock(step, Path(value['repository'])):
            if not _authorized(step):
                raise _Stopped('dependency_authorization_changed')
            code, stopped, output = _run(step, value)
            success = code == 0 and stopped is None
            reason = stopped or ('dependency_completed' if success else 'dependency_failed')
            text = f"Dependency maintenance: {value['operation']} in {value['repository']}\nExit status: {code}\n{reason}\n\n{output}"
    except Exception as exc:
        if isinstance(exc, _Stopped):
            reason = exc.args[0]
        # Never echo exception text containing environment values or credentials.
        text = f'Dependency maintenance failed: {reason} ({type(exc).__name__})'
    db.update_assistant_message(child, text, None, 'done' if success else 'error', only_if_pending=True)
    message = db.get_message(child)
    if success and (message is None or message['status'] != 'done'):
        success, reason = False, 'dependency_authorization_changed'
    with c._connection() as conn:
        conn.execute('UPDATE continuations SET result_success=? WHERE id=?', (int(success), step['id']))
    db.insert_run_event(child, 1, 'done' if success else 'error', None if success else reason)
    c.complete(child, success, reason)
