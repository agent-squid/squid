"""Recorded, reviewed, single-task publication. No agent shell executes Git writes.

Intent precedes effects. Recovery observes exact refs/PRs; it never retries an
uncertain push or PR creation. Host-authorized reconciliation can adopt a new
human owner without changing the saved destination or intended commit.
"""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode

from . import stats_db as db, task_workspaces as tasks


log = logging.getLogger(__name__)


class PublicationBlocked(RuntimeError):
    pass


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS task_publication_grants (
        task_id TEXT NOT NULL, request_id INTEGER NOT NULL, agent TEXT NOT NULL,
        request_digest TEXT NOT NULL, destination_json TEXT NOT NULL, scope_json TEXT NOT NULL,
        expires REAL NOT NULL, audit TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(task_id,request_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_reviews (
        step_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, msg_id INTEGER NOT NULL,
        head TEXT NOT NULL, tree TEXT NOT NULL, destination_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending')''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_publications (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, child_id INTEGER NOT NULL,
        owner_json TEXT NOT NULL, destination_json TEXT NOT NULL, scope_json TEXT NOT NULL,
        base_sha TEXT NOT NULL, parent_head TEXT NOT NULL, tree TEXT NOT NULL,
        intended_sha TEXT NOT NULL, remote_before TEXT, state TEXT NOT NULL,
        pr_number INTEGER, pr_url TEXT, error TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_publication_authorities (
        id INTEGER PRIMARY KEY AUTOINCREMENT, publication_id TEXT NOT NULL,
        owner_json TEXT NOT NULL, created REAL NOT NULL)''')
    if 'published_sha' not in db._table_columns(conn, 'task_workspaces'):
        conn.execute('ALTER TABLE task_workspaces ADD COLUMN published_sha TEXT')


def encode(value):
    return json.dumps(value, sort_keys=True)


def record_grant(task_id, *, request_id, publication, expires_at, audit):
    """Trusted host-control path only; never called by continuation transport."""
    from .watch_publication import capture
    task = tasks.get(task_id)
    request = db.get_message(request_id)
    if (not request or request['source'] != 'human' or request['role'] != 'user'
            or request['status'] != 'done' or request['topic'] != task['topic']
            or type(expires_at) not in (float, int) or not math.isfinite(expires_at)
            or not time.time() < expires_at <= time.time() + 86400 or not audit):
        raise PermissionError('publication_grant_not_authorized')
    tasks.validate(task, agent=request['agent'])
    scope = capture(publication, task['topic'], request['agent'])
    if scope['repository'] != task['repo_root']:
        raise PermissionError('publication_repository_mismatch')
    values = (request['agent'], hashlib.sha256(request['content'].encode()).hexdigest(),
              encode(tasks.identity(task)), encode(scope), expires_at, audit)
    with db._connection() as conn:
        existing = conn.execute('SELECT * FROM task_publication_grants WHERE task_id=? AND request_id=?', (task_id, request_id)).fetchone()
        if existing:
            if tuple(existing[k] for k in ('agent','request_digest','destination_json','scope_json','expires','audit')) != values:
                raise ValueError('publication_grant_conflict')
            return
        conn.execute('INSERT INTO task_publication_grants VALUES(?,?,?,?,?,?,?,?,0)', (task_id, request_id, *values))


def scope_for(conn, owner_json, task):
    from . import continuation_owners as owners
    from .watch_publication import matches
    owner = json.loads(owner_json)
    tasks.validate(task, agent=owner['agent'])
    if not owners.valid(conn, owner_json, topic=task['topic'], agent=owner['agent']):
        raise PublicationBlocked('publication_owner_changed')
    if owner['kind'] == 'watch':
        grant = conn.execute('SELECT * FROM watch_execution_grants WHERE id=?', (owner['grant_id'],)).fetchone()
        bound = conn.execute('SELECT task_id FROM watch_task_workspaces WHERE run_id=?', (owner['run_id'],)).fetchone()
        if (not grant or not grant['publication_json'] or not grant['publication_enabled'] or not bound or bound['task_id'] != task['id']
                or task['watch_id'] != owner['watch_id']):
            raise PublicationBlocked('publication_not_granted')
        encoded, agent = grant['publication_json'], grant['agent']
    else:
        grant = conn.execute('SELECT * FROM task_publication_grants WHERE task_id=? AND request_id=?',
                             (task['id'], owner['request_id'])).fetchone()
        if (not grant or grant['revoked'] or grant['expires'] <= time.time()
                or grant['request_digest'] != owner['request_digest']
                or grant['destination_json'] != encode(tasks.identity(task))):
            raise PublicationBlocked('publication_not_granted')
        encoded, agent = grant['scope_json'], grant['agent']
    if not matches(encoded, task['topic'], agent):
        raise PublicationBlocked('publication_scope_changed')
    scope = json.loads(encoded)
    if scope['repository'] != task['repo_root'] or not task['branch'].startswith('squid/task/'):
        raise PublicationBlocked('publication_destination_changed')
    if scope['fetch_url'] != scope['push_url']:
        github = GitHub()
        if github.repository(dict(push_url=scope['fetch_url'])) != github.repository(scope):
            raise PublicationBlocked('publication_requires_single_remote_repository')
    if owner.get('flow'):
        # Publishing in an earlier Flow member would precede its downstream
        # review, and waiting for that review would deadlock its dependencies.
        pending = conn.execute("SELECT 1 FROM flow_steps WHERE flow_run_id=? AND assistant_msg_id IS NOT ? AND status!='done'",
                               (owner['flow']['flow_run_id'], owner['parent_id'])).fetchone()
        if pending:
            raise PublicationBlocked('publication_requires_final_flow_member')
    return scope


def begin_review(step, child):
    from .worktree import _snapshot_tree
    task = tasks.for_turn(child)
    if not task:
        raise PublicationBlocked('review_requires_task')
    head = tasks._git(task['worktree_path'], 'rev-parse', 'HEAD')
    tree = _snapshot_tree(Path(task['worktree_path']))
    with db._connection() as conn:
        conn.execute('INSERT INTO task_reviews(step_id,task_id,msg_id,head,tree,destination_json) VALUES(?,?,?,?,?,?)',
                     (step['id'],task['id'],child,head,tree,encode(tasks.identity(task))))


def finish_review(conn, step):
    from .continuations import authorization_error
    from .worktree import _snapshot_tree
    review = conn.execute('SELECT * FROM task_reviews WHERE step_id=?', (step['id'],)).fetchone()
    task = tasks.for_turn(step['child_id'])
    valid = bool(review and task and not authorization_error(conn, step)
                 and review['destination_json'] == encode(tasks.identity(task))
                 and review['head'] == tasks._git(task['worktree_path'], 'rev-parse', 'HEAD')
                 and review['tree'] == _snapshot_tree(Path(task['worktree_path'])))
    conn.execute('UPDATE task_reviews SET state=? WHERE step_id=?', ('passed' if valid else 'failed',step['id']))
    if valid:
        conn.execute('UPDATE task_workspaces SET review_required=0 WHERE id=?', (task['id'],))
    return valid


def get(identifier):
    with db._connection() as conn:
        row = conn.execute('SELECT * FROM task_publications WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise PublicationBlocked('publication_missing')
        return dict(row)


def save(identifier, state, error=None, **fields):
    assert set(fields) <= {'pr_number','pr_url'}
    with db._connection() as conn:
        conn.execute('UPDATE task_publications SET state=?,error=?' + ''.join(', '+k+'=?' for k in fields) + ' WHERE id=?',
                     (state,error,*fields.values(),identifier))


def authority(record):
    from .continuations import authorization_error
    with db._connection() as conn:
        replacement = conn.execute('SELECT owner_json FROM task_publication_authorities WHERE publication_id=? ORDER BY id DESC LIMIT 1', (record['id'],)).fetchone()
        owner_json = replacement['owner_json'] if replacement else record['owner_json']
        task = tasks.get(record['task_id'])
        if encode(tasks.identity(task)) != record['destination_json']:
            raise PublicationBlocked('publication_destination_changed')
        scope = scope_for(conn, owner_json, task)
        if encode(scope) != record['scope_json']:
            raise PublicationBlocked('publication_scope_changed')
        if not replacement:
            step = conn.execute('SELECT * FROM continuations WHERE id=?', (record['id'],)).fetchone()
            if not step or authorization_error(conn, step):
                raise PublicationBlocked('publication_authority_changed')
        # Reconciliation under a fresh authenticated human owner never revives
        # the old sequence's authority or changes its destination.
        return task, scope


def git(path, *args, env=None, input=None):
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT='0')
    if env:
        environment.update(env)
    return subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','push.followTags=false',*args],
        cwd=path, env=environment, input=input, capture_output=True, text=True, check=True, timeout=45).stdout.strip()


def effect_authority(record):
    from . import continuations
    if not continuations._enabled:
        raise PublicationBlocked('publication_execution_disabled')
    return authority(record)


def remote_head(path, scope, branch):
    output = git(path, 'ls-remote', '--refs', '--', scope['push_url'], 'refs/heads/' + branch)
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1 or lines[0].split()[1] != 'refs/heads/' + branch:
        raise PublicationBlocked('unexpected_remote_head')
    return lines[0].split()[0]


class GitHub:
    """Only PR lookup and creation; Git pushes update existing PR diffs."""
    def repository(self, scope):
        match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?', scope['push_url'])
        if not match:
            raise PublicationBlocked('unsupported_github_remote')
        return match[1]

    def api(self, scope, suffix, body=None):
        repo = self.repository(scope)
        args = ['gh','api','--hostname','github.com','--method','POST' if body else 'GET', 'repos/'+repo+suffix]
        if body:
            args += ['--input','-']
        result = subprocess.run(args, input=json.dumps(body) if body else None, text=True,
                                capture_output=True, check=True, timeout=45)
        return json.loads(result.stdout)

    def find(self, scope, task):
        repo = self.repository(scope)
        query = urlencode(dict(state='all', head=repo.split('/')[0]+':'+task['branch'], base=scope['base_branch'], per_page=100))
        result = self.api(scope, '/pulls?'+query)
        if not isinstance(result, list) or len(result) >= 100:
            raise PublicationBlocked('ambiguous_pr_identity')
        for pr in result:
            self.validate_repository(scope, pr)
        return result

    def validate_repository(self, scope, pr):
        repo = self.repository(scope)
        if (pr.get('base', {}).get('repo', {}).get('full_name') != repo
                or pr.get('head', {}).get('repo', {}).get('full_name') != repo
                or pr.get('html_url') != 'https://github.com/'+repo+'/pull/'+str(pr.get('number'))):
            raise PublicationBlocked('unexpected_pr_identity')

    def create(self, scope, task, title):
        pr = self.api(scope, '/pulls', dict(title=title, head=task['branch'], base=scope['base_branch'],
                                            body=marker(task), maintainer_can_modify=False))
        self.validate_repository(scope, pr)
        return pr


def marker(task):
    return '<!-- squid-task:' + task['id'] + ' -->\nReviewed task changes from base ' + task['base_sha'] + '.'


def validate_pr(pr, scope, task, sha):
    if (not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] <= 0
            or pr.get('state') != 'open' or pr.get('merged_at')
            or pr.get('base', {}).get('ref') != scope['base_branch']
            or pr.get('head', {}).get('ref') != task['branch'] or pr['head'].get('sha') != sha
            or not isinstance(pr.get('body'), str)
            or pr['body'].splitlines().count(marker(task).splitlines()[0]) != 1
            or not isinstance(pr.get('html_url'), str)):
        raise PublicationBlocked('unexpected_pr_identity')
    return pr


def find_published_pr(github, scope, task, record):
    """Allow a short GitHub propagation delay without weakening identity checks."""
    for attempt in range(4):
        prs = github.find(scope, task)
        if len(prs) > 1:
            raise PublicationBlocked('ambiguous_pr_identity')
        stale = False
        if prs:
            sha = prs[0].get('head', {}).get('sha')
            stale = (record['remote_before'] is not None
                     and sha == record['remote_before'] and sha != record['intended_sha'])
            validate_pr(prs[0], scope, task, sha if stale else record['intended_sha'])
            if task['pr_number'] is not None and task['pr_number'] != prs[0]['number']:
                raise PublicationBlocked('unexpected_pr_identity')
        if (prs and not stale) or attempt == 3:
            return prs
        time.sleep(0.25 * (attempt + 1))


def prepare(step, child, task):
    from .worktree import _snapshot_tree, _internal_commit_env
    current = tasks.for_turn(step['parent_id'])
    if not current or tasks.identity(current) != tasks.identity(task):
        raise PublicationBlocked('publication_destination_changed')
    task = current
    with db._connection() as conn:
        scope = scope_for(conn, step['owner_json'], task)
        if conn.execute("SELECT 1 FROM task_publications WHERE task_id=? AND id!=? AND state!='completed'", (task['id'],step['id'])).fetchone():
            raise PublicationBlocked('publication_reconciliation_required')
        reviews = conn.execute("SELECT * FROM task_reviews WHERE task_id=? AND state='passed'", (task['id'],)).fetchall()
    path = task['worktree_path']
    head = git(path, 'rev-parse', 'HEAD')
    tree = _snapshot_tree(Path(path))
    if task['review_required'] or not any(r['tree'] == tree and r['destination_json'] == encode(tasks.identity(task)) for r in reviews):
        raise PublicationBlocked('publication_requires_review')
    if head != (task['published_sha'] or task['base_sha']):
        raise PublicationBlocked('unexpected_task_head')
    if git(path, 'diff', '--name-only', '--diff-filter=U'):
        raise PublicationBlocked('unresolved_task_conflict')
    if git(path, 'write-tree') not in {tree,git(path, 'rev-parse', head+'^{tree}')}:
        raise PublicationBlocked('staged_task_changes_require_reconciliation')
    rewrites = subprocess.run(['git','config','--get-regexp',r'^url\..*\.(insteadof|pushinsteadof)$'], cwd=path, capture_output=True, text=True, timeout=10)
    if rewrites.returncode != 1:
        raise PublicationBlocked('unsupported_url_rewrite')
    before = remote_head(path, scope, task['branch'])
    if before != task['published_sha']:
        raise PublicationBlocked('unexpected_remote_head')
    if tree == git(path, 'rev-parse', head+'^{tree}'):
        if not task['published_sha']:
            raise PublicationBlocked('no_task_changes')
        sha = head
    else:
        env = _internal_commit_env()
        sha = git(path, 'commit-tree', tree, '-p', head, env=env,
                  input='Squid reviewed task '+task['id']+'\n')
    with db._connection() as conn:
        conn.execute('INSERT INTO task_publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)',
            (step['id'],task['id'],child,step['owner_json'],encode(tasks.identity(task)),encode(scope),task['base_sha'],head,tree,sha,before,'prepared'))
    return get(step['id'])


def advance(identifier, *, allow_effects=False, github=None):
    from .worktree import _lock_for
    record = get(identifier)
    with _lock_for(Path(json.loads(record['destination_json'])['worktree_path'])):
        return _advance_locked(identifier, allow_effects=allow_effects, github=github)


def _advance_locked(identifier, *, allow_effects=False, github=None):
    """Reconcile first; only known unperformed effects may be executed."""
    from .worktree import _snapshot_tree
    github = github or GitHub()
    record = get(identifier)
    task, scope = authority(record)
    path, branch = task['worktree_path'], task['branch']
    head = git(path, 'rev-parse', 'HEAD')
    if head not in {record['parent_head'],record['intended_sha']} or _snapshot_tree(Path(path)) != record['tree']:
        raise PublicationBlocked('publication_tree_changed')
    if allow_effects and record['state'] != 'completed' and task['review_required']:
        raise PublicationBlocked('publication_requires_review')
    if head != record['intended_sha']:
        if not allow_effects or record['state'] != 'prepared':
            return record
        effect_authority(record)
        git(path, 'update-ref', 'refs/heads/'+branch, record['intended_sha'], record['parent_head'])
    # Repair a crash between ref promotion and index update without touching files.
    if allow_effects and record['state'] == 'prepared':
        effect_authority(record)
        if git(path, 'write-tree') not in {record['tree'],git(path, 'rev-parse', record['parent_head']+'^{tree}')}:
            raise PublicationBlocked('task_index_changed')
        git(path, 'read-tree', record['intended_sha'])
    if record['state'] == 'prepared' and not allow_effects and git(path, 'write-tree') != record['tree']:
        # Ref promotion can finish before index repair. Keep the prepared state
        # so an authorized resume still repairs the index before publishing.
        return record
    if record['state'] == 'prepared':
        save(identifier, 'committed')
        record = get(identifier)
    authority(record)
    remote = remote_head(path, scope, branch)
    if remote not in {record['remote_before'],record['intended_sha']}:
        raise PublicationBlocked('unexpected_remote_head')
    if remote != record['intended_sha']:
        # The remote still matches the saved pre-push state. Recovery may
        # clear uncertainty, but only an explicit resume may retry the write.
        if record['state'] in {'push_intent', 'push_unknown'}:
            save(identifier, 'committed')
            record = get(identifier)
        if not allow_effects or record['state'] != 'committed':
            return record
        # Check the existing PR before updating its branch. A closed, merged,
        # missing or unrelated PR must never receive another publication push.
        prs = github.find(scope, task)
        if len(prs) > 1:
            raise PublicationBlocked('ambiguous_pr_identity')
        if not prs and task['pr_number'] is not None:
            raise PublicationBlocked('recorded_pr_missing')
        if prs:
            pr = validate_pr(prs[0], scope, task, remote)
            if task['pr_number'] is not None and task['pr_number'] != pr['number']:
                raise PublicationBlocked('unexpected_pr_identity')
        effect_authority(record)
        save(identifier, 'push_intent')
        try:
            git(path, 'push', '--no-follow-tags', '--', scope['push_url'], record['intended_sha']+':refs/heads/'+branch)
        except Exception:
            save(identifier, 'push_unknown', 'push_outcome_unknown')
        authority(record)
        remote = remote_head(path, scope, branch)
        if remote not in {record['remote_before'],record['intended_sha']}:
            raise PublicationBlocked('unexpected_remote_head')
        if remote != record['intended_sha']:
            return get(identifier)
    if record['state'] in {'prepared','committed','push_intent','push_unknown'} or get(identifier)['state'] in {'push_intent','push_unknown'}:
        save(identifier, 'pushed')
    with db._connection() as conn:
        conn.execute('UPDATE task_workspaces SET published_sha=? WHERE id=?', (record['intended_sha'],task['id']))
    authority(record)
    prs = find_published_pr(github, scope, task, record)
    if len(prs) > 1:
        raise PublicationBlocked('ambiguous_pr_identity')
    if not prs:
        if task['pr_number'] is not None:
            raise PublicationBlocked('recorded_pr_missing')
        record = get(identifier)
        # Lookup found no matching PR; read-only recovery never creates one.
        if record['state'] in {'pr_intent', 'pr_unknown'}:
            save(identifier, 'pushed')
            record = get(identifier)
        if not allow_effects or record['state'] != 'pushed':
            return record
        effect_authority(record)
        save(identifier, 'pr_intent')
        try:
            prs = [github.create(scope, task, 'Reviewed task '+task['id'][:12])]
        except Exception:
            save(identifier, 'pr_unknown', 'pr_outcome_unknown')
        authority(record)
        if not prs:
            prs = find_published_pr(github, scope, task, record)
        if not prs:
            return get(identifier)
    if len(prs) != 1:
        raise PublicationBlocked('ambiguous_pr_identity')
    pr = validate_pr(prs[0], scope, task, record['intended_sha'])
    if task['pr_number'] is not None and task['pr_number'] != pr['number']:
        raise PublicationBlocked('unexpected_pr_identity')
    authority(record)
    # Confirm both the Git transport and GitHub view before recording completion.
    if remote_head(path, scope, branch) != record['intended_sha']:
        raise PublicationBlocked('unexpected_remote_head')
    save(identifier, 'completed', pr_number=pr['number'], pr_url=pr['html_url'])
    with db._connection() as conn:
        conn.execute('UPDATE task_workspaces SET pr_number=?,pr_url=? WHERE id=?', (pr['number'],pr['html_url'],task['id']))
    return get(identifier)


def finish(record):
    from .worktree import _lock_for
    path = Path(json.loads(record['destination_json'])['worktree_path'])
    with _lock_for(path):
        # A concurrent reconciliation may have completed since this caller's
        # snapshot. Never overwrite its confirmed outcome with an older one.
        _finish_locked(get(record['id']))


def _finish_locked(record):
    from . import continuations as c
    done = record['state'] == 'completed'
    committed = record['state'] != 'prepared'
    if not committed:
        try:
            head = tasks._git(json.loads(record['destination_json'])['worktree_path'], 'rev-parse', 'HEAD')
            committed = True if head == record['intended_sha'] else False if head == record['parent_head'] else None
        except RuntimeError:
            committed = None
    pushed = None if record['state'] in {'push_intent','push_unknown'} else record['state'] in {'pushed','pr_intent','pr_unknown','completed'}
    content = encode(dict(publication=record['state'], committed=committed, commit=record['intended_sha'] if committed else None,
                          intended_commit=record['intended_sha'], pushed=pushed,
                          pr=record['pr_url'], error=record['error']))
    db.update_assistant_message(record['child_id'], content, None, 'done' if done else 'error')
    event_type, payload = ('done', None) if done else ('error', 'task_publication_partial')
    with db._connection() as conn:
        last = conn.execute('SELECT seq,event_type,payload FROM run_events WHERE msg_id=? ORDER BY seq DESC LIMIT 1',
                            (record['child_id'],)).fetchone()
    # Reconciliation appends a terminal outcome after the original failure;
    # repeated observations of the same outcome do not duplicate events.
    if not last or (last['event_type'], last['payload']) != (event_type, payload):
        db.insert_run_event(record['child_id'], last['seq'] + 1 if last else 4, event_type, payload)
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET result_success=? WHERE id=?', (int(done),record['id']))
    c.complete(record['child_id'], done, 'publication_completed' if done else 'publication_partial')
    # Read-only reconciliation may resolve an old ambiguous result. Publication
    # is a terminal step, so this must never append or revive more work.
    if done:
        with db._connection() as conn:
            conn.execute("UPDATE continuations SET state='successful',reason='publication_reconciled' WHERE id=? AND mode='task_publication'", (record['id'],))


def execute(step, *, github=None):
    from . import continuations as c
    task = tasks.for_turn(step['parent_id'])
    if not task:
        raise PublicationBlocked('publication_requires_task')
    user = db.insert_user_message(step['topic'], step['agent'], 'Publish reviewed task', source='continuation')
    child = db.insert_assistant_message(step['topic'], step['agent'], user, source='continuation', adhoc=True)
    tasks.bind(child, task['id'])
    if not c.link(step['id'], child):
        db.mark_assistant_cancelled(child, 'Publication no longer claimed')
        return
    from .worktree import _lock_for
    with _lock_for(Path(task['worktree_path'])):
        try:
            record = prepare(step, child, task)
            record = _advance_locked(record['id'], allow_effects=True, github=github)
        except Exception as exc:
            with db._connection() as conn:
                exists = conn.execute('SELECT 1 FROM task_publications WHERE id=?', (step['id'],)).fetchone()
            if not exists:
                db.update_assistant_message(child, 'Task publication blocked: '+type(exc).__name__, None, 'error')
                c.complete(child, False, 'publication_preparation_blocked')
                return
            record = get(step['id'])
            save(record['id'], record['state'], str(exc) if isinstance(exc, PublicationBlocked) else 'publication_failed')
            record = get(step['id'])
        _finish_locked(record)


def recover():
    with db._connection() as conn:
        identifiers = [r[0] for r in conn.execute("SELECT p.id FROM task_publications p JOIN continuations c ON c.id=p.id WHERE c.state IN ('claimed','running','ambiguous')")]
    for identifier in identifiers:
        try:
            record = advance(identifier)  # Observe only. Never replay a write on restart.
            # Confirmed commits/pushes remain useful evidence even when the
            # remaining publication needs explicit human reconciliation.
            finish(record)
        except Exception:
            log.warning('Task publication recovery failed: %s', identifier, exc_info=True)
