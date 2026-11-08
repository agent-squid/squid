"""Persistent Git task workspaces underneath branchless per-turn isolation.

Destination bindings are immutable evidence. Missing tasks never route to the
shared checkout, including cleanup and conflict recovery after restart.
"""
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from . import stats_db as db
from .admission_reads import local_git_read

TASKS_HOME = Path.home() / '.squid' / 'tasks'
IDENTITY_FIELDS = ('id', 'topic', 'repo_root', 'git_common_dir', 'worktree_path', 'git_dir', 'branch', 'base_sha', 'scope_json')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS task_workspaces (
        id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, topic TEXT NOT NULL,
        repo_root TEXT NOT NULL, git_common_dir TEXT NOT NULL, worktree_path TEXT NOT NULL UNIQUE,
        git_dir TEXT, branch TEXT NOT NULL, base_sha TEXT NOT NULL, scope_json TEXT NOT NULL,
        origin_request_id INTEGER, watch_id TEXT, issue_key TEXT,
        state TEXT NOT NULL DEFAULT 'preparing', review_required INTEGER NOT NULL DEFAULT 1,
        pr_number INTEGER, pr_url TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS turn_task_workspaces (
        msg_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, destination_json TEXT NOT NULL,
        execution_scope_json TEXT)''')
    if 'execution_scope_json' not in db._table_columns(conn, 'turn_task_workspaces'):
        conn.execute('ALTER TABLE turn_task_workspaces ADD COLUMN execution_scope_json TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS watch_task_workspaces (
        run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_recovery_turns (
        msg_id INTEGER PRIMARY KEY, source_msg_id INTEGER NOT NULL,
        destination_json TEXT NOT NULL, integration_path TEXT NOT NULL, git_dir TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_integrations (
        topic TEXT NOT NULL, turn_key TEXT NOT NULL, destination_json TEXT NOT NULL,
        integration_path TEXT NOT NULL, git_dir TEXT NOT NULL, head TEXT NOT NULL,
        PRIMARY KEY(topic,turn_key))''')
    from .task_publication import initialize as initialize_publication
    initialize_publication(conn)


def get(task_id):
    with db._connection() as conn:
        row = conn.execute('SELECT * FROM task_workspaces WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise RuntimeError('task workspace missing')
        return dict(row)


@local_git_read
def _git(path, *args):
    from .worktree import _run_git
    try:
        return _run_git(Path(path), *args).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError('task workspace Git identity unavailable') from exc


def identity(task):
    return {key: task[key] for key in IDENTITY_FIELDS}


def validate(task, *, agent=None):
    from .continuations import current_scope
    path = Path(task['worktree_path'])
    if task['state'] != 'ready' or path.is_symlink() or not (path / '.git').is_file():
        raise RuntimeError('task workspace missing or unavailable')
    if (str(path.resolve()) != task['worktree_path']
            or str(Path(_git(path, 'rev-parse', '--show-toplevel')).resolve()) != task['worktree_path']
            or _git(path, 'symbolic-ref', 'HEAD') != 'refs/heads/' + task['branch']
            or _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') != task['git_common_dir']
            or _git(path, 'rev-parse', '--absolute-git-dir') != task['git_dir']
            or _git(task['repo_root'], 'rev-parse', '--path-format=absolute', '--git-common-dir') != task['git_common_dir']):
        raise RuntimeError('task workspace identity changed')
    _git(path, 'merge-base', '--is-ancestor', task['base_sha'], 'HEAD')
    if (agent is not None and json.loads(task['scope_json'])['code_roots']
            != current_scope(task['topic'], agent)['code_roots']):
        raise RuntimeError('task repository scope changed')
    return task


def create(*, topic, agent, repository, base_ref, request_id=None, watch_id=None, issue_key=None):
    """Trusted request/grant path; pin committed Git objects, never backing files."""
    from .continuations import current_scope
    from .worktree import repo_root_for, _lock_for
    if (not isinstance(base_ref, str) or not base_ref or base_ref.startswith('-')
            or not isinstance(repository, str)):
        raise ValueError('invalid task repository or base')
    repo = Path(repository).expanduser().resolve()
    scope = current_scope(topic, agent)
    if str(repo) not in {str(repo_root_for(root)) for root in scope['code_roots']}:
        raise PermissionError('task repository outside configured scope')
    if (request_id is None) == (watch_id is None):
        raise ValueError('task requires one originating owner')
    if request_id is not None:
        request = db.get_message(request_id)
        if (not request or request['role'] != 'user' or request['source'] != 'human'
                or request['topic'] != topic or request['status'] != 'done'):
            raise PermissionError('task requires original human request')
        key = f'human:{request_id}:{repo}'
    else:
        if not issue_key:
            raise ValueError('task requires issue identity')
        with db._connection() as conn:
            watch = conn.execute('SELECT topic FROM watches WHERE id=?', (watch_id,)).fetchone()
        if not watch or watch['topic'] != topic:
            raise PermissionError('task watch scope mismatch')
        key = f'watch:{watch_id}:{issue_key}:{repo}'
    with _lock_for(repo):
        with db._connection() as conn:
            existing = conn.execute('SELECT id FROM task_workspaces WHERE identity_key=?', (key,)).fetchone()
        if existing:
            return validate(get(existing['id']), agent=agent)
        identifier = uuid4().hex
        path = (TASKS_HOME / identifier).resolve()
        branch = 'squid/task/' + identifier
        base_sha = _git(repo, 'rev-parse', '--verify', base_ref + '^{commit}')
        common = _git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        with db._connection() as conn:
            conn.execute('''INSERT INTO task_workspaces
                (id,identity_key,topic,repo_root,git_common_dir,worktree_path,branch,base_sha,scope_json,origin_request_id,watch_id,issue_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (identifier, key, topic, str(repo), common, str(path), branch,
                    base_sha, json.dumps(scope, sort_keys=True), request_id, watch_id, issue_key))
        # Persist intent first. Interrupted creation retains its row and files;
        # a retry blocks on 'preparing' rather than discarding recovery evidence.
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, 'worktree', 'add', '-b', branch, str(path), base_sha)
        git_dir = _git(path, 'rev-parse', '--absolute-git-dir')
        with db._connection() as conn:
            conn.execute("UPDATE task_workspaces SET git_dir=?,state='ready' WHERE id=?", (git_dir, identifier))
        return validate(get(identifier), agent=agent)


def binding(msg_id):
    with db._connection() as conn:
        if not db._table_columns(conn, 'turn_task_workspaces'):
            return None
        row = conn.execute('SELECT * FROM turn_task_workspaces WHERE msg_id=?', (msg_id,)).fetchone()
        return dict(row) if row else None


def bind(msg_id, task_id):
    from .continuations import current_scope
    message = db.get_message(msg_id)
    task = get(task_id)
    if not message or message['topic'] != task['topic']:
        raise RuntimeError('task turn scope mismatch')
    validate(task, agent=message['agent'])
    encoded = json.dumps(identity(task), sort_keys=True)
    scope = json.dumps(current_scope(task['topic'], message['agent']), sort_keys=True)
    with db._connection() as conn:
        conn.execute('''INSERT OR IGNORE INTO turn_task_workspaces
            (msg_id,task_id,destination_json,execution_scope_json) VALUES(?,?,?,?)''', (msg_id, task_id, encoded, scope))
        saved = conn.execute('SELECT * FROM turn_task_workspaces WHERE msg_id=?', (msg_id,)).fetchone()
        if saved['task_id'] != task_id or saved['destination_json'] != encoded or saved['execution_scope_json'] != scope:
            raise RuntimeError('task turn destination cannot change')
    return task


def for_turn(msg_id):
    from .continuations import current_scope
    saved = binding(msg_id)
    if not saved:
        return None
    task = get(saved['task_id'])
    if identity(task) != json.loads(saved['destination_json']):
        raise RuntimeError('task turn destination changed')
    message = db.get_message(msg_id)
    if not message or message['topic'] != task['topic']:
        raise RuntimeError('task turn owner missing')
    if not saved['execution_scope_json'] or json.loads(saved['execution_scope_json']) != current_scope(task['topic'], message['agent']):
        raise RuntimeError('task turn execution scope changed')
    return validate(task, agent=message['agent'])


def check_destination(repo_root, topic, turn_key):
    """Shared guard used by setup, sync, cleanup and retained conflict promotion."""
    task = for_turn(turn_key)
    if task:
        if task['topic'] != topic or Path(repo_root).resolve() != Path(task['worktree_path']):
            raise RuntimeError('task sync destination mismatch')
    else:
        with db._connection() as conn:
            saved = (conn.execute('SELECT id FROM task_workspaces WHERE worktree_path=?', (str(Path(repo_root).resolve()),)).fetchone()
                     if db._table_columns(conn, 'task_workspaces') else None)
        if saved or Path(repo_root).resolve().is_relative_to(TASKS_HOME.resolve()):
            raise RuntimeError('task sync destination binding missing')
    return task


def execution_scope(msg_id, roots, configured_cwd):
    from .continuations import capture_scope
    task = for_turn(msg_id)
    return json.loads(binding(msg_id)['execution_scope_json']) if task else capture_scope(roots, configured_cwd)


def bind_recovery(msg_id, source_msg_id, repo_root):
    from .worktree import integration_worktree_path
    task = check_destination(repo_root, db.get_message(source_msg_id)['topic'], str(source_msg_id))
    if not task:
        return
    integration = integration_worktree_path(Path(repo_root), task['topic'], str(source_msg_id))
    check_integration(task, task['topic'], str(source_msg_id), integration)
    with db._connection() as conn:
        conn.execute('INSERT INTO task_recovery_turns VALUES(?,?,?,?,?)',
            (msg_id, source_msg_id, json.dumps(identity(task), sort_keys=True), str(integration),
             _git(integration, 'rev-parse', '--absolute-git-dir')))


def check_recovery(msg_id, cwd):
    with db._connection() as conn:
        if not db._table_columns(conn, 'task_recovery_turns'):
            return
        rec = conn.execute('SELECT * FROM task_recovery_turns WHERE msg_id=?', (msg_id,)).fetchone()
    if not rec:
        return
    task = for_turn(rec['source_msg_id'])
    if (not task or identity(task) != json.loads(rec['destination_json'])
            or str(Path(cwd).resolve()) != rec['integration_path']
            or _git(cwd, 'rev-parse', '--absolute-git-dir') != rec['git_dir']
            or _git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir') != task['git_common_dir']):
        raise RuntimeError('task conflict recovery destination changed')
    check_integration(task, task['topic'], str(rec['source_msg_id']), Path(cwd))


def save_integration(task, topic, turn_key, path, head):
    with db._connection() as conn:
        conn.execute('INSERT OR REPLACE INTO task_integrations VALUES(?,?,?,?,?,?)',
            (topic, turn_key, json.dumps(identity(task), sort_keys=True), str(path),
             _git(path, 'rev-parse', '--absolute-git-dir'), head))


def check_integration(task, topic, turn_key, path):
    with db._connection() as conn:
        rec = conn.execute('SELECT * FROM task_integrations WHERE topic=? AND turn_key=?', (topic, turn_key)).fetchone()
    if (not rec or json.loads(rec['destination_json']) != identity(task)
            or str(path.resolve()) != rec['integration_path']
            or _git(path, 'rev-parse', '--absolute-git-dir') != rec['git_dir']
            or _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir') != task['git_common_dir']
            or _git(path, 'rev-parse', 'HEAD') != rec['head']):
        raise RuntimeError('task integration workspace identity changed')


def require_review(task_id):
    with db._connection() as conn:
        conn.execute('UPDATE task_workspaces SET review_required=1 WHERE id=?', (task_id,))


def for_flow(flow_id):
    if not flow_id:
        return None
    with db._connection() as conn:
        rows = conn.execute('''SELECT DISTINCT t.task_id FROM turn_task_workspaces t
            JOIN chat_messages m ON m.id=t.msg_id JOIN flow_runs f
            ON m.reply_to=f.original_prompt_msg_id WHERE f.flow_run_id=?''', (flow_id,)).fetchall()
    if len(rows) > 1:
        raise RuntimeError('ambiguous Flow task workspace')
    return rows[0]['task_id'] if rows else None


def issue_identity(event, watch):
    payload = json.loads(event['payload'])
    issue = payload.get('issue', payload) if isinstance(payload, dict) else {}
    number = issue.get('number') if isinstance(issue, dict) else None
    if type(number) is int and number > 0:
        return str(number)
    match = re.search(r'/issues/(\d+)(?:$|[?#])', watch['target'])
    return match.group(1) if match else None


def for_watch(run_id, agent, *, service=None):
    from . import watch_runs
    from .watch_publication import matches
    run, watch, grant, events = watch_runs.details(run_id)
    if not grant['publication_json']:
        return None
    with db._connection() as conn:
        if (not service or run['state'] not in {'claimed', 'queued', 'running'}
                or not watch_runs.authorized(service, conn, watch, grant, now=service.clock())):
            raise PermissionError('task watch no longer authorized')
    if not matches(grant['publication_json'], watch['topic'], grant['agent']):
        raise RuntimeError('task publication scope changed')
    keys = {issue_identity(e, watch) for e in events}
    if len(keys) != 1 or None in keys:
        raise RuntimeError('task watch requires one issue identity')
    with db._connection() as conn:
        saved = conn.execute('SELECT task_id FROM watch_task_workspaces WHERE run_id=?', (run_id,)).fetchone()
    if saved:
        task = validate(get(saved['task_id']), agent=agent)
        scope = json.loads(grant['publication_json'])
        if (task['watch_id'] != watch['id'] or task['issue_key'] != next(iter(keys))
                or task['repo_root'] != scope['repository']):
            raise RuntimeError('task watch destination changed')
        return task
    scope = json.loads(grant['publication_json'])
    task = create(topic=watch['topic'], agent=agent, repository=scope['repository'],
        base_ref='refs/remotes/' + scope['remote'] + '/' + scope['base_branch'],
        watch_id=watch['id'], issue_key=next(iter(keys)))
    with db._connection() as conn:
        conn.execute('INSERT OR IGNORE INTO watch_task_workspaces VALUES(?,?)', (run_id, task['id']))
    return task
