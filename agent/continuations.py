"""Durable bounded sequences with inherited human, watch and Flow ownership."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import logging
import math
import secrets
import time
from uuid import uuid4

from . import stats_db as db
from . import continuation_owners as owners
from .watch_async import call, settle

log = logging.getLogger(__name__)
_enabled = False
MAX_STEPS = 8


@contextmanager
def _connection():
    """Wake realtime readers only after the domain/outbox transaction commits."""
    event_id = None
    with db._connection() as conn:
        before = conn.total_changes
        cursor = conn.execute('SELECT COALESCE(MAX(event_id),0) FROM realtime_events').fetchone()[0]
        yield conn
        if conn.total_changes != before:
            event_id = conn.execute("""SELECT MAX(event_id) FROM realtime_events
                WHERE event_type='continuation.changed' AND event_id>?""", (cursor,)).fetchone()[0]
    if event_id is not None:
        try:
            db._notify_realtime_commit(event_id)
        except Exception:
            # Domain state and its outbox row have already committed. A failed
            # socket wake-up must not strand a claim or misreport an acceptance.
            log.exception('Continuation realtime notification failed event_id=%s', event_id)


def configure(enabled):
    global _enabled
    _enabled = enabled is True


def initialize(conn=None):
    """Use the common database initialization transaction, or initialize alone."""
    if conn is None:
        with db._connection() as owned:
            initialize(owned)
        return
    owners.initialize(conn)
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
        if 'owner_json' not in db._table_columns(conn, table):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN owner_json TEXT')

    for name, kind in (('origin_parent_id', 'INTEGER'), ('remaining_json', "TEXT NOT NULL DEFAULT '[]'"),
                       ('request_json', 'TEXT'), ('requires_result', 'INTEGER NOT NULL DEFAULT 0'),
                       ('result_success', 'INTEGER'), ('deadline_at', 'REAL'),
                       ('cancel_requested', 'INTEGER NOT NULL DEFAULT 0')):
        if name not in db._table_columns(conn, 'continuations'):
            conn.execute(f'ALTER TABLE continuations ADD COLUMN {name} {kind}')

    # Triggers cover every writer, including host publication reconciliation.
    # Payloads deliberately exclude prompts, credentials, and repository scope.
    for operation in ('INSERT', 'UPDATE'):
        condition = "" if operation == 'INSERT' else """WHEN
            OLD.state IS NOT NEW.state OR OLD.reason IS NOT NEW.reason
            OR OLD.child_id IS NOT NEW.child_id
            OR OLD.cancel_requested IS NOT NEW.cancel_requested
            OR OLD.result_success IS NOT NEW.result_success"""
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS continuation_changed_{operation.lower()}
            AFTER {operation} ON continuations {condition}
            BEGIN
                INSERT INTO realtime_events(event_type,topic,agent,msg_id,payload)
                VALUES('continuation.changed',NEW.topic,NEW.agent,NEW.parent_id,
                    json_object('id',NEW.id,'parent_id',NEW.parent_id,
                        'origin_parent_id',COALESCE(NEW.origin_parent_id,NEW.parent_id),
                        'child_id',NEW.child_id,'mode',NEW.mode,'state',NEW.state,
                        'reason',NEW.reason,'cancel_requested',NEW.cancel_requested,
                        'result_success',NEW.result_success));
            END""")

    # Replace the prior definition as part of the same schema transaction.
    conn.execute('DROP TRIGGER IF EXISTS continuation_history_deleted')
    conn.execute("""CREATE TRIGGER continuation_history_deleted
        BEFORE DELETE ON chat_messages
        BEGIN
            UPDATE continuations SET cancel_requested=1,remaining_json='[]',
                reason=CASE WHEN state IN ('waiting','eligible','claimed','running')
                    THEN 'history_deleted' ELSE reason END,
                state=CASE WHEN state IN ('claimed','running') THEN 'ambiguous'
                    WHEN state IN ('waiting','eligible') THEN 'cancelled' ELSE state END
            WHERE COALESCE(origin_parent_id,parent_id) IN (
                SELECT COALESCE(origin_parent_id,parent_id) FROM continuations
                WHERE parent_id=OLD.id OR child_id=OLD.id OR origin_parent_id=OLD.id
                    OR parent_id IN (SELECT id FROM chat_messages WHERE reply_to=OLD.id)
                    OR child_id IN (SELECT id FROM chat_messages WHERE reply_to=OLD.id)
            ) AND COALESCE(origin_parent_id,parent_id) IN (
                SELECT COALESCE(origin_parent_id,parent_id) FROM continuations
                WHERE state IN ('waiting','eligible','claimed','running')
            );
        END""")


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
    # Disabling new continuations must not bypass persisted child authority.
    with _connection() as conn:
        from . import routes
        if not routes.execution_allowed(conn, msg_id, capture_scope(code_roots, configured_cwd)):
            return False
        row = conn.execute('SELECT * FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
        if row is None:
            return True
        if row['state'] != 'running':
            return False
        child = conn.execute('SELECT status FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
        if child is None or child['status'] != 'pending':
            conn.execute("UPDATE continuations SET state='blocked',reason='child_unavailable' WHERE id=?", (row['id'],))
            return False
        reason = authorization_error(conn, row, capture_scope(code_roots, configured_cwd))
        if reason:
            conn.execute("UPDATE continuations SET state='blocked',reason=? WHERE id=?", (reason, row['id']))
        return reason is None


def authorization_error(conn, row, actual=None):
    row = dict(row)
    if row.get('cancel_requested', 0):
        return 'sequence_cancelled'
    if not owners.valid(conn, row['owner_json'], topic=row['topic'], agent=row['agent']):
        return 'owner_authorization_changed'
    deadline = row.get('deadline_at')
    owner = json.loads(row['owner_json'] or '{}')
    clock = owners._service.clock if owner.get('kind') == 'watch' and owners._service else time.time
    if deadline is not None and clock() >= deadline:
        return 'sequence_deadline'
    if owner['kind'] != 'human' and row.get('mode', 'isolated') not in {'isolated','task_review','task_publication'}:
        return 'owner_mode_not_authorized'
    if row.get('mode') == 'task_publication':
        from . import task_publication, task_workspaces
        try:
            task = task_workspaces.for_turn(row['parent_id'])
            if not task:
                return 'publication_requires_task'
            task_publication.scope_for(conn, row['owner_json'], task)
        except (RuntimeError, ValueError, PermissionError):
            return 'publication_not_authorized'
    if owner['kind'] == 'watch' and row.get('state') == 'eligible':
        from .watch_runs import pending_user_work
        # Human work wins admission. Once claimed, let the admitted step
        # finish; later human messages must not interrupt publication effects.
        if pending_user_work(conn, row['topic']):
            return 'user_work_priority'
    if not owners.successors_enabled(row['owner_json']):
        return 'owner_successors_disabled'
    if not scope_matches(row, actual):
        return 'repository_scope_changed'
    return None


def child_owner(msg_id):
    with _connection() as conn:
        row = conn.execute('SELECT owner_json FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
        return json.loads(row['owner_json']) if row else None


def sequence_outcome(conn, parent_id):
    """Read only durable descendant evidence, never message text or model claims."""
    rows = conn.execute('SELECT * FROM continuations WHERE COALESCE(origin_parent_id,parent_id)=?',
                        (parent_id,)).fetchall()
    from .routes import outcome
    route_outcome = outcome(conn, parent_id)
    if route_outcome and route_outcome[0] != 'succeeded':
        return route_outcome
    if not rows:
        return 'succeeded', 'no_descendants'
    if any(r['state'] == 'ambiguous' for r in rows):
        return 'ambiguous', 'descendant_ambiguous'
    if any(r['state'] in {'running', 'claimed'} for r in rows):
        return 'pending', 'descendants_running'
    if any(r['cancel_requested'] or r['state'] in {'failed', 'blocked', 'cancelled'} for r in rows):
        return 'failed', 'descendant_failed'
    if any(r['state'] in {'waiting', 'eligible'} for r in rows):
        return 'pending', 'descendants_pending'
    # A successful final row must exhaust the declared plan. Missing an
    # advance after a crash cannot manufacture completion evidence.
    # Wall clocks can move backwards or give several records the same time.
    # Only the saved parent/child links establish the order of execution.
    by_parent = {r['parent_id']: r for r in rows}
    cursor, visited, final = parent_id, set(), None
    while cursor in by_parent and cursor not in visited:
        visited.add(cursor)
        final = by_parent[cursor]
        cursor = final['child_id']
    if (len(visited) != len(rows) or cursor in visited or final is None
            or final['child_id'] is None or final['state'] != 'successful'
            or json.loads(final['remaining_json'])):
        return 'ambiguous', 'descendant_completion_unverified'
    return 'succeeded', 'descendants_done'


def sequence_owner_valid(conn, parent_id, *, service=None):
    """Recheck the frozen owner before acknowledging or releasing dependencies."""
    root = conn.execute('SELECT owner_json,topic,agent,deadline_at FROM continuations WHERE parent_id=?', (parent_id,)).fetchone()
    if root is None:
        return True
    if not owners.valid(conn, root['owner_json'], topic=root['topic'], agent=root['agent'], service=service):
        return False
    owner = json.loads(root['owner_json'])
    watch_service = service or owners._service
    now = watch_service.clock() if owner['kind'] == 'watch' and watch_service else time.time()
    return root['deadline_at'] is None or now < root['deadline_at']


def stop_sequence(parent_id, reason):
    """Freeze successors and preserve evidence of possibly executed effects."""
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('SELECT * FROM continuations WHERE COALESCE(origin_parent_id,parent_id)=?', (parent_id,)).fetchall()
        for row in rows:
            if row['state'] in {'waiting', 'eligible', 'claimed', 'running'}:
                state = 'ambiguous' if row['state'] in {'claimed', 'running'} else 'blocked'
                conn.execute("UPDATE continuations SET state=?,reason=?,cancel_requested=1 WHERE id=?", (state, reason, row['id']))
        return [r['child_id'] for r in rows if r['child_id'] and r['state'] in {'claimed', 'running'}]


def _descendant_status(parent_id):
    """Authorization can inspect Git and must run outside the event loop."""
    with _connection() as conn:
        outcome = sequence_outcome(conn, parent_id)
        rows = conn.execute("SELECT * FROM continuations WHERE COALESCE(origin_parent_id,parent_id)=? AND state IN ('waiting','eligible','claimed','running')", (parent_id,)).fetchall()
        reason = next((error for row in rows if (error := authorization_error(conn, row))), None)
    return outcome, reason


async def wait_for_descendants(parent_id, *, interrupted=False):
    """Wait on the existing continuation scheduler; this never claims work."""
    while True:
        outcome, reason = await asyncio.to_thread(_descendant_status, parent_id)
        if outcome[0] != 'pending':
            return outcome
        if interrupted or reason or not _enabled:
            children = await asyncio.to_thread(stop_sequence, parent_id, reason or 'descendants_interrupted')
            from .runners import kill_proc_by_msg_id
            for child in children:
                await asyncio.to_thread(db.mark_assistant_cancelled, child, reason or 'Descendants interrupted')
                kill_proc_by_msg_id(child)
            with _connection() as conn:
                return sequence_outcome(conn, parent_id)
        await asyncio.sleep(.1)


def launch_timeout(msg_id, timeout, watch_run_id=None, now=None):
    """Every retry uses the remaining absolute budget, never a renewed budget."""
    with _connection() as conn:
        step = conn.execute('SELECT deadline_at,owner_json FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
        owner = json.loads(step['owner_json']) if step else {}
        deadline = step['deadline_at'] if step else None
        watch_run_id = watch_run_id or owner.get('run_id')
        if watch_run_id:
            run = conn.execute('SELECT deadline_at FROM watch_runs WHERE id=?', (watch_run_id,)).fetchone()
            if run and run['deadline_at'] is not None:
                deadline = min(deadline, run['deadline_at']) if deadline is not None else run['deadline_at']
    from .routes import timeout as route_timeout
    with _connection() as conn:
        timeout = route_timeout(conn, msg_id, timeout)
    if deadline is None:
        return timeout
    if now is None:
        clock = owners._service.clock if watch_run_id and owners._service else time.time
        now = clock()
    if deadline <= now:
        raise RuntimeError('Sequence deadline expired before execution')
    from .config import RESPONSE_TIMEOUT
    return min(timeout if timeout is not None else RESPONSE_TIMEOUT, deadline - now)


def mint(parent_id, scope=None, response_timeout=None):
    if not _enabled or not parent_id:
        return None
    from .config import RESPONSE_TIMEOUT
    lifetime = RESPONSE_TIMEOUT if response_timeout is None else response_timeout
    if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 0:
        return None
    with _connection() as conn:
        parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        step = conn.execute("SELECT * FROM continuations WHERE child_id=? AND state='running' AND requires_result=1", (parent_id,)).fetchone()
        if (not parent or parent['role'] != 'assistant'
                or parent['status'] != 'pending'
                or (not step and conn.execute('SELECT 1 FROM continuations WHERE child_id=?', (parent_id,)).fetchone())):
            return None
        owner_json = step['owner_json'] if step else owners.record(conn, parent_id)
        if (not owners.successors_enabled(owner_json)
                or not owners.valid(conn, owner_json, topic=parent['topic'], agent=parent['agent'])):
            return None
        token = secrets.token_hex(32)
        scope = current_scope(parent['topic'], parent['agent']) if scope is None else scope
        conn.execute('INSERT INTO continuation_credentials(digest,parent_id,expires,scope_json,owner_json) VALUES(?,?,?,?,?)',
                     (hashlib.sha256(token.encode()).hexdigest(), parent_id, time.time() + lifetime + 60,
                      json.dumps(scope, sort_keys=True), owner_json))
        return token


def revoke(token):
    with _connection() as conn:
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
                   or step['mode'] not in ('isolated', 'backing_git', 'task_review', 'task_publication')
                   or not isinstance(step['prompt'], str) or not step['prompt'].strip()
                   or len(step['prompt']) > 16000 for step in steps)
            or sum(len(step['prompt']) for step in steps) > 48000):
        raise ValueError('invalid_continuation_request')
    if any(step['mode'] == 'task_publication' for step in steps[:-1]):
        raise ValueError('invalid_continuation_request')
    return steps


def create(token, body):
    steps = _steps(body)
    request_json = json.dumps(body, sort_keys=True)
    with _connection() as conn:
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
        from . import routes
        if conn.execute('SELECT 1 FROM route_requests WHERE parent_id=?', (parent_id,)).fetchone():
            raise ValueError('continuation_conflict')
        parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        if (not parent or parent['status'] != 'pending' or not parent['agent']
                or conn.execute('SELECT 1 FROM continuations WHERE child_id=?', (parent_id,)).fetchone()):
            raise ValueError('parent_not_running')
        credential = conn.execute('SELECT scope_json,owner_json FROM continuation_credentials WHERE digest=?',
                                  (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not credential['scope_json']:
            raise ValueError('original_scope_unavailable')
        if not owners.successors_enabled(credential['owner_json']):
            raise PermissionError('continuation_not_authorized')
        if not owners.valid(conn, credential['owner_json'], topic=parent['topic'], agent=parent['agent']):
            raise PermissionError('continuation_not_authorized')
        owner = json.loads(credential['owner_json'])
        if any(step['mode'] in {'backing_git','task_review','task_publication'} for step in steps):
            from . import task_workspaces, task_publication
            task = task_workspaces.for_turn(parent_id)
            if ((task and any(step['mode'] == 'backing_git' for step in steps))
                    or (not task and any(step['mode'] in {'task_review','task_publication'} for step in steps))):
                raise PermissionError('continuation_not_authorized')
            if any(step['mode'] == 'task_publication' for step in steps):
                try:
                    task_publication.scope_for(conn, credential['owner_json'], task)
                except (RuntimeError, ValueError):
                    raise PermissionError('continuation_not_authorized') from None
        member = routes.membership(conn, parent_id)
        if member:
            if any(step['mode'] != 'isolated' for step in steps):
                raise PermissionError('continuation_not_authorized')
            routes.reserve(conn, member['root_id'], len(steps), member['depth'] + len(steps))
        deadline = None
        if owner['kind'] != 'human':
            if any(step['mode'] not in {'isolated','task_review','task_publication'} for step in steps):
                raise PermissionError('continuation_not_authorized')
            # Reserve the entire declared sequence, across all Flow members.
            # A later member cannot multiply the owner's eight-step allowance.
            group_path = '$.run_id' if owner['kind'] == 'watch' else '$.flow.flow_run_id'
            group_id = owner['run_id'] if owner['kind'] == 'watch' else owner['flow']['flow_run_id']
            siblings = conn.execute('''SELECT * FROM continuations WHERE parent_id=origin_parent_id
                AND json_extract(owner_json,?)=?''', (group_path, group_id)).fetchall()
            reserved = sum(len(_steps(json.loads(r['request_json']))) for r in siblings)
            if reserved + len(steps) > MAX_STEPS:
                raise ValueError('invalid_continuation_request')
            from .config import RESPONSE_TIMEOUT
            if owner['kind'] == 'watch':
                run = conn.execute('SELECT deadline_at FROM watch_runs WHERE id=?', (owner['run_id'],)).fetchone()
                deadline = run['deadline_at'] if run else None
                if deadline is None:
                    raise PermissionError('continuation_not_authorized')
            else:
                deadline = min([r['deadline_at'] for r in siblings if r['deadline_at'] is not None]
                               or [time.time() + RESPONSE_TIMEOUT])
                if deadline <= time.time():
                    raise PermissionError('continuation_not_authorized')
        identifier = uuid4().hex
        conn.execute('''INSERT INTO continuations(id,parent_id,request_key,prompt,mode,topic,agent,created,
                        scope_json,origin_parent_id,remaining_json,request_json,requires_result,owner_json,deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (identifier, parent_id, body['request_key'], steps[0]['prompt'], steps[0]['mode'],
                      parent['topic'], parent['agent'], time.time(), credential['scope_json'], parent_id,
                      json.dumps(steps[1:]), request_json, int('steps' in body or steps[0]['mode'] in {'task_review','task_publication'}), credential['owner_json'], deadline))
        return dict(conn.execute('SELECT * FROM continuations WHERE id=?', (identifier,)).fetchone())


def result(token, body):
    """A step can acknowledge its own outcome, never authorize additional work."""
    if not isinstance(body, dict) or set(body) != {'success'} or type(body['success']) is not bool:
        raise ValueError('invalid_continuation_request')
    with _connection() as conn:
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
    with _connection() as conn:
        if cancel:
            conn.execute('BEGIN IMMEDIATE')
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
            # Persist the freeze before queue/process cancellation outside this transaction.
            conn.execute("UPDATE continuations SET remaining_json='[]',cancel_requested=1 WHERE COALESCE(origin_parent_id,parent_id)=?", (origin,))
            conn.execute("UPDATE continuations SET state='cancelled',reason='cancelled_by_parent' WHERE COALESCE(origin_parent_id,parent_id)=? AND state IN ('waiting','eligible','claimed')", (origin,))
            return [dict(r) for r in conn.execute('SELECT * FROM continuations WHERE COALESCE(origin_parent_id,parent_id)=? ORDER BY created', (origin,))]
        return [dict(r) for r in rows]


def inspect_result(conn, parent_id):
    return [dict(r) for r in conn.execute('SELECT * FROM continuations WHERE parent_id=?', (parent_id,))]


def _advance(conn, row):
    """Persist the next step in the same transaction as its completion evidence."""
    if row['mode'] == 'task_publication' or row['cancel_requested']:
        return
    remaining = json.loads(row['remaining_json'])
    if not remaining:
        return
    step = remaining[0]
    conn.execute('''INSERT OR IGNORE INTO continuations(id,parent_id,request_key,prompt,mode,topic,agent,
                    state,reason,created,scope_json,origin_parent_id,remaining_json,requires_result,owner_json,deadline_at)
                    VALUES(?,?,?,?,?,?,?,'eligible','completed_and_synced',?,?,?,?,1,?,?)''',
                 (uuid4().hex, row['child_id'], row['request_key'], step['prompt'], step['mode'],
                  row['topic'], row['agent'], time.time(), row['scope_json'],
                  row['origin_parent_id'] or row['parent_id'], json.dumps(remaining[1:]), row['owner_json'], row['deadline_at']))


def complete(parent_id, success, reason):
    """Called only after persisted output and every sync result, including no-ops."""
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        from . import routes
        routes.finish_turn(conn, parent_id)
        has_sequence = conn.execute('SELECT 1 FROM continuations WHERE parent_id=? OR child_id=?', (parent_id, parent_id)).fetchone()
        has_route = conn.execute('SELECT 1 FROM route_requests WHERE parent_id=?', (parent_id,)).fetchone()
        if not has_sequence and not has_route and not routes.membership(conn, parent_id):
            return
        message = conn.execute('SELECT status FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
        success = success and message is not None and message['status'] == 'done'
        step = conn.execute('SELECT * FROM continuations WHERE child_id=?', (parent_id,)).fetchone()
        if success and step and step['requires_result'] and step['result_success'] != 1:
            success, reason = False, 'step_failed' if step['result_success'] == 0 else 'step_result_missing'
        if success and step and step['mode'] == 'task_review':
            from .task_publication import finish_review
            if not finish_review(conn, step):
                success, reason = False, 'task_review_invalid'
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


def _reconcile_gates(conn):
    rows = conn.execute("""SELECT c.*, g.success AS gate_success, g.reason AS gate_reason
        FROM continuations c JOIN turn_completion_gates g
        ON g.parent_id=CASE WHEN c.state='waiting' THEN c.parent_id ELSE c.child_id END
        WHERE c.state IN ('waiting','claimed','running')""").fetchall()
    for row in rows:
        if row['state'] == 'waiting':
            state = 'eligible' if row['gate_success'] else 'blocked'
        else:
            state = 'successful' if row['gate_success'] else 'failed'
        conn.execute('UPDATE continuations SET state=?,reason=? WHERE id=?',
                     (state, row['gate_reason'], row['id']))
        if state == 'successful':
            _advance(conn, row)
    return len(rows)


def reconcile():
    """Repair only proven completion; live work without a gate remains untouched."""
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _reconcile_gates(conn)


def recover():
    """Startup alone can classify ungated claims as interrupted; never replay them."""
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE continuation_credentials SET revoked=1')
        _reconcile_gates(conn)
        conn.execute("""UPDATE continuations SET state='blocked',reason='parent_completion_unknown'
            WHERE state='waiting'""")
        conn.execute("""UPDATE continuations SET state='ambiguous',reason='interrupted_after_claim'
            WHERE state IN ('claimed','running')""")


def claim(busy):
    from . import admission_reads
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE continuations SET state='blocked',reason='parent_cancelled_or_deleted' WHERE state='waiting' AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.id=continuations.parent_id AND m.status IN ('pending','done'))")
        candidates = [r['id'] for r in conn.execute("SELECT id FROM continuations WHERE state='eligible' ORDER BY created")]
    for identifier in candidates:
        # This connection has no write transaction. Slow Git reads must not
        # prevent cancellation, grant revocation or unrelated message writes.
        with _connection() as conn:
            row = conn.execute("SELECT * FROM continuations WHERE id=? AND state='eligible'", (identifier,)).fetchone()
            if row is None:
                continue
            with admission_reads.capture() as observations:
                authorization_error(conn, row)
        with _connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM continuations WHERE id=? AND state='eligible'", (identifier,)).fetchone()
            if row is None:
                continue
            try:
                with admission_reads.replay(observations):
                    reason = authorization_error(conn, row)
            except admission_reads.UncapturedRead:
                # Authority changed the set of required identity reads. Let a
                # later scan collect them outside the lock before trying again.
                continue
            if reason:
                conn.execute("UPDATE continuations SET state='blocked',reason=? WHERE id=?", (reason, row['id']))
                continue
            if row['topic'] in busy:
                continue
            parent = conn.execute('SELECT status FROM chat_messages WHERE id=?', (row['parent_id'],)).fetchone()
            if not parent or parent['status'] != 'done':
                conn.execute("UPDATE continuations SET state='blocked',reason='parent_unavailable' WHERE id=?", (row['id'],))
                continue
            from . import routes
            member = routes.membership(conn, row['parent_id'])
            if member:
                try:
                    admitted = routes.charge(conn, 'continuation:' + row['id'], member['root_id'])
                except ValueError as exc:
                    conn.execute("UPDATE continuations SET state='blocked',reason=? WHERE id=?", (str(exc), row['id']))
                    continue
                if not admitted:
                    continue
            conn.execute("UPDATE continuations SET state='claimed' WHERE id=?", (row['id'],))
            return dict(row)


def link(identifier, child_id):
    with _connection() as conn:
        changed = conn.execute("UPDATE continuations SET child_id=?,state='running' WHERE id=? AND state='claimed'",
                               (child_id, identifier)).rowcount == 1
        if changed:
            from . import routes
            row = conn.execute('SELECT parent_id FROM continuations WHERE id=?', (identifier,)).fetchone()
            member = routes.membership(conn, row['parent_id'])
            if member:
                conn.execute('INSERT OR IGNORE INTO route_turns VALUES(?,?,?,?)',
                    (child_id, member['root_id'], member['depth'] + 1, 'continuation:' + identifier))
        return changed


def fail(identifier, reason):
    with _connection() as conn:
        conn.execute("UPDATE continuations SET state='blocked',reason=? WHERE id=? AND state IN ('claimed','running')", (reason, identifier))


def _sequence_history(row):
    """Only this sequence's ancestors, for stateless runners or failed resumes."""
    turns = []
    parent_id = row['parent_id']
    origin_id = row['origin_parent_id'] or parent_id
    with _connection() as conn:
        for _ in range(MAX_STEPS + 1):
            assistant = db.get_message(parent_id)
            user = db.get_message(assistant['reply_to']) if assistant else None
            if not assistant or not user:
                raise ValueError('sequence_context_unavailable')
            turns.append([user, assistant])
            if parent_id == origin_id:
                return [message for turn in reversed(turns) for message in turn]
            previous = conn.execute('SELECT parent_id FROM continuations WHERE child_id=?',
                                    (parent_id,)).fetchone()
            if not previous:
                break
            parent_id = previous['parent_id']
    raise ValueError('sequence_context_unavailable')


def _dispatch_authorization(row):
    with _connection() as conn:
        current = conn.execute('SELECT * FROM continuations WHERE id=?', (row['id'],)).fetchone()
        if current is None or current['state'] != 'claimed':
            return False, None
        reason = authorization_error(conn, current)
    return True, reason


async def dispatch(row, dispatcher):
    from .server import _prepare_chat_turn
    from fastapi.responses import JSONResponse
    claimed, reason = await asyncio.to_thread(_dispatch_authorization, row)
    if not claimed:
        return
    if reason:
        await asyncio.to_thread(fail, row['id'], reason)
        return
    if row['mode'] == 'task_publication':
        from .task_publication import execute
        operation = asyncio.create_task(asyncio.to_thread(execute, row))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            with _connection() as conn:
                conn.execute("UPDATE continuations SET cancel_requested=1,state='ambiguous',reason='publication_interrupted' WHERE id=? AND state IN ('claimed','running')", (row['id'],))
            # The current bounded transport call may already have had effects.
            # Join it; the next authority boundary must block further writes.
            await settle(operation)
            raise
        owner = json.loads(row['owner_json'])
        if owner['kind'] == 'human_flow':
            from .flow import complete_durable_step
            await complete_durable_step(owner['parent_id'])
        return
    parent = await asyncio.to_thread(db.get_message, row['parent_id'])
    origin = await asyncio.to_thread(db.get_message, row['origin_parent_id'] or row['parent_id'])
    owner = json.loads(row['owner_json'])
    original = await asyncio.to_thread(db.get_message, owner.get('request_id') or origin['reply_to']) if origin else None
    watch_run_id = owner.get('run_id') if owner['kind'] == 'watch' else None
    if watch_run_id:
        from .watch_runs import details
        _, _, grant, _ = await asyncio.to_thread(details, watch_run_id)
        original = dict(role='user', source='human', content=grant['instructions'])
    if (not parent or not origin or origin['status'] != 'done' or not original
            or original['role'] != 'user' or original['source'] != 'human'):
        await asyncio.to_thread(fail, row['id'], 'original_request_unavailable')
        return
    from .turn_context import continuation_instruction
    scope = continuation_instruction(row['mode'])['content']
    remaining = json.loads(row['remaining_json'])
    sequence_notice = (f'{len(remaining)} predeclared steps remain after this step. They run only after successful completion and sync. '
                       'Do not perform later steps in this turn.\nRemaining steps:\n' + json.dumps(remaining)
                       if remaining else 'No further automatic successor is available.')
    if row['requires_result']:
        sequence_notice += ('\nBefore responding, record this step outcome with `agentsquid continue result --input -` '
                            'using JSON {"success":true} only after all requested actions and checks succeed, '
                            'or {"success":false} on any failure or incomplete work. '
                            'Missing or false results stop the sequence. Recording success does not bypass sync.')
    if owner['kind'] != 'human':
        prompt = ('Squid continuation ' + row['id'] + ' of assistant message ' + str(row['parent_id']) + '.\n'
                  + scope + '\n' + sequence_notice + '\nParent sync succeeded; '
                  'other work may have changed the repos since then.\nOriginal user request:\n'
                  + original['content'][:16000] + '\nParent result:\n' + parent['content'][:16000]
                  + '\nRequested next step (within original scope):\n' + row['prompt']
                  + '\nReport actual completed steps and any remaining failure or limitation concisely. '
                    'A scheduled step is not a completed step. If commit succeeds and push fails, report both; '
                    'do not imply rollback or promise another automatic turn.')
    else:
        prompt = (f"Squid continuation {row['id']} of assistant message {row['parent_id']}.\n"
                  + sequence_notice + '\nParent sync succeeded; other work may have changed the repos.\n'
                  + 'Requested next step (within original scope):\n' + row['prompt']
                  + '\nReport actual completed steps and remaining failures concisely. '
                    'If commit succeeds and push fails, report both.')
    island = bool(origin['adhoc']) or owner['kind'] != 'human'
    history = await asyncio.to_thread(_sequence_history, row) if island else None
    from .task_workspaces import for_turn
    task = await asyncio.to_thread(for_turn, row['parent_id'])
    if task and row['mode'] == 'backing_git':
        await asyncio.to_thread(fail, row['id'], 'task_workspace_requires_publication_mode')
        return
    task_kwargs = {'task_id': task['id']} if task else {}
    prepared = await _prepare_chat_turn(message=prompt, topic=row['topic'], agent=row['agent'],
                                       adhoc=island, source='continuation', **task_kwargs)
    if isinstance(prepared, JSONResponse):
        await asyncio.to_thread(fail, row['id'], 'preparation_blocked')
        return
    # Claim is durable before preparation. A crash in this gap is ambiguous,
    # rather than permission to prepare/execute a second child.
    if not await asyncio.to_thread(link, row['id'], prepared['asst_msg_id']):
        await asyncio.to_thread(db.mark_assistant_cancelled, prepared['asst_msg_id'], 'Continuation no longer claimed')
        return
    if row['mode'] == 'task_review':
        from .task_publication import begin_review
        await asyncio.to_thread(begin_review, row, prepared['asst_msg_id'])
    if island:
        # Ad hoc dispatch skips both regular-session lookup and publication.
        # Resume only the immediately preceding turn in this sequence.
        prepared['resume_session_id'] = parent.get('session_id')
        prepared['context_history'] = history
    actual = capture_scope(prepared['code_roots'], prepared['configured_cwd'])
    if not await asyncio.to_thread(scope_matches, row, actual):
        await asyncio.to_thread(fail, row['id'], 'repository_scope_changed')
        await asyncio.to_thread(db.mark_assistant_cancelled, prepared['asst_msg_id'], 'Repository scope changed')
        return
    keys = ('topic', 'context_history', 'harness', 'provider', 'backend', 'model', 'agent',
            'cwd', 'source_cwd', 'configured_cwd', 'response_timeout', 'resume_session_id',
            'adhoc', 'lookback', 'code_roots', 'display_prompt', 'worktree_setup_elapsed_ms', 'worktree_isolated')
    kwargs = {key: prepared[key] for key in keys}
    kwargs['effort'] = prepared.get('effort')
    try:
        kwargs['response_timeout'] = await asyncio.to_thread(launch_timeout, prepared['asst_msg_id'],
                                                            kwargs['response_timeout'])
    except RuntimeError:
        await asyncio.to_thread(fail, row['id'], 'sequence_deadline')
        await asyncio.to_thread(db.mark_assistant_cancelled, prepared['asst_msg_id'], 'Sequence deadline')
        return
    if watch_run_id:
        kwargs['watch_run_id'] = watch_run_id
    # The accepted turn already carries the host's configured deadline. A
    # shorter hidden cap aborts long edits and revokes their result credential
    # while the CLI may still be finishing tool work.
    out, _, _ = await dispatcher.dispatch(prompt=prepared['effective_message'], msg_id=prepared['asst_msg_id'], **kwargs)
    while await out.get() is not None:
        pass
    with _connection() as conn:
        conn.execute("UPDATE continuations SET state='ambiguous',reason='missing_completion_evidence' WHERE id=? AND state='running'", (row['id'],))
    if owner.get('flow') and owner['kind'] == 'human_flow':
        from .flow import complete_durable_step
        await complete_durable_step(owner['parent_id'])


async def maintain(dispatcher):
    # Dispatch waits for the child's output stream. Stops must keep retrying
    # during that wait, and both loops must be joined on lifespan shutdown.
    cancellations = asyncio.create_task(_maintain_cancellations(dispatcher))
    try:
        await _maintain_dispatch(dispatcher)
    finally:
        cancellations.cancel()
        await settle(cancellations)


async def _maintain_cancellations(dispatcher):
    while True:
        try:
            await call(reconcile)
            from . import routes
            await routes.maintain_stops(dispatcher)
        except Exception:
            log.exception('Continuation reconciliation scan failed')
        try:
            await cancel_descendants(dispatcher)
        except Exception:
            log.exception('Continuation cancellation scan failed')
        await asyncio.sleep(.5)


async def _maintain_dispatch(dispatcher):
    while True:
        row = None
        try:
            if _enabled:
                row = await call(claim, dispatcher.busy_topics())
                if row:
                    await dispatch(row, dispatcher)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Continuation dispatch failed')
            if row:
                # Unknown side effects after a dispatch failure require inspection.
                with _connection() as conn:
                    conn.execute("UPDATE continuations SET state='ambiguous',reason='dispatch_failed' WHERE id=? AND state IN ('claimed','running')", (row['id'],))
        await asyncio.sleep(.5)


async def cancel_descendants(dispatcher, rows=None):
    """Drain/stop only after cancellation commits; periodic calls retry interruptions."""
    if rows is None:
        def pending():
            with _connection() as conn:
                return [dict(r) for r in conn.execute('''SELECT c.* FROM continuations c
                    LEFT JOIN chat_messages m ON m.id=c.child_id
                    WHERE c.cancel_requested=1 AND c.child_id IS NOT NULL
                    AND (m.id IS NULL OR m.status IN ('pending','cancelled'))''')]
        rows = await call(pending)
    from .runners import kill_proc_by_msg_id
    for row in rows:
        if not row['cancel_requested'] or not row['child_id']:
            continue
        child = row['child_id']
        try:
            await call(db.mark_assistant_cancelled, child, 'Sequence cancelled')
            # Completion may win the race with the cancellation update. Preserve
            # terminal work rather than acting on an earlier pending snapshot.
            message = await call(db.get_message, child)
            if message and message['status'] != 'cancelled':
                continue
            dispatcher.drain_topic(row['topic'], msg_id=child)
            await call(kill_proc_by_msg_id, child)
        except Exception:
            # The durable freeze is already accepted. One failed stop must not
            # fail the API or prevent attempts to stop the other descendants.
            log.exception('Continuation child cancellation failed child_id=%s', child)


def git_only(msg_id, *, durable=False):
    if not _enabled and not durable:
        return False
    with _connection() as conn:
        # Promotion also runs with continuations disabled and in databases
        # created before this feature. Persisted mode still governs old work.
        if durable and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuations'").fetchone():
            return False
        return conn.execute("SELECT 1 FROM continuations WHERE child_id=? AND mode='backing_git'", (msg_id,)).fetchone() is not None


def read_only(msg_id, *, durable=False):
    if durable:
        with _connection() as conn:
            if not db._table_columns(conn, 'continuations'):
                return False
    if git_only(msg_id, durable=durable):
        return True
    with _connection() as conn:
        return conn.execute("SELECT 1 FROM continuations WHERE child_id=? AND mode='task_review'", (msg_id,)).fetchone() is not None


def sequence_step(msg_id):
    with _connection() as conn:
        return conn.execute('SELECT 1 FROM continuations WHERE child_id=? AND requires_result=1', (msg_id,)).fetchone() is not None
