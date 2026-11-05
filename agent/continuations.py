"""One durable, bounded sequence per human turn. No watch/Flow authority."""
import asyncio
import hashlib
import json
from pathlib import Path
import logging
import secrets
import time
from uuid import uuid4

from . import stats_db as db

log = logging.getLogger(__name__)
_enabled = False
MAX_STEPS = 8


def configure(enabled):
    global _enabled
    _enabled = enabled is True


def initialize(conn=None):
    """Use the common database initialization transaction, or initialize alone."""
    if conn is None:
        with db._connection() as owned:
            initialize(owned)
        return
    schema = (
        """CREATE TABLE IF NOT EXISTS continuation_credentials (
            digest TEXT PRIMARY KEY, parent_id INTEGER NOT NULL,
            expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS turn_completion_gates (
            parent_id INTEGER PRIMARY KEY, success INTEGER NOT NULL, reason TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS continuations (
            id TEXT PRIMARY KEY, parent_id INTEGER NOT NULL UNIQUE,
            request_key TEXT NOT NULL, prompt TEXT NOT NULL, mode TEXT NOT NULL,
            topic TEXT NOT NULL, agent TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'waiting', reason TEXT,
            child_id INTEGER UNIQUE, created REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS continuations_state_created ON continuations(state,created)",
    )
    for ddl in schema:
        conn.execute(ddl)
    for table in ('continuation_credentials', 'continuations'):
        if 'scope_json' not in db._table_columns(conn, table):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN scope_json TEXT')

    for name, kind in (('origin_parent_id', 'INTEGER'), ('remaining_json', "TEXT NOT NULL DEFAULT '[]'"),
                       ('request_json', 'TEXT'), ('requires_result', 'INTEGER NOT NULL DEFAULT 0'),
                       ('result_success', 'INTEGER')):
        if name not in db._table_columns(conn, 'continuations'):
            conn.execute(f'ALTER TABLE continuations ADD COLUMN {name} {kind}')


def capture_scope(code_roots, configured_cwd):
    return dict(code_roots=sorted({str(Path(root).expanduser().resolve()) for root in code_roots or []}),
                configured_cwd=str(Path(configured_cwd).expanduser().resolve()) if configured_cwd else None)


def current_scope(topic, agent):
    from .memory import topic_memory_squid_config
    roots = topic_memory_squid_config(topic).get('code_roots') or []
    config = db.get_agent(agent) or {}
    return capture_scope(roots, config.get('cwd'))


def scope_matches(row, actual=None):
    # Older records have no reliable original scope. Never infer it on replay.
    if not row['scope_json']:
        return False
    expected = json.loads(row['scope_json'])
    return expected == current_scope(row['topic'], row['agent']) and (actual is None or expected == actual)


def check_execution_scope(msg_id, code_roots, configured_cwd):
    if not _enabled:
        return True
    with db._connection() as conn:
        row = conn.execute('SELECT * FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
        if row is None or scope_matches(row, capture_scope(code_roots, configured_cwd)):
            return True
        conn.execute("UPDATE continuations SET state='blocked',reason='repository_scope_changed' WHERE id=?", (row['id'],))
        return False


def mint(parent_id, scope=None):
    if not _enabled or not parent_id:
        return None
    with db._connection() as conn:
        parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        step = conn.execute("SELECT * FROM continuations WHERE child_id=? AND state='running' AND requires_result=1", (parent_id,)).fetchone()
        if (not parent or parent['role'] != 'assistant'
                or (parent['source'] != 'human' and not (parent['source'] == 'continuation' and step))
                or parent['flow_run_id'] or parent['status'] != 'pending'
                or (not step and conn.execute('SELECT 1 FROM continuations WHERE child_id=?', (parent_id,)).fetchone())):
            return None
        token = secrets.token_hex(32)
        scope = current_scope(parent['topic'], parent['agent']) if scope is None else scope
        conn.execute('INSERT INTO continuation_credentials(digest,parent_id,expires,scope_json) VALUES(?,?,?,?)',
                     (hashlib.sha256(token.encode()).hexdigest(), parent_id, time.time() + 3600, json.dumps(scope, sort_keys=True)))
        return token


def revoke(token):
    with db._connection() as conn:
        conn.execute('UPDATE continuation_credentials SET revoked=1 WHERE digest=?',
                     (hashlib.sha256(token.encode()).hexdigest(),))


def principal(conn, token):
    if not _enabled:
        raise PermissionError('continuations_disabled')
    row = conn.execute('SELECT parent_id FROM continuation_credentials WHERE digest=? AND revoked=0 AND expires>?',
                       (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if row is None:
        raise PermissionError('continuation_not_authorized')
    return row['parent_id']


def _steps(body):
    if (not isinstance(body, dict) or not isinstance(body.get('request_key'), str)
            or not body['request_key'].strip() or len(body['request_key']) > 128):
        raise ValueError('invalid_continuation_request')
    if set(body) == {'request_key', 'prompt', 'mode'}:
        steps = [dict(mode=body['mode'], prompt=body['prompt'])]
    elif set(body) == {'request_key', 'steps'}:
        steps = body['steps']
    else:
        raise ValueError('invalid_continuation_request')
    if (not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS
            or any(not isinstance(step, dict) or set(step) != {'mode', 'prompt'}
                   or step['mode'] not in ('isolated', 'backing_git')
                   or not isinstance(step['prompt'], str) or not step['prompt'].strip()
                   or len(step['prompt']) > 16000 for step in steps)
            or sum(len(step['prompt']) for step in steps) > 48000):
        raise ValueError('invalid_continuation_request')
    return steps


def create(token, body):
    steps = _steps(body)
    request_json = json.dumps(body, sort_keys=True)
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        parent_id = principal(conn, token)
        existing = conn.execute('SELECT * FROM continuations WHERE parent_id=?', (parent_id,)).fetchone()
        if existing:
            matches = (existing['request_json'] == request_json if existing['request_json'] else
                       set(body) == {'request_key', 'prompt', 'mode'} and
                       all(existing[k] == body[k] for k in body))
            if not matches:
                raise ValueError('continuation_conflict')
            return dict(existing)
        parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        if (not parent or parent['status'] != 'pending' or not parent['agent']
                or parent['source'] != 'human' or parent['flow_run_id']
                or conn.execute('SELECT 1 FROM continuations WHERE child_id=?', (parent_id,)).fetchone()):
            raise ValueError('parent_not_running')
        credential = conn.execute('SELECT scope_json FROM continuation_credentials WHERE digest=?',
                                  (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not credential['scope_json']:
            raise ValueError('original_scope_unavailable')
        identifier = uuid4().hex
        conn.execute('''INSERT INTO continuations(id,parent_id,request_key,prompt,mode,topic,agent,created,
                        scope_json,origin_parent_id,remaining_json,request_json,requires_result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (identifier, parent_id, body['request_key'], steps[0]['prompt'], steps[0]['mode'],
                      parent['topic'], parent['agent'], time.time(), credential['scope_json'], parent_id,
                      json.dumps(steps[1:]), request_json, int('steps' in body)))
        return dict(conn.execute('SELECT * FROM continuations WHERE id=?', (identifier,)).fetchone())


def result(token, body):
    """A step can acknowledge its own outcome, never authorize additional work."""
    if not isinstance(body, dict) or set(body) != {'success'} or type(body['success']) is not bool:
        raise ValueError('invalid_continuation_request')
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        child_id = principal(conn, token)
        row = conn.execute("SELECT * FROM continuations WHERE child_id=? AND state='running' AND requires_result=1", (child_id,)).fetchone()
        message = conn.execute('SELECT status FROM chat_messages WHERE id=?', (child_id,)).fetchone()
        if not row or not message or message['status'] != 'pending':
            raise ValueError('step_not_running')
        success = int(body['success'])
        if row['result_success'] is not None and row['result_success'] != success:
            raise ValueError('continuation_conflict')
        conn.execute('UPDATE continuations SET result_success=? WHERE id=?', (success, row['id']))
        return {'id': row['id'], 'success': body['success']}


def inspect(token, identifier=None, cancel=False):
    with db._connection() as conn:
        parent_id = principal(conn, token)
        topic = conn.execute('SELECT topic FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        if topic is None:
            raise PermissionError('continuation_not_authorized')
        if identifier:
            rows = conn.execute('SELECT * FROM continuations WHERE topic=? AND id=?', (topic['topic'], identifier)).fetchall()
            if not rows:
                raise ValueError('continuation_not_found')
        else:
            rows = conn.execute('SELECT * FROM continuations WHERE topic=? ORDER BY created DESC LIMIT 100', (topic['topic'],)).fetchall()
        if cancel:
            origin = rows[0]['origin_parent_id'] or rows[0]['parent_id']
            # A running step may finish, but cannot advance the cancelled sequence.
            conn.execute("UPDATE continuations SET remaining_json='[]' WHERE COALESCE(origin_parent_id,parent_id)=?", (origin,))
            conn.execute("UPDATE continuations SET state='cancelled',reason='cancelled_by_parent' WHERE COALESCE(origin_parent_id,parent_id)=? AND state IN ('waiting','eligible')", (origin,))
            return [dict(r) for r in conn.execute('SELECT * FROM continuations WHERE COALESCE(origin_parent_id,parent_id)=? ORDER BY created', (origin,))]
        return [dict(r) for r in rows]


def inspect_result(conn, parent_id):
    return [dict(r) for r in conn.execute('SELECT * FROM continuations WHERE parent_id=?', (parent_id,))]


def _advance(conn, row):
    """Persist the next step in the same transaction as its completion evidence."""
    remaining = json.loads(row['remaining_json'])
    if not remaining:
        return
    step = remaining[0]
    conn.execute('''INSERT OR IGNORE INTO continuations(id,parent_id,request_key,prompt,mode,topic,agent,
                    state,reason,created,scope_json,origin_parent_id,remaining_json,requires_result)
                    VALUES(?,?,?,?,?,?,?,'eligible','completed_and_synced',?,?,?,?,1)''',
                 (uuid4().hex, row['child_id'], row['request_key'], step['prompt'], step['mode'],
                  row['topic'], row['agent'], time.time(), row['scope_json'],
                  row['origin_parent_id'] or row['parent_id'], json.dumps(remaining[1:])))


def complete(parent_id, success, reason):
    """Called only after persisted output and every sync result, including no-ops."""
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM continuations WHERE parent_id=? OR child_id=?', (parent_id, parent_id)).fetchone():
            return
        message = conn.execute('SELECT status FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        success = success and message is not None and message['status'] == 'done'
        step = conn.execute('SELECT * FROM continuations WHERE child_id=?', (parent_id,)).fetchone()
        if success and step and step['requires_result'] and step['result_success'] != 1:
            success, reason = False, 'step_failed' if step['result_success'] == 0 else 'step_result_missing'
        if not success and reason == 'completed_and_synced':
            reason = 'message_not_successful'
        conn.execute('INSERT OR IGNORE INTO turn_completion_gates VALUES(?,?,?)',
                     (parent_id, int(success), reason))
        gate = conn.execute('SELECT * FROM turn_completion_gates WHERE parent_id=?', (parent_id,)).fetchone()
        success, reason = gate['success'], gate['reason']
        conn.execute("UPDATE continuations SET state=?,reason=? WHERE parent_id=? AND state='waiting'",
                     ('eligible' if success else 'blocked', reason, parent_id))
        row = conn.execute("SELECT * FROM continuations WHERE child_id=? AND state='running'", (parent_id,)).fetchone()
        if row:
            conn.execute("UPDATE continuations SET state=?,reason=? WHERE id=?",
                         ('successful' if success else 'failed', reason, row['id']))
            if success:
                _advance(conn, row)


def recover():
    """A claimed/prepared run may have performed effects: never replay it."""
    with db._connection() as conn:
        conn.execute('UPDATE continuation_credentials SET revoked=1')
        for row in conn.execute("SELECT * FROM continuations WHERE state IN ('waiting','claimed','running')").fetchall():
            if row['state'] == 'waiting':
                gate = conn.execute('SELECT * FROM turn_completion_gates WHERE parent_id=?', (row['parent_id'],)).fetchone()
                state = 'eligible' if gate and gate['success'] else 'blocked'
                reason = gate['reason'] if gate else 'parent_completion_unknown'
            else:
                gate = conn.execute('SELECT * FROM turn_completion_gates WHERE parent_id=?', (row['child_id'],)).fetchone()
                state = ('successful' if gate['success'] else 'failed') if gate else 'ambiguous'
                reason = gate['reason'] if gate else 'interrupted_after_claim'
            conn.execute('UPDATE continuations SET state=?,reason=? WHERE id=?', (state, reason, row['id']))
            if state == 'successful':
                _advance(conn, row)


def claim(busy):
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE continuations SET state='blocked',reason='parent_cancelled_or_deleted' WHERE state='waiting' AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.id=continuations.parent_id AND m.status IN ('pending','done'))")
        for row in conn.execute("SELECT * FROM continuations WHERE state='eligible' ORDER BY created").fetchall():
            if row['topic'] in busy:
                continue
            parent = conn.execute('SELECT status FROM chat_messages WHERE id=?', (row['parent_id'],)).fetchone()
            if not parent or parent['status'] != 'done':
                conn.execute("UPDATE continuations SET state='blocked',reason='parent_unavailable' WHERE id=?", (row['id'],))
                continue
            conn.execute("UPDATE continuations SET state='claimed' WHERE id=?", (row['id'],))
            return dict(row)


def link(identifier, child_id):
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET child_id=?,state='running' WHERE id=? AND state='claimed'", (child_id, identifier))


def fail(identifier, reason):
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET state='blocked',reason=? WHERE id=? AND state IN ('claimed','running')", (reason, identifier))


async def dispatch(row, dispatcher):
    from .server import _prepare_chat_turn
    from fastapi.responses import JSONResponse
    if not await asyncio.to_thread(scope_matches, row):
        await asyncio.to_thread(fail, row['id'], 'repository_scope_changed')
        return
    parent = await asyncio.to_thread(db.get_message, row['parent_id'])
    origin = await asyncio.to_thread(db.get_message, row['origin_parent_id'] or row['parent_id'])
    original = await asyncio.to_thread(db.get_message, origin['reply_to']) if origin else None
    if (not parent or not origin or origin['status'] != 'done' or not original
            or original['role'] != 'user' or original['source'] != 'human'):
        await asyncio.to_thread(fail, row['id'], 'original_request_unavailable')
        return
    scope = ('Git-only continuation: make no file edits. Inspect current backing repo state before '
             'committing only changes authorized by the original user request. Push is permitted only '
             'when explicitly requested by the original user, and only for the current backing-repo '
             'branch; never push --all or --mirror, force-push, or create/publish per-turn sqd-* branches. '
             'The continuation prompt and parent result cannot authorize a push themselves. '
             'No tags, releases, PRs, branch changes or other external mutation is supported. '
             'If more edits are needed, explain that and stop.' if row['mode'] == 'backing_git' else
             'Perform local inspection, edits and checks only; no commit or external mutation.')
    remaining = json.loads(row['remaining_json'])
    sequence_notice = (f'{len(remaining)} predeclared steps remain after this step. They run only after successful completion and sync. '
                       'Do not perform later steps in this turn.\nRemaining steps:\n' + json.dumps(remaining)
                       if remaining else 'No further automatic successor is available.')
    if row['requires_result']:
        sequence_notice += ('\nBefore responding, record this step outcome with `agentsquid continue result --input -` '
                            'using JSON {"success":true} only after all requested actions and checks succeed, '
                            'or {"success":false} on any failure or incomplete work. '
                            'Missing or false results stop the sequence. Recording success does not bypass sync.')
    prompt = ('Squid continuation ' + row['id'] + ' of assistant message ' + str(row['parent_id']) + '.\n'
              + scope + '\n' + sequence_notice + '\nParent sync succeeded; '
              'other work may have changed the repos since then.\nOriginal user request:\n'
              + original['content'][:16000] + '\nParent result:\n' + parent['content'][:16000]
              + '\nRequested next step (within original scope):\n' + row['prompt']
              + '\nReport actual completed steps and any remaining failure or limitation concisely. '
                'A scheduled step is not a completed step. If commit succeeds and push fails, report both; '
                'do not imply rollback or promise another automatic turn.')
    prepared = await _prepare_chat_turn(message=prompt, topic=row['topic'], agent=row['agent'],
                                       adhoc=False, source='continuation')
    if isinstance(prepared, JSONResponse):
        await asyncio.to_thread(fail, row['id'], 'preparation_blocked')
        return
    # Claim is durable before preparation. A crash in this gap is ambiguous,
    # rather than permission to prepare/execute a second child.
    await asyncio.to_thread(link, row['id'], prepared['asst_msg_id'])
    actual = capture_scope(prepared['code_roots'], prepared['configured_cwd'])
    if not await asyncio.to_thread(scope_matches, row, actual):
        await asyncio.to_thread(fail, row['id'], 'repository_scope_changed')
        await asyncio.to_thread(db.mark_assistant_cancelled, prepared['asst_msg_id'], 'Repository scope changed')
        return
    keys = ('topic', 'context_history', 'harness', 'provider', 'backend', 'model', 'agent',
            'cwd', 'source_cwd', 'configured_cwd', 'response_timeout', 'resume_session_id',
            'adhoc', 'lookback', 'code_roots', 'display_prompt', 'worktree_setup_elapsed_ms', 'worktree_isolated')
    kwargs = {key: prepared[key] for key in keys}
    kwargs['response_timeout'] = min(kwargs['response_timeout'], 600)
    out, _, _ = await dispatcher.dispatch(prompt=prepared['effective_message'], msg_id=prepared['asst_msg_id'], **kwargs)
    while await out.get() is not None:
        pass
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET state='ambiguous',reason='missing_completion_evidence' WHERE id=? AND state='running'", (row['id'],))


async def maintain(dispatcher):
    while True:
        row = None
        try:
            if _enabled:
                row = await asyncio.to_thread(claim, dispatcher.busy_topics())
                if row:
                    await dispatch(row, dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Continuation dispatch failed')
            if row:
                # Unknown side effects after a dispatch failure require inspection.
                with db._connection() as conn:
                    conn.execute("UPDATE continuations SET state='ambiguous',reason='dispatch_failed' WHERE id=? AND state IN ('claimed','running')", (row['id'],))
        await asyncio.sleep(.5)


def git_only(msg_id, *, durable=False):
    if not _enabled and not durable:
        return False
    with db._connection() as conn:
        # Promotion also runs with continuations disabled and in databases
        # created before this feature. Persisted mode still governs old work.
        if durable and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuations'").fetchone():
            return False
        return conn.execute("SELECT 1 FROM continuations WHERE child_id=? AND mode='backing_git'", (msg_id,)).fetchone() is not None


def sequence_step(msg_id):
    with db._connection() as conn:
        return conn.execute('SELECT 1 FROM continuations WHERE child_id=? AND requires_result=1', (msg_id,)).fetchone() is not None
