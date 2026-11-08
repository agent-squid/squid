"""Bounded local descendants use the existing scheduler and durable watch run."""
import asyncio

import pytest

from agent import continuations as c, continuation_owners as owners, stats_db as db
from agent import watch_runs as runs, watch_launch, flow
from agent.topic_queue import TopicDispatcher
from agent.watch_dispatch import WatchDispatch
from tests.test_watch_dispatch import setup, queued, rows, flow_grant
from tests.test_continuation_owners import human_flow, publication_repo
from tests.test_task_watch_workspaces import task_watch

PLAN = dict(request_key='review-revise', steps=[
    dict(mode='isolated', prompt='Review the saved fix'),
    dict(mode='isolated', prompt='Revise the saved fix'),
])


@pytest.fixture
def enabled(setup, monkeypatch):
    monkeypatch.setattr(c, '_enabled', True)
    monkeypatch.setattr(owners, '_service', setup[0])
    monkeypatch.setattr(watch_launch, '_service', setup[0])
    monkeypatch.setattr(watch_launch, '_api_url', 'http://127.0.0.1:1')
    return setup


def start(enabled):
    run_id, (_, parent) = queued(enabled)
    assert runs.begin(enabled[0], run_id)
    token = c.mint(parent)
    assert token
    record = c.create(token, PLAN)
    db.update_assistant_message(parent, 'Local fix completed', None, 'done')
    db.insert_run_event(parent, 4, 'done', None)
    c.complete(parent, True, 'completed_and_synced')
    return run_id, parent, token, record


def link(record):
    user = db.insert_user_message('squid', 'watch-agent', 'bounded step', source='continuation')
    child = db.insert_assistant_message('squid', 'watch-agent', user, source='continuation', adhoc=True)
    assert c.link(record['id'], child)
    return child


def finish(child, success=True):
    token = c.mint(child)
    assert token
    c.result(token, {'success': success})
    db.update_assistant_message(child, 'Step complete', None, 'done')
    db.insert_run_event(child, 4, 'done', None)
    c.complete(child, True, 'completed_and_synced')


@pytest.mark.parametrize('state', ['eligible', 'claimed', 'running', 'failed', 'ambiguous', 'cancelled'])
def test_settlement_waits_or_pauses_without_replaying_local_fix(enabled, state):
    run_id, parent, token, record = start(enabled)
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET state=? WHERE id=?', (state, record['id']))
    runs.settle(enabled[0], run_id)
    assert rows('watch_events')[0]['disposition'] == 'pending'
    result = runs.details(run_id)[0]
    if state in {'eligible', 'claimed', 'running'}:
        assert result['state'] == 'running'
    else:
        assert result['state'] == ('ambiguous' if state == 'ambiguous' else 'partial')
        assert rows('watches')[0]['status'] == 'paused'
        # Even explicitly resuming a watch cannot blindly replay partial work.
        with db._connection() as conn:
            conn.execute("UPDATE watches SET status='active'")
    assert runs.claim_batch(enabled[0], lambda _: False) is None


@pytest.mark.parametrize('boundary', ['accept', 'claim', 'dispatch', 'admit'])
@pytest.mark.parametrize('mutation', ['expiry', 'revoke', 'cancel', 'scope'])
@pytest.mark.asyncio
async def test_watch_authority_at_every_descendant_boundary(enabled, monkeypatch, boundary, mutation):
    if boundary == 'accept':
        run_id, (_, parent) = queued(enabled)
        token = c.mint(parent)
    else:
        run_id, parent, token, record = start(enabled)
        if boundary in {'dispatch', 'admit'}:
            record = c.claim(set())
        if boundary == 'admit':
            child = link(record)
    if mutation == 'expiry':
        enabled[2][0] = 901
    elif mutation == 'revoke':
        runs.revoke_execution(enabled[0], topic='squid', watch_id=enabled[3]['id'], grant_id=enabled[4]['id'])
    elif mutation == 'cancel':
        with db._connection() as conn:
            conn.execute("UPDATE watches SET status='cancelled'")
    else:
        db.upsert_agent('watch-agent', 'codex', 'openai', 'changed-model')
    if boundary == 'accept':
        with pytest.raises((PermissionError, ValueError)):
            c.create(token, PLAN)
    elif boundary == 'claim':
        assert c.claim(set()) is None
    elif boundary == 'dispatch':
        async def unexpected(**kw):
            pytest.fail('Revoked watch must not prepare a child')
        monkeypatch.setattr('agent.server._prepare_chat_turn', unexpected)
        await c.dispatch(record, None)
    else:
        assert not c.check_execution_scope(child, [], None)
    if boundary != 'accept':
        runs.settle(enabled[0], run_id)
        assert runs.details(run_id)[0]['state'] == 'partial'
        assert rows('watch_events')[0]['disposition'] == 'pending'


@pytest.mark.parametrize('state', ['eligible', 'claimed', 'running'])
def test_restart_freezes_unresolved_descendants_and_does_not_ack(enabled, state):
    run_id, parent, _, record = start(enabled)
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET state=? WHERE id=?', (state, record['id']))
    if state == 'running':
        with db._connection() as conn:
            conn.execute("UPDATE continuations SET state='claimed' WHERE id=?", (record['id'],))
        child = link(record)
    db.init_db()
    c.recover()
    runs.settle(enabled[0], run_id, interrupted=True)
    assert runs.details(run_id)[0]['state'] == 'ambiguous'
    runs.finish_dispatch(run_id)
    if state == 'running':
        assert db.get_message(child)['status'] == 'cancelled'
    assert runs.claim_batch(enabled[0], lambda _: False) is None
    assert c.claim(set()) is None
    assert rows('watch_events')[0]['disposition'] == 'pending'


def test_watch_deadline_and_one_budget_cover_all_children(enabled):
    run_id, parent, _, record = start(enabled)
    deadline = runs.details(run_id)[0]['deadline_at']
    for n in range(2):
        step = c.claim(set())
        assert step['deadline_at'] == deadline
        child = link(step)
        assert c.launch_timeout(child, 7200) == deadline - enabled[2][0]
        finish(child)
        enabled[2][0] += 10
        runs.settle(enabled[0], run_id)
        assert runs.details(run_id)[0]['state'] == ('running' if n == 0 else 'succeeded')
    assert len(rows('watch_runs')) == 1
    assert sum(r['started_at'] is not None for r in rows('watch_runs')) == 1
    assert rows('watch_events')[0]['disposition'] == 'handled'


@pytest.mark.asyncio
async def test_expiry_while_waiting_stops_children(enabled):
    run_id, parent, _, _ = start(enabled)
    child = link(c.claim(set()))
    enabled[2][0] = 901
    assert (await c.wait_for_descendants(parent))[0] == 'ambiguous'
    assert db.get_message(child)['status'] == 'cancelled'
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'ambiguous'
    assert c.claim(set()) is None


@pytest.mark.parametrize('boundary', ['eligible', 'claimed', 'running'])
def test_sequence_cancellation_never_acknowledges_incomplete_plan(enabled, boundary):
    run_id, parent, token, record = start(enabled)
    if boundary in {'claimed', 'running'}:
        record = c.claim(set())
    if boundary == 'running':
        child = link(record)
    c.inspect(token, record['id'], cancel=True)
    if boundary == 'running':
        finish(child)
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'partial'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert c.claim(set()) is None


@pytest.mark.asyncio
async def test_human_flow_waits_for_its_bounded_descendants(enabled, monkeypatch):
    request, parent = human_flow()
    token = c.mint(parent)
    c.create(token, PLAN)
    db.update_assistant_message(parent, 'Fix complete', None, 'done')
    db.insert_run_event(parent, 4, 'done', None)
    c.complete(parent, True, 'completed_and_synced')
    dispatched = []
    async def dispatch_ready(flow_id):
        dispatched.append(flow_id)
    monkeypatch.setattr(flow, '_dispatch_ready_durable_steps', dispatch_ready)
    await flow.complete_durable_step(parent)
    assert db.get_flow_steps('human-flow')[0]['status'] == 'running'
    assert not dispatched
    for _ in range(2):
        finish(link(c.claim(set())))
    await flow.complete_durable_step(parent)
    assert db.get_flow_steps('human-flow')[0]['status'] == 'done'
    assert dispatched == ['human-flow']


def test_flow_members_share_eight_step_reservation(enabled):
    request, parent = human_flow()
    steps = [dict(mode='isolated', prompt='Local review')] * 8
    c.create(c.mint(parent), dict(request_key='eight', steps=steps))
    second = db.insert_assistant_message('squid', 'watch-agent', request, source='workflow',
        flow_run_id='human-flow', flow_route='#squid@watch-agent>@watch-agent', flow_step_id='second')
    with db._connection() as conn:
        conn.execute("UPDATE flow_steps SET assistant_msg_id=?,status='running' WHERE flow_run_id='human-flow' AND step_id='second'", (second,))
    with pytest.raises(ValueError):
        c.create(c.mint(second), PLAN)


@pytest.mark.asyncio
async def test_normal_scheduler_holds_watch_until_review_and_revision_finish(enabled, monkeypatch):
    service = enabled[0]
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    ready, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def runner(prompt, **kw):
        msg_id = kw['msg_id']
        token = kw['backend_env']['SQUID_CONTINUE_TOKEN']
        seen.append(msg_id)
        if len(seen) == 1:
            c.create(token, PLAN)
        else:
            assert 'Requested next step' in prompt
            if len(seen) == 2:
                ready.set()
                await release.wait()
            c.result(token, {'success': True})
        yield 'Local step complete'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    run_id = runs.claim_batch(service, lambda _: False)
    task = asyncio.create_task(runtime.dispatch(run_id))
    runtime.tasks[run_id] = task
    scheduler = asyncio.create_task(c.maintain(dispatcher))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        assert runs.details(run_id)[0]['state'] == 'running'
        assert rows('watch_events')[0]['disposition'] == 'pending'
        with db._connection() as conn:
            assert not runs.pending_user_work(conn, 'squid')
        with db._connection() as conn:
            conn.execute("INSERT INTO watch_events(watch_id,event_key,payload,observed_at) VALUES(?,'issue-two','{}',101)", (enabled[3]['id'],))
        assert runs.claim_batch(service, lambda _: False) is None
        release.set()
        await asyncio.wait_for(task, 5)
        assert len(seen) == 3
        assert runs.details(run_id)[0]['state'] == 'succeeded'
        assert len(rows('watch_runs')) == 1
        assert [e['disposition'] for e in rows('watch_events')] == ['handled', 'pending']
        assert all(c.child_owner(child)['run_id'] == run_id for child in seen[1:])
    finally:
        release.set()
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await runtime.stop()
        for worker in dispatcher._workers.values():
            worker.q.put_nowait(None)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pending_human_work_preempts_next_watch_child_without_rerunning_fix(enabled):
    run_id, parent, _, _ = start(enabled)
    user = db.insert_user_message('squid', 'watch-agent', 'Human work first')
    db.insert_assistant_message('squid', 'watch-agent', user)
    assert c.claim(set()) is None
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'partial'
    assert rows('watch_events')[0]['disposition'] == 'pending'


@pytest.mark.asyncio
async def test_watch_flow_does_not_start_next_member_before_descendants(enabled, monkeypatch):
    service = enabled[0]
    flow_grant(enabled)
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    order = []
    async def runner(prompt, **kw):
        message = db.get_message(kw['msg_id'])
        token = kw['backend_env']['SQUID_CONTINUE_TOKEN']
        if message['source'] == 'watch':
            order.append(kw['agent'])
            if kw['agent'] == 'watch-agent':
                c.create(token, PLAN)
        else:
            order.append('descendant')
            c.result(token, {'success': True})
        yield 'Step complete'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    run_id = runs.claim_batch(service, lambda _: False)
    task = asyncio.create_task(runtime.dispatch(run_id))
    runtime.tasks[run_id] = task
    scheduler = asyncio.create_task(c.maintain(dispatcher))
    try:
        await asyncio.wait_for(task, 10)
        assert order == ['watch-agent', 'descendant', 'descendant', 'review-agent']
        assert runs.details(run_id)[0]['state'] == 'succeeded'
        assert len(rows('watch_runs')) == 1
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await runtime.stop()
        for worker in dispatcher._workers.values():
            worker.q.put_nowait(None)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_task_descendants_sync_only_to_saved_task(enabled, task_watch, monkeypatch):
    from pathlib import Path
    from agent import task_workspaces as tasks
    monkeypatch.setattr('agent.config.WORKTREE_ISOLATION_ENABLED', True)
    backing = Path(task_watch[0]['repository'])
    (backing / 'app.txt').write_text('Concurrent human edit\n')
    (backing / 'human.txt').write_text('Human untracked file\n')
    service = enabled[0]
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    seen = []
    async def runner(prompt, **kw):
        cwd = Path(kw['cwd'])
        assert not (cwd / '.git').exists()
        assert not (cwd / 'human.txt').exists()
        task = tasks.for_turn(kw['msg_id'])
        seen.append(task['id'])
        token = kw['backend_env']['SQUID_CONTINUE_TOKEN']
        if len(seen) == 1:
            assert (cwd / 'app.txt').read_text() == 'base\n'
            (cwd / 'app.txt').write_text('Fix\n')
            c.create(token, PLAN)
        else:
            assert (cwd / 'app.txt').read_text() == 'Fix\n'
            if len(seen) == 3:
                (cwd / 'app.txt').write_text('Reviewed revision\n')
            c.result(token, {'success': True})
        yield 'Local task step complete'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    run_id = runs.claim_batch(service, lambda _: False)
    task = asyncio.create_task(runtime.dispatch(run_id))
    runtime.tasks[run_id] = task
    scheduler = asyncio.create_task(c.maintain(dispatcher))
    try:
        await asyncio.wait_for(task, 20)
        assert len(seen) == 3 and len(set(seen)) == 1
        assert runs.details(run_id)[0]['state'] == 'succeeded'
        saved = tasks.get(seen[0])
        assert (Path(saved['worktree_path']) / 'app.txt').read_text() == 'Reviewed revision\n'
        assert (backing / 'app.txt').read_text() == 'Concurrent human edit\n'
        assert (backing / 'human.txt').read_text() == 'Human untracked file\n'
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await runtime.stop()
        for worker in dispatcher._workers.values():
            worker.q.put_nowait(None)
        await asyncio.sleep(0)


@pytest.mark.parametrize('mutation', ['revoke', 'expire', 'cancel'])
def test_authority_rechecked_before_final_event_acknowledgement(enabled, mutation):
    run_id, parent, _, _ = start(enabled)
    for _ in range(2):
        finish(link(c.claim(set())))
    if mutation == 'revoke':
        runs.revoke_execution(enabled[0], topic='squid', watch_id=enabled[3]['id'], grant_id=enabled[4]['id'])
    elif mutation == 'expire':
        enabled[2][0] = 901
    else:
        with db._connection() as conn:
            conn.execute("UPDATE watches SET status='cancelled'")
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'partial'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert runs.claim_batch(enabled[0], lambda _: False) is None


@pytest.mark.asyncio
async def test_flow_safety_scan_settles_child_blocked_before_worker_callback(enabled):
    request, parent = human_flow()
    c.create(c.mint(parent), PLAN)
    db.update_assistant_message(parent, 'Fix complete', None, 'done')
    db.insert_run_event(parent, 4, 'done', None)
    c.complete(parent, True, 'completed_and_synced')
    with db._connection() as conn:
        conn.execute("UPDATE chat_messages SET content='Changed authorization' WHERE id=?", (request,))
    assert c.claim(set()) is None
    await flow.recover_durable_flows()
    assert db.get_flow_steps('human-flow')[0]['status'] == 'error'


def test_sequence_settlement_uses_links_when_wall_clock_moves_backwards(enabled):
    run_id, parent, _, first = start(enabled)
    for _ in range(2):
        finish(link(c.claim(set())))
    with db._connection() as conn:
        conn.execute('UPDATE continuations SET created=1')
        conn.execute('UPDATE continuations SET created=2 WHERE id=?', (first['id'],))
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'succeeded'
    assert rows('watch_events')[0]['disposition'] == 'handled'


def test_final_watch_settlement_rechecks_frozen_instruction_proof(enabled):
    run_id, parent, _, _ = start(enabled)
    for _ in range(2):
        finish(link(c.claim(set())))
    with db._connection() as conn:
        conn.execute("UPDATE watch_execution_grants SET instructions='Changed instructions'")
    runs.settle(enabled[0], run_id)
    assert runs.details(run_id)[0]['state'] == 'partial'
    assert rows('watch_events')[0]['disposition'] == 'pending'


@pytest.mark.parametrize('reconcile', [False, True])
@pytest.mark.parametrize('mutation', ['request', 'deadline'])
@pytest.mark.asyncio
async def test_flow_does_not_advance_after_owner_changes_at_final_settlement(enabled, monkeypatch, reconcile, mutation):
    request, parent = human_flow()
    c.create(c.mint(parent), PLAN)
    db.update_assistant_message(parent, 'Fix complete', None, 'done')
    db.insert_run_event(parent, 4, 'done', None)
    c.complete(parent, True, 'completed_and_synced')
    for _ in range(2):
        finish(link(c.claim(set())))
    with db._connection() as conn:
        if mutation == 'request':
            conn.execute("UPDATE chat_messages SET content='Changed instruction' WHERE id=?", (request,))
        else:
            conn.execute('UPDATE continuations SET deadline_at=1 WHERE parent_id=?', (parent,))
    async def unexpected(*args, **kwargs):
        pytest.fail('Changed owner must not dispatch another Flow member')
    monkeypatch.setattr(flow, '_dispatch_ready_durable_steps', unexpected)
    if reconcile:
        await flow.recover_durable_flows()
    else:
        await flow.complete_durable_step(parent)
    assert db.get_flow_steps('human-flow')[0]['status'] == 'error'


@pytest.mark.parametrize('callback', [False, True])
@pytest.mark.parametrize('gate_recorded', [False, True])
@pytest.mark.asyncio
async def test_failed_flow_parent_with_declared_descendants_does_not_stay_running(enabled, callback, gate_recorded):
    request, parent = human_flow()
    c.create(c.mint(parent), PLAN)
    db.update_assistant_message(parent, 'CLI failed', None, 'error')
    # The worker's Flow callback can run before its finally block persists the
    # continuation gate. Failure must not wait for those never-started children.
    if callback:
        await flow.complete_durable_step(parent, error='CLI failed')
        assert db.get_flow_steps('human-flow')[0]['status'] == 'error'
    if gate_recorded:
        c.complete(parent, False, 'turn_failed')
    await flow.recover_durable_flows()
    assert db.get_flow_steps('human-flow')[0]['status'] == 'error'
    assert c.claim(set()) is None


@pytest.mark.parametrize('recover', [False, True])
@pytest.mark.parametrize('failure', ['partial_error', 'sync_conflict', 'missing_done'])
@pytest.mark.asyncio
async def test_native_flow_requires_successful_completion_and_sync_evidence(enabled, monkeypatch, recover, failure):
    request, parent = human_flow()
    context = '[{"name":"WorktreeSync","status":"conflict"}]' if failure == 'sync_conflict' else None
    db.update_assistant_message(parent, 'Partial output', None, 'done', context=context)
    if failure == 'partial_error':
        db.insert_run_event(parent, 4, 'error', 'CLI failed after producing output')
    if failure != 'missing_done':
        db.insert_run_event(parent, 5, 'done', None)
    async def unexpected(*args, **kwargs):
        pytest.fail('Unverified Flow completion must not dispatch another member')
    monkeypatch.setattr(flow, '_dispatch_ready_durable_steps', unexpected)
    if recover:
        await flow.recover_durable_flows(startup=True)
    else:
        await flow.complete_durable_step(parent)
    assert db.get_flow_steps('human-flow')[0]['status'] == 'error'


@pytest.mark.asyncio
async def test_worker_partial_cli_failure_never_releases_native_flow(enabled, monkeypatch):
    from agent.topic_queue import QueueItem, TopicWorker
    from agent.runners import CLIError
    request, parent = human_flow()
    async def runner(*args, **kwargs):
        yield 'Partial output worth keeping'
        raise CLIError('CLI failed after output')
    async def unexpected(*args, **kwargs):
        pytest.fail('Partial CLI output must not release the next Flow member')
    monkeypatch.setattr('agent.runners.run_codex', runner)
    monkeypatch.setattr(flow, '_dispatch_ready_durable_steps', unexpected)
    item = QueueItem(seq=0, topic='squid', agent='watch-agent', prompt='Fix', context_history=[],
        backend='codex', msg_id=parent, adhoc=True)
    await TopicWorker('squid')._process(item)
    async def settled():
        while db.get_flow_steps('human-flow')[0]['status'] == 'running':
            await asyncio.sleep(.01)
    await asyncio.wait_for(settled(), 2)
    assert db.get_message(parent)['content'] == 'Partial output worth keeping'
    assert db.get_flow_steps('human-flow')[0]['status'] == 'error'


@pytest.mark.parametrize('cancel_at', ['queued', 'retry'])
@pytest.mark.asyncio
async def test_native_flow_cancellation_blocks_queued_and_fresh_session_launches(enabled, monkeypatch, cancel_at):
    from agent.topic_queue import QueueItem, TopicWorker
    from agent.runners import CLIError
    request, parent = human_flow()
    attempts = []
    async def runner(*args, **kwargs):
        attempts.append(kwargs.get('resume_session_id'))
        if cancel_at == 'retry' and len(attempts) == 1:
            assert db.cancel_flow_run('human-flow', db._utc_now_iso(), 'User cancelled')
            raise CLIError('No conversation found')
        yield 'Unexpected work in a cancelled Flow'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    if cancel_at == 'queued':
        assert db.cancel_flow_run('human-flow', db._utc_now_iso(), 'User cancelled')
    item = QueueItem(seq=0, topic='squid', agent='watch-agent', prompt='Fix', context_history=[],
        backend='codex', msg_id=parent, adhoc=True, resume_session_id='old-session')
    await TopicWorker('squid')._process(item)
    assert attempts == ([] if cancel_at == 'queued' else ['old-session'])
    assert db.get_flow_run('human-flow')['status'] == 'cancelled'


@pytest.mark.parametrize('missing', ['step', 'run'])
@pytest.mark.asyncio
async def test_native_flow_launch_blocks_missing_bound_plan(enabled, monkeypatch, missing):
    from agent.topic_queue import QueueItem, TopicWorker
    request, parent = human_flow()
    with db._connection() as conn:
        if missing == 'step':
            conn.execute("DELETE FROM flow_steps WHERE flow_run_id='human-flow' AND step_id='first'")
        else:
            conn.execute("DELETE FROM flow_runs WHERE flow_run_id='human-flow'")
    attempts = []
    async def runner(*args, **kwargs):
        attempts.append(kwargs['msg_id'])
        yield 'Unexpected work after losing the owning Flow'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    item = QueueItem(seq=0, topic='squid', agent='watch-agent', prompt='Fix', context_history=[],
        backend='codex', msg_id=parent, adhoc=True)
    await TopicWorker('squid')._process(item)
    assert not attempts
    assert db.get_message(parent)['status'] == 'error'
