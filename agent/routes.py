"""Opt-in, turn-scoped routes on the existing durable Flow scheduler.

Reservations are shared by route fan-out, nested routes and local continuations.
Unknown starts keep their time charge. This is orchestration, not a shell sandbox.
"""
import hashlib
import json
import time
from uuid import uuid4

from . import stats_db as db, continuations as c, continuation_owners as owners

from .route_policy import DEFAULTS, CAPS, configuration


def settings():
    from .config import ROUTES
    return ROUTES


def initialize(conn):
    conn.execute('DROP TRIGGER IF EXISTS route_parent_deleted')
    for ddl in (
        '''CREATE TABLE IF NOT EXISTS route_roots (
            id INTEGER PRIMARY KEY, topic TEXT NOT NULL, owner_json TEXT NOT NULL,
            limits_json TEXT NOT NULL, reserved_turns INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS route_requests (
            id TEXT PRIMARY KEY, parent_id INTEGER NOT NULL UNIQUE, root_id INTEGER NOT NULL,
            depth INTEGER NOT NULL, request_json TEXT NOT NULL, owner_json TEXT NOT NULL,
            scopes_json TEXT NOT NULL, members_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'waiting', reason TEXT, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS route_turns (
            msg_id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL, depth INTEGER NOT NULL,
            charge_key TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS route_charges (
            id TEXT PRIMARY KEY, root_id INTEGER NOT NULL, seconds REAL NOT NULL,
            started REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)''',
        'CREATE INDEX IF NOT EXISTS route_requests_state ON route_requests(state,created)',
        'CREATE INDEX IF NOT EXISTS route_turns_root ON route_turns(root_id)',
        'CREATE INDEX IF NOT EXISTS route_charges_root_active ON route_charges(root_id,active)',
        '''CREATE TRIGGER IF NOT EXISTS route_parent_deleted BEFORE DELETE ON chat_messages
            BEGIN UPDATE route_requests SET state='cancelled',reason='history_deleted'
            WHERE state IN ('waiting','active') AND (parent_id=OLD.id
                OR id IN (SELECT substr(charge_key,6,32) FROM route_turns
                          WHERE msg_id=OLD.id AND charge_key LIKE 'flow:%')
                OR id IN (SELECT json_extract(owner_json,'$.route_id') FROM continuations
                          WHERE child_id=OLD.id OR parent_id=OLD.id)); END''',
    ):
        conn.execute(ddl)


def membership(conn, msg_id):
    return conn.execute('SELECT * FROM route_turns WHERE msg_id=?', (msg_id,)).fetchone()


def limits(conn, root_id):
    root = conn.execute('SELECT * FROM route_roots WHERE id=?', (root_id,)).fetchone()
    saved = json.loads(root['limits_json'])
    return {key: min(saved[key], settings()[key]) for key in DEFAULTS}


def reserve(conn, root_id, turns, depth):
    bounds = limits(conn, root_id)
    if depth > bounds['max_depth']:
        raise ValueError('route_depth_exhausted')
    changed = conn.execute('''UPDATE route_roots SET reserved_turns=reserved_turns+?
        WHERE id=? AND reserved_turns+?<=?''', (turns, root_id, turns, bounds['max_turns'])).rowcount
    if not changed:
        raise ValueError('route_turns_exhausted')


def charge(conn, key, root_id):
    """Claim and capacity/time reservation share the scheduler's transaction."""
    if conn.execute('SELECT 1 FROM route_charges WHERE id=?', (key,)).fetchone():
        # A recovered, previously claimed turn must never renew its allowance.
        return False
    bounds = limits(conn, root_id)
    topic = conn.execute('SELECT topic FROM route_roots WHERE id=?', (root_id,)).fetchone()[0]
    for sql, args, cap in (
        ('SELECT COUNT(*) FROM route_charges WHERE active=1 AND root_id=?', (root_id,), bounds['concurrency']),
        ('SELECT COUNT(*) FROM route_charges c JOIN route_roots r ON r.id=c.root_id WHERE active=1 AND topic=?', (topic,), bounds['topic_concurrency']),
        ('SELECT COUNT(*) FROM route_charges WHERE active=1', (), bounds['global_concurrency']),
    ):
        if conn.execute(sql, args).fetchone()[0] >= cap:
            return False
    used = conn.execute('SELECT COALESCE(SUM(seconds),0) FROM route_charges WHERE root_id=?', (root_id,)).fetchone()[0]
    remaining = bounds['max_seconds'] - used
    if remaining <= 0:
        raise ValueError('route_time_exhausted')
    conn.execute('INSERT INTO route_charges VALUES(?,?,?,?,1)',
                 (key, root_id, min(bounds['turn_seconds'], remaining), time.time()))
    return True


def finish_turn(conn, msg_id, *, proven=True):
    member = membership(conn, msg_id)
    if not member:
        return
    if proven:
        conn.execute('''UPDATE route_charges SET seconds=MIN(seconds,MAX(0,?-started)),active=0
            WHERE id=? AND active=1''', (time.time(), member['charge_key']))
    else:
        conn.execute('UPDATE route_charges SET active=0 WHERE id=?', (member['charge_key'],))


def worker_finished(msg_id):
    """A topic worker exited, even if pre-launch rejection wrote no success gate."""
    with db._connection() as conn:
        finish_turn(conn, msg_id)


def timeout(conn, msg_id, value):
    member = membership(conn, msg_id)
    if not member:
        return value
    row = conn.execute('SELECT * FROM route_charges WHERE id=?', (member['charge_key'],)).fetchone()
    if not row or not row['active']:
        raise RuntimeError('Route execution reservation unavailable')
    remaining = row['seconds'] - max(0, time.time() - row['started'])
    if remaining <= 0:
        raise RuntimeError('Route execution time exhausted')
    return min(value, remaining) if value is not None else remaining


def principal(conn, token):
    parent_id = c.principal(conn, token)
    credential = conn.execute('SELECT * FROM continuation_credentials WHERE digest=?',
        (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
    if not parent or not owners.valid(conn, credential['owner_json'], topic=parent['topic'], agent=parent['agent']):
        raise PermissionError('route_not_authorized')
    return parent, credential


def compile_route(text, parent, credential):
    from .flow import parse_flow_route, durable_flow_plan
    from .watch_runs import agent_revision
    if not isinstance(text, str) or not text.strip() or len(text) > 2048:
        raise ValueError('unsupported_route')
    chain = parse_flow_route(text)
    # Bound the parser before compilation: repeats can otherwise create huge DAGs.
    if (not chain or any(b['op'].get('count', 1) != 1 or b['op'].get('rounds', 1) != 1
                         or b['op'].get('wait') for b in chain['branches'])):
        raise ValueError('unsupported_route')
    if len(chain['branches']) > settings()['max_turns'] or len(chain['origins']) > settings()['max_turns']:
        raise ValueError('unsupported_route')
    plan = durable_flow_plan(chain['route'])
    if not plan or len(plan) > settings()['max_turns'] or any(s['topic'] != parent['topic'] for s in plan):
        raise ValueError('unsupported_route')
    scope = json.loads(credential['scope_json'])
    scopes, members = {}, {}
    for step in plan:
        agent = step['agent']
        scopes[agent] = c.current_scope(parent['topic'], agent)
        if scopes[agent] != scope:
            raise PermissionError('route_scope_mismatch')
        members[agent] = agent_revision(agent, parent['topic'])
    return chain['route'], plan, scopes, members


def validate(token, body):
    if not isinstance(body, dict) or set(body) != {'route'}:
        raise ValueError('invalid_route_request')
    with db._connection() as conn:
        parent, credential = principal(conn, token)
        route, plan, _, _ = compile_route(body['route'], parent, credential)
    from .shared_tools import discover
    effective = discover(token)['routes']
    budget = effective['budget']
    if budget:
        for blocked, reason in ((budget['remaining_turns'] < len(plan), 'route_turns_exhausted'),
                                (budget['depth'] + 1 > effective['limits']['max_depth'], 'route_depth_exhausted')):
            if blocked and reason not in effective['blockers']:
                effective['blockers'].append(reason)
        effective['executable'] = not effective['blockers']
    return dict(version=1, route=route, steps=plan, **effective)


def launch(token, body):
    if (not isinstance(body, dict) or set(body) != {'request_key', 'route', 'prompt'}
            or not isinstance(body['request_key'], str) or not body['request_key'].strip()
            or len(body['request_key']) > 128 or not isinstance(body['prompt'], str)
            or not body['prompt'].strip() or len(body['prompt']) > 16000):
        raise ValueError('invalid_route_request')
    from .config import WORKTREE_ISOLATION_ENABLED
    if not settings()['enabled'] or not WORKTREE_ISOLATION_ENABLED:
        raise PermissionError('routes_disabled')
    encoded = json.dumps(body, sort_keys=True)
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        parent, credential = principal(conn, token)
        existing = conn.execute('SELECT * FROM route_requests WHERE parent_id=?', (parent['id'],)).fetchone()
        if existing:
            if existing['request_json'] != encoded:
                raise ValueError('route_conflict')
            return public(existing)
        owner = json.loads(credential['owner_json'])
        from .task_workspaces import for_turn
        if owner['kind'] not in {'human', 'route'} or parent['source'] == 'continuation' or for_turn(parent['id']):
            raise PermissionError('route_origin_unsupported')
        if parent['status'] != 'pending':
            raise ValueError('parent_not_running')
        if conn.execute('SELECT 1 FROM continuations WHERE parent_id=?', (parent['id'],)).fetchone():
            raise ValueError('route_conflict')
        route, plan, scopes, members = compile_route(body['route'], parent, credential)
        member = membership(conn, parent['id'])
        root_id, depth = (member['root_id'], member['depth'] + 1) if member else (parent['id'], 1)
        if not member:
            conn.execute('INSERT INTO route_roots VALUES(?,?,?,?,0)',
                (root_id, parent['topic'], credential['owner_json'], json.dumps(settings(), sort_keys=True)))
        reserve(conn, root_id, len(plan), depth)
        identifier = uuid4().hex
        conn.execute('''INSERT INTO route_requests(id,parent_id,root_id,depth,request_json,owner_json,
            scopes_json,members_json,created) VALUES(?,?,?,?,?,?,?,?,?)''',
            (identifier, parent['id'], root_id, depth, encoded, credential['owner_json'],
             json.dumps(scopes, sort_keys=True), json.dumps(members, sort_keys=True), time.time()))
        db._insert_flow_run(conn, identifier, route, owner['request_id'], plan, execution_mode='durable')
        return public(conn.execute('SELECT * FROM route_requests WHERE id=?', (identifier,)).fetchone())


def public(row):
    return {key: row[key] for key in ('id', 'parent_id', 'root_id', 'depth', 'state', 'reason', 'created')}


def inspect(token, identifier=None, cancel=False):
    with db._connection() as conn:
        if cancel:
            conn.execute('BEGIN IMMEDIATE')
        parent, _ = principal(conn, token)
        rows = conn.execute('''SELECT q.* FROM route_requests q JOIN route_roots r ON r.id=q.root_id
            WHERE r.topic=? AND (? IS NULL OR q.id=?) ORDER BY created DESC LIMIT 100''',
            (parent['topic'], identifier, identifier)).fetchall()
        if identifier and not rows:
            raise ValueError('route_not_found')
        if cancel:
            if not identifier:
                raise ValueError('invalid_route_request')
            conn.execute("UPDATE route_requests SET state='cancelled',reason='cancelled_by_user' WHERE id=? AND state IN ('waiting','active')", (identifier,))
            rows = conn.execute('SELECT * FROM route_requests WHERE id=?', (identifier,)).fetchall()
        return [public(row) for row in rows]


def request_for_flow(conn, flow_id):
    return conn.execute('SELECT * FROM route_requests WHERE id=?', (flow_id,)).fetchone()


def authorization_error(conn, row):
    from .watch_runs import agent_revision
    from .config import WORKTREE_ISOLATION_ENABLED
    if not settings()['enabled'] or not WORKTREE_ISOLATION_ENABLED:
        return 'routes_disabled'
    owner = json.loads(row['owner_json'])
    if not owners.valid(conn, row['owner_json'], topic=owner['topic'], agent=owner['agent']):
        return 'route_owner_changed'
    scopes = json.loads(row['scopes_json'])
    try:
        for agent, revision in json.loads(row['members_json']).items():
            if agent_revision(agent, owner['topic']) != revision or c.current_scope(owner['topic'], agent) != scopes[agent]:
                return 'route_scope_changed'
    except (ValueError, KeyError):
        return 'route_scope_changed'
    return None


def claim_flow(conn, flow_id, step_id):
    row = request_for_flow(conn, flow_id)
    if not row:
        return True
    if row['state'] != 'active':
        return False
    error = authorization_error(conn, row)
    if error:
        conn.execute("UPDATE route_requests SET state='blocked',reason=? WHERE id=?", (error, row['id']))
        return False
    topic = json.loads(row['owner_json'])['topic']
    if conn.execute("SELECT 1 FROM chat_messages WHERE topic=? AND role='assistant' AND source='human' AND status='pending' LIMIT 1", (topic,)).fetchone():
        return False
    try:
        return charge(conn, 'flow:' + flow_id + ':' + step_id, row['root_id'])
    except ValueError as exc:
        conn.execute("UPDATE route_requests SET state='blocked',reason=? WHERE id=?", (str(exc), row['id']))
        return False


def bind_flow(conn, flow_id, step_id, msg_id):
    row = request_for_flow(conn, flow_id)
    if row:
        conn.execute('INSERT OR IGNORE INTO route_turns VALUES(?,?,?,?)',
            (msg_id, row['root_id'], row['depth'], 'flow:' + flow_id + ':' + step_id))


def capture_owner(conn, parent):
    row = request_for_flow(conn, parent['flow_run_id']) if parent['flow_run_id'] else None
    if not row:
        return None
    if row['state'] not in {'active', 'completed'} or parent['source'] != 'workflow' or authorization_error(conn, row):
        return None
    proof = owners._flow_proof(conn, parent, watch_owned=False)
    root = conn.execute('SELECT * FROM route_roots WHERE id=?', (row['root_id'],)).fetchone()
    if not proof or not membership(conn, parent['id']):
        return None
    return dict(kind='route', parent_id=parent['id'], request_id=json.loads(root['owner_json'])['request_id'],
                topic=parent['topic'], agent=parent['agent'], root_id=row['root_id'], route_id=row['id'], flow=proof)


def execution_allowed(conn, msg_id, actual=None):
    member = membership(conn, msg_id)
    if not member:
        return True
    message = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if message is None:
        return False
    step = conn.execute('SELECT owner_json FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
    owner = json.loads(step['owner_json']) if step else capture_owner(conn, message)
    if not owner or not settings()['enabled']:
        return False
    row = request_for_flow(conn, owner['route_id'])
    return bool(row and row['state'] == 'active' and not authorization_error(conn, row)
                and (actual is None or actual == json.loads(row['scopes_json'])[message['agent']]))


def outcome(conn, parent_id):
    row = conn.execute('SELECT state,reason FROM route_requests WHERE parent_id=?', (parent_id,)).fetchone()
    if not row:
        return None
    if row['state'] in {'waiting', 'active'}:
        return 'pending', 'route_running'
    return ('succeeded', 'route_completed') if row['state'] == 'completed' else ('failed', row['reason'] or row['state'])


def reconcile(*, startup=False):
    """Activate only after sync. Native Flow recovery owns all actual turns."""
    stops = []
    with db._connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT * FROM route_requests WHERE state IN ('waiting','active')").fetchall():
            gate = conn.execute('SELECT * FROM turn_completion_gates WHERE parent_id=?', (row['parent_id'],)).fetchone()
            reason = authorization_error(conn, row)
            state = row['state']
            if reason:
                state = 'blocked'
            elif state == 'waiting':
                if gate:
                    state, reason = ('active', None) if gate['success'] else ('blocked', gate['reason'])
                elif startup:
                    state, reason = 'blocked', 'parent_completion_unknown'
            else:
                run = conn.execute('SELECT status FROM flow_runs WHERE flow_run_id=?', (row['id'],)).fetchone()
                if run['status'] in {'completed', 'failed', 'cancelled'}:
                    state, reason = ('completed', None) if run['status'] == 'completed' else ('failed', run['status'])
            conn.execute('UPDATE route_requests SET state=?,reason=? WHERE id=?', (state, reason, row['id']))
        # Cascade cancellation before the existing scheduler can admit more work.
        for _ in range(CAPS['max_depth'] + 1):
            changed = conn.execute('''UPDATE route_requests SET state='cancelled',reason='ancestor_stopped'
                WHERE state IN ('waiting','active') AND parent_id IN (
                    SELECT t.msg_id FROM route_turns t JOIN chat_messages m ON m.id=t.msg_id
                    JOIN route_requests q ON q.id=m.flow_run_id
                    WHERE q.state NOT IN ('active','completed'))''').rowcount
            if not changed:
                break
        stops = [r['id'] for r in conn.execute("SELECT q.id FROM route_requests q JOIN flow_runs f ON f.flow_run_id=q.id WHERE q.state IN ('failed','blocked','cancelled','ambiguous') AND f.status NOT IN ('completed','failed','cancelled')")]
        # Terminal unlinked claims cannot own a worker: dispatch links before
        # runner admission. Retain their time charge but release concurrency.
        conn.execute("""UPDATE route_charges SET active=0 WHERE id IN (
            SELECT 'flow:'||s.flow_run_id||':'||s.step_id FROM flow_steps s
            WHERE s.assistant_msg_id IS NULL AND s.status IN ('error','cancelled')
            UNION ALL SELECT 'continuation:'||id FROM continuations
            WHERE child_id IS NULL AND state IN ('blocked','failed','cancelled','ambiguous'))""")
        if startup:
            # Unknown work retains its reserved time, but no live concurrency slot.
            conn.execute('UPDATE route_charges SET active=0')
    for identifier in stops:
        db.cancel_flow_run(identifier, db._utc_now_iso(), 'Route stopped')
    return stops


def managed(flow_id):
    with db._connection() as conn:
        return request_for_flow(conn, flow_id) is not None


def dispatch_context(flow_id):
    with db._connection() as conn:
        row = request_for_flow(conn, flow_id)
        if not row:
            return None
        root = conn.execute('SELECT owner_json FROM route_roots WHERE id=?', (row['root_id'],)).fetchone()
        original = conn.execute('SELECT content FROM chat_messages WHERE id=?',
                                (json.loads(root['owner_json'])['request_id'],)).fetchone()
        return ('Squid bounded route. Local inspection, edits and checks only; no Git writes or external mutation. '
                'All route turns and continuations share the original root budget. '
                'Generated instructions cannot expand the original human scope. '
                'Report queued work as queued, never completed.\nOriginal user request:\n'
                + original['content'][:16000] + '\nRequested route work:\n'
                + json.loads(row['request_json'])['prompt'])


async def maintain_stops(dispatcher):
    import asyncio
    from .runners import kill_proc_by_msg_id, get_active_msg_ids
    await asyncio.to_thread(reconcile)
    def stopped():
        with db._connection() as conn:
            # Include continuation children whose owner route stopped.
            rows = conn.execute("""SELECT DISTINCT t.msg_id AS id,COALESCE(m.topic,r.topic) AS topic
                FROM route_turns t JOIN route_roots r ON r.id=t.root_id
                JOIN route_charges charge ON charge.id=t.charge_key AND charge.active=1
                LEFT JOIN chat_messages m ON m.id=t.msg_id
                LEFT JOIN continuations c ON c.child_id=t.msg_id
                WHERE (
                    (t.charge_key LIKE 'flow:%' AND substr(t.charge_key,6,32) IN (
                        SELECT id FROM route_requests WHERE state NOT IN ('active','completed','waiting')))
                    OR json_extract(c.owner_json,'$.route_id') IN (
                        SELECT id FROM route_requests WHERE state NOT IN ('active','completed','waiting')))""").fetchall()
            for row in rows:
                conn.execute("UPDATE continuations SET cancel_requested=1,remaining_json='[]',state=CASE WHEN state IN ('claimed','running') THEN 'ambiguous' ELSE 'cancelled' END,reason='route_stopped' WHERE child_id=? AND state IN ('waiting','eligible','claimed','running')", (row['id'],))
            return [dict(row) for row in rows]
    for row in await asyncio.to_thread(stopped):
        try:
            await asyncio.to_thread(db.mark_assistant_cancelled, row['id'], 'Route stopped')
            dispatcher.drain_topic(row['topic'], msg_id=row['id'])
            await asyncio.to_thread(kill_proc_by_msg_id, row['id'])
            if row['id'] in get_active_msg_ids():
                continue  # Failed signal: the registered worker still owns its slot.
            def release():
                with db._connection() as conn:
                    processing = conn.execute("SELECT 1 FROM run_events WHERE msg_id=? AND event_type='processing' LIMIT 1", (row['id'],)).fetchone()
                    gate = conn.execute('SELECT 1 FROM turn_completion_gates WHERE parent_id=?', (row['id'],)).fetchone()
                    # SIGTERM delivery is not worker completion. A processing
                    # worker releases its own slot after runner/sync cleanup.
                    if processing and not gate:
                        return
                    finish_turn(conn, row['id'], proven=False)
            await asyncio.to_thread(release)
        except Exception:
            # The freeze is durable. Retry this worker on the next scan without
            # starving other workers, and keep its concurrency charge until stopped.
            import logging
            logging.getLogger(__name__).exception('Route worker stop failed msg_id=%s', row['id'])


async def run():
    """Own managed Flow dispatch tasks; claims remain in the common Flow store."""
    import asyncio
    import logging
    from .flow import _dispatch_claimed_durable_step
    tasks = set()
    try:
        while True:
            try:
                await asyncio.to_thread(reconcile)
                # A nested route can finish after its parent worker exited.
                # Reuse Flow's completion gate instead of waiting for a new worker callback.
                from .flow import complete_durable_step
                for running in await asyncio.to_thread(db.get_running_flow_steps):
                    if not await asyncio.to_thread(managed, running['flow_run_id']):
                        continue
                    message = await asyncio.to_thread(db.get_message, running['assistant_msg_id'])
                    if message and message['status'] in {'done', 'error', 'cancelled'}:
                        await complete_durable_step(message['id'], error=None if message['status'] == 'done' else 'Route turn failed')
                for step in await asyncio.to_thread(db.get_due_flow_steps, db._utc_now_iso()):
                    if not await asyncio.to_thread(managed, step['flow_run_id']):
                        continue
                    claimed = await asyncio.to_thread(db.claim_flow_step, step['flow_run_id'], step['step_id'], db._utc_now_iso())
                    if claimed:
                        task = asyncio.create_task(_dispatch_claimed_durable_step(step['flow_run_id'], claimed))
                        tasks.add(task)
                for task in list(tasks):
                    if task.done():
                        tasks.remove(task)
                        task.result()
            except Exception:
                logging.getLogger(__name__).exception('Route maintenance failed')
            await asyncio.sleep(.5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def dispatch_finished(flow_id, step_id, *, started):
    # Closing a stream does not stop its independent topic worker. Only the
    # worker's completion gate, a confirmed stop or startup recovery can release
    # concurrency for a possibly started turn.
    if started:
        return
    with db._connection() as conn:
        conn.execute('UPDATE route_charges SET active=0,seconds=0 WHERE id=? AND active=1',
                     ('flow:' + flow_id + ':' + step_id,))
