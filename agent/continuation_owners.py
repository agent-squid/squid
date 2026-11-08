"""Recorded continuation authority, independent of generated step prompts.

Watch and Flow proofs are inherited by bounded local successors; generated
instructions never replace the recorded human authority.
"""
import hashlib
import json

_service = None


def configure(service):
    global _service
    _service = service


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS continuation_owner_records (
        parent_id INTEGER PRIMARY KEY, owner_json TEXT NOT NULL)''')


def record(conn, parent_id):
    """Freeze a watch/Flow proof at launch even while successors are disabled."""
    existing = conn.execute('SELECT owner_json FROM continuation_owner_records WHERE parent_id=?', (parent_id,)).fetchone()
    if existing:
        return existing['owner_json']
    owner = capture(conn, parent_id)
    encoded = json.dumps(owner, sort_keys=True)
    if owner and owner['kind'] != 'human':
        conn.execute('INSERT INTO continuation_owner_records VALUES(?,?)', (parent_id, encoded))
    return encoded


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def successors_enabled(encoded):
    try:
        return json.loads(encoded).get('kind') in {'human', 'human_flow', 'watch', 'route'}
    except (TypeError, ValueError, AttributeError):
        return False


def _watch(conn, parent, service):
    from . import watch_runs
    run = conn.execute('''SELECT r.* FROM watch_runs r WHERE r.assistant_msg_id=? OR r.id IN
        (SELECT run_id FROM watch_flow_turns WHERE msg_id=?)''', (parent['id'], parent['id'])).fetchone()
    if (not run or not service or parent['source'] != 'watch' or run['state'] not in {'claimed', 'queued', 'running'}
            or (run['deadline_at'] is not None and service.clock() >= run['deadline_at'])):
        return None
    watch = conn.execute('SELECT * FROM watches WHERE id=?', (run['watch_id'],)).fetchone()
    grant = conn.execute('SELECT * FROM watch_execution_grants WHERE id=?', (run['grant_id'],)).fetchone()
    if not watch or not grant or not watch_runs.authorized(service, conn, watch, grant, now=service.clock()):
        return None
    failures = conn.execute("SELECT COUNT(*) FROM watch_runs WHERE watch_id=? AND batch_key=? AND state='failed'",
                            (watch['id'], run['batch_key'])).fetchone()[0]
    if (watch['topic'] != parent['topic'] or grant['watch_id'] != watch['id']
            or failures >= grant['max_attempts']):
        return None
    used = conn.execute("SELECT COUNT(*) FROM watch_runs WHERE watch_id=? AND (started_at IS NOT NULL OR state='failed')", (watch['id'],)).fetchone()[0]
    if used > grant['max_runs']:
        return None
    if run['flow_run_id']:
        flow = _flow_proof(conn, parent, watch_owned=True)
        if flow is None or flow['flow_run_id'] != run['flow_run_id'] or flow['route'] != grant['flow_route']:
            return None
    else:
        flow = None
        if parent['agent'] != grant['agent'] or parent['flow_run_id']:
            return None
    return dict(kind='watch', parent_id=parent['id'], topic=parent['topic'], agent=parent['agent'],
                watch_id=watch['id'], run_id=run['id'], grant_id=grant['id'],
                run_attempt=run['attempt'], run_deadline=run['deadline_at'],
                grant_digest=_digest(dict(grant)),
                watch_digest=_digest({key: watch[key] for key in
                    ('id', 'topic', 'adapter', 'target', 'observation_plan', 'expires_at')}), flow=flow)


def _flow_proof(conn, parent, *, watch_owned):
    from .watch_runs import agent_revision
    flow = conn.execute('SELECT * FROM flow_runs WHERE flow_run_id=?', (parent['flow_run_id'],)).fetchone()
    if (not flow or bool(flow['watch_owned']) != watch_owned or flow['execution_mode'] != 'durable'
            or flow['status'] not in {'scheduled', 'running', 'completed'}):
        return None
    steps = conn.execute('SELECT * FROM flow_steps WHERE flow_run_id=? ORDER BY step_id', (flow['flow_run_id'],)).fetchall()
    if not any(s['assistant_msg_id'] == parent['id'] and s['agent'] == parent['agent']
               and s['topic'] == parent['topic'] and s['step_id'] == parent['flow_step_id']
               and s['status'] not in {'error', 'cancelled'} for s in steps):
        return None
    if not steps or any(s['topic'] != parent['topic'] for s in steps):
        return None
    plan = [{key: s[key] for key in ('step_id', 'topic', 'agent', 'dependencies', 'fresh', 'delay_seconds')}
            for s in steps]
    try:
        members = {s['agent']: agent_revision(s['agent'], s['topic']) for s in steps}
    except (ValueError, KeyError):
        return None
    return dict(flow_run_id=flow['flow_run_id'], route=flow['route'], plan_digest=_digest(plan), members=members)


def capture(conn, parent_id, *, service=None):
    parent = conn.execute('SELECT * FROM chat_messages WHERE id=?', (parent_id,)).fetchone()
    if (not parent or parent['role'] != 'assistant' or parent['status'] not in {'pending', 'done'}):
        return None
    from .routes import capture_owner, request_for_flow
    if parent['flow_run_id'] and request_for_flow(conn, parent['flow_run_id']):
        return capture_owner(conn, parent)
    if parent['source'] == 'watch':
        return _watch(conn, parent, service or _service)
    if parent['flow_run_id']:
        if parent['source'] not in {'human', 'workflow'}:
            return None
        proof = _flow_proof(conn, parent, watch_owned=False)
        if proof is None:
            return None
        flow = conn.execute('SELECT original_prompt_msg_id FROM flow_runs WHERE flow_run_id=?', (parent['flow_run_id'],)).fetchone()
        request_id = flow['original_prompt_msg_id']
    else:
        if parent['source'] != 'human':
            return None
        proof = None
        request_id = parent['reply_to']
    request = conn.execute('SELECT * FROM chat_messages WHERE id=?', (request_id,)).fetchone()
    if (not request or request['role'] != 'user' or request['source'] != 'human'
            or (request['flow_run_id'] and request['flow_run_id'] != parent['flow_run_id']) or request['status'] != 'done'
            or request['topic'] != parent['topic']):
        return None
    owner = dict(kind='human_flow' if proof else 'human', parent_id=parent_id, request_id=request['id'],
                topic=parent['topic'], agent=parent['agent'],
                request_digest=hashlib.sha256(request['content'].encode()).hexdigest())
    if proof:
        owner['flow'] = proof
    return owner


def valid(conn, encoded, *, topic, agent, service=None):
    """Recheck persisted provenance; missing/unknown owners never gain authority."""
    try:
        owner = json.loads(encoded)
        return (isinstance(owner, dict) and owner.get('kind') in {'human', 'human_flow', 'watch', 'route'}
                and owner.get('topic') == topic and owner.get('agent') == agent
                and capture(conn, owner.get('parent_id'), service=service) == owner)
    except (TypeError, ValueError, RecursionError):
        return False
