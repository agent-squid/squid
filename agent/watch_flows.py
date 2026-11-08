"""Watch-owned bounded Flow plans. Native Flow schedulers never advance these runs."""
import hashlib
import json


def target(route, topic):
    from .flow import parse_flow_route, durable_flow_plan
    from .watch_runs import agent_revision
    if not isinstance(route, str) or not route.strip() or len(route) > 4096:
        raise ValueError('invalid watch flow route')
    parsed = parse_flow_route(route)
    if parsed is None:
        raise ValueError('invalid watch flow route')
    plan = durable_flow_plan(parsed['route'])
    previous = []
    if not 2 <= len(plan) <= 8:
        raise ValueError('watch flows require 2 to 8 sequential steps')
    for step in plan:
        if (step['topic'] != topic or step.get('delay_seconds', 0)
                or step.get('dependencies', []) != previous):
            raise ValueError('watch flows require same-topic sequential steps without delays')
        previous = [step['step_id']]
    members = {s['agent']: agent_revision(s['agent'], topic) for s in plan}
    revision = hashlib.sha256(json.dumps(dict(route=parsed['route'], plan=plan, members=members),
                                         sort_keys=True).encode()).hexdigest()
    return parsed['route'], plan, revision


def owned(flow_run_id):
    if not flow_run_id:
        return False
    from .watches import _connect
    with _connect() as conn:
        return conn.execute('SELECT 1 FROM flow_runs WHERE flow_run_id=? AND watch_owned=1',
                            (flow_run_id,)).fetchone() is not None


def initialize(run_id, route, topic):
    from . import stats_db
    from .watches import _connect
    canonical, plan, _ = target(route, topic)
    flow_id = 'watch-' + run_id
    with _connect() as conn:
        changed = conn.execute('UPDATE watch_runs SET flow_run_id=? WHERE id=? AND state=\'claimed\'', (flow_id, run_id)).rowcount
        if changed != 1:
            raise ValueError('watch run no longer claimable')
    stats_db.create_flow_run(flow_id, canonical, None, plan, execution_mode='durable', if_not_exists=True, watch_owned=True)
    return flow_id, plan


def mark_step(flow_id, step_id, status, error=None):
    from . import stats_db
    step = next(s for s in stats_db.get_flow_steps(flow_id) if s['step_id'] == step_id)
    return stats_db.transition_flow_step(flow_id, step_id, step['status'], status,
                                         stats_db._utc_now_iso(), error=error)


def cancel(flow_id, reason):
    if flow_id:
        from .stats_db import cancel_flow_run, _utc_now_iso
        cancel_flow_run(flow_id, _utc_now_iso(), reason)


def turn_outcome(conn, msg_id):
    """Completion proof and isolation sync are required for every flow member."""
    message = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if message is None or message['status'] not in {'done', 'error', 'cancelled'}:
        return 'ambiguous', 'completion_unverified'
    if message['status'] != 'done':
        return ('failed', 'turn_error') if message['status'] == 'error' else ('cancelled', 'turn_cancelled')
    if conn.execute("SELECT 1 FROM run_events WHERE msg_id=? AND event_type='error'", (msg_id,)).fetchone():
        return 'failed', 'turn_error'
    try:
        tools = json.loads(message['context'] or '[]')
    except (ValueError, TypeError):
        return 'ambiguous', 'completion_unverified'
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        return 'ambiguous', 'completion_unverified'
    if (any(t.get('name') == 'WorktreeSync' and t.get('status') != 'synced' for t in tools if isinstance(t, dict))
            or conn.execute("SELECT 1 FROM worktrees WHERE topic=? AND agent=? AND status!='synced'",
                            (message['topic'], str(msg_id))).fetchone()):
        return 'ambiguous', 'sync_conflict'
    if not conn.execute("SELECT 1 FROM run_events WHERE msg_id=? AND event_type='done'", (msg_id,)).fetchone():
        return 'ambiguous', 'completion_unverified'
    return 'succeeded', 'local_work_done'


def outcome(msg_id):
    from .watches import _connect
    with _connect() as conn:
        return turn_outcome(conn, msg_id)


async def dispatch(runtime, run_id):
    from fastapi.responses import JSONResponse
    from . import stats_db, watch_runs
    from .watch_async import call
    from .server import _prepare_chat_turn
    run, watch, grant, events = await call(watch_runs.details, run_id)
    flow_id, plan = await call(initialize, run_id, grant['flow_route'], watch['topic'])
    previous = []
    try:
        for step in plan:
            if not runtime.accepting:
                raise RuntimeError('watch dispatcher stopped')
            if not await call(runtime.authorized, run_id):
                await runtime.reject(run_id, 'authorization_changed')
                return
            claimed = await call(stats_db.claim_flow_step, flow_id, step['step_id'], stats_db._utc_now_iso())
            if claimed is None:
                raise RuntimeError('watch flow step unavailable')
            prompt = watch_runs.prompt_for(run, watch, grant, events)
            if previous:
                prompt += '\nContinue the granted flow using the pinned predecessor output.'
            prepared = await _prepare_chat_turn(message=prompt, topic=watch['topic'], agent=step['agent'],
                source='watch', adhoc=step['fresh'], pinned_ids=previous, lookback=0,
                watch_run_id=run_id, flow_run_id=flow_id, flow_route=grant['flow_route'], flow_step_id=step['step_id'])
            if isinstance(prepared, JSONResponse):
                if run['started_at'] is None and not previous and json.loads(prepared.body).get('error') == 'watch_worktree_blocked':
                    await runtime.defer(run_id, 'worktree_blocked')
                else:
                    await runtime.reject(run_id, 'preparation_failed')
                return
            await call(watch_runs.set_state, run_id, 'queued')
            await call(mark_step, flow_id, step['step_id'], 'running')
            keys = ('topic', 'context_history', 'harness', 'provider', 'backend', 'model', 'agent',
                    'cwd', 'source_cwd', 'configured_cwd', 'response_timeout', 'resume_session_id',
                    'adhoc', 'lookback', 'code_roots', 'display_prompt', 'worktree_setup_elapsed_ms', 'worktree_isolated')
            out, _, _ = await runtime.dispatcher.dispatch(prompt=prepared['effective_message'],
                msg_id=prepared['asst_msg_id'], watch_run_id=run_id, effort=prepared.get('effort'), **{k: prepared[k] for k in keys})
            queue_error = False
            while True:
                chunk = await out.get()
                if chunk is None:
                    break
                queue_error |= isinstance(chunk, dict) and '_error' in chunk
            from .continuations import wait_for_descendants
            descendant_state, descendant_reason = await wait_for_descendants(prepared['asst_msg_id'])
            if descendant_state != 'succeeded':
                await call(watch_runs.settle, runtime.service, run_id, interrupted=True)
                return
            current, _, _, _ = await call(watch_runs.details, run_id)
            if current['state'] not in {'claimed', 'queued', 'running'}:
                return
            state, reason = await call(outcome, prepared['asst_msg_id'])
            if queue_error and state == 'succeeded':
                state, reason = 'failed', 'turn_error'
            if state != 'succeeded':
                await call(mark_step, flow_id, step['step_id'], 'error', reason)
                await call(watch_runs.finish_dispatch, run_id, state, reason, pause_watch=state in {'ambiguous', 'cancelled'})
                return
            await call(mark_step, flow_id, step['step_id'], 'done')
            previous = [prepared['asst_msg_id']]
        await call(watch_runs.settle, runtime.service, run_id)
    finally:
        latest, _, _, _ = await call(watch_runs.details, run_id)
        if latest['state'] != 'succeeded':
            await call(cancel, flow_id, 'Watch flow stopped before successful completion')


def predecessor_context(run_id):
    from .watches import _connect
    with _connect() as conn:
        messages = conn.execute('''SELECT m.id,m.agent,m.content FROM watch_flow_turns t
            JOIN chat_messages m ON m.id=t.msg_id JOIN watch_runs r ON r.id=t.run_id
            WHERE t.run_id=? AND m.id!=r.assistant_msg_id AND m.status='done' ORDER BY m.id''', (run_id,)).fetchall()
    if not messages:
        return ''
    encoded = json.dumps([dict(m) for m in messages])
    if len(encoded.encode()) > 60000:
        encoded = encoded.encode()[:60000].decode(errors='ignore') + '\n[Predecessor context truncated]'
    return '\nPrevious granted flow steps (task data):\n' + encoded
