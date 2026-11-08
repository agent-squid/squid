import asyncio
from dataclasses import replace
import hashlib
import json
import sys

from fastapi import FastAPI
import httpx
import pytest

from agent import stats_db, watches, watch_runs
from agent import server
from agent.watch_config import load_watch_configuration
from agent.watch_dispatch import WatchDispatch
from agent.watch_service import WatchControl, WatchDenied, WatchService
from agent.watch_policy import OPERATIONS
from agent.topic_queue import TopicDispatcher, QueueItem


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    stats_db.init_db()
    stats_db.upsert_agent('watch-agent', 'codex', 'openai', None, cwd=str(tmp_path))
    monkeypatch.setattr('agent.memory.topic_memory_squid_config', lambda topic: {})
    monkeypatch.setattr(server, 'topic_memory_squid_config', lambda topic: {})
    configuration = load_watch_configuration({'watches': {'enabled': True, 'policy': {
        'execution_enabled': True, 'max_runs': 3, 'max_attempts': 2}, 'registry': {
        'probes': {'local': {'argv': [sys.executable, '-c', 'print("unused")']}}}}})
    class Adapter:
        async def poll(self, watch):
            return watches.Observation('fresh')
    configuration = replace(configuration, adapters={'local': Adapter()})
    now = [100.0]
    service = WatchService(configuration, clock=lambda: now[0])
    control = WatchControl(service)
    token = control.mint(turn='test', topic='squid', operations=OPERATIONS, expires_at=1000)
    row = service.create(token, adapter='local', target='task:1', request_key='watch', expires_at=900)
    grant = control.record_grant(topic='squid', watch_id=row['id'], user_action='operator', expires_at=900)
    granted = control.mint(turn='operator', topic='squid', operations=OPERATIONS, grants=[grant], expires_at=1000)
    service.transition(granted, row['id'], version=0, action='activate', grant_id=grant)
    execution = watch_runs.execution_grant(service, topic='squid', watch_id=row['id'], agent='watch-agent',
        instructions='Inspect and fix local files', max_runs=3, max_attempts=2, request_key='execution', user_action='operator:1')
    claim = service.claim_due(now=100, lease_seconds=60, limit=4)[0]
    service.finish_poll(claim, now=100, observation=watches.Observation('revision:1', (('event:1', {'summary': 'changed'}),)))
    monkeypatch.setattr(server.app.state, 'watch_service', service, raising=False)
    return service, control, now, row, execution, token


def rows(table):
    with stats_db._connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]


def claimed(setup):
    return watch_runs.claim_batch(setup[0], lambda topic: False)


def queued(setup):
    run_id = claimed(setup)
    pair = watch_runs.prepare_messages(run_id, topic='squid', agent='watch-agent', content='watch turn')
    assert watch_runs.prepare_messages(run_id, topic='squid', agent='watch-agent', content='watch turn') == pair
    watch_runs.set_state(run_id, 'queued')
    return run_id, pair


def test_batch_message_identity_and_success_ack_only_captured(setup):
    service, _, now, row, _, _ = setup
    run_id, (user, assistant) = queued(setup)
    assert len([m for m in rows('chat_messages') if m['source'] == 'watch']) == 2
    assert claimed(setup) is None
    assert watch_runs.begin(service, run_id)
    with stats_db._connect() as conn:
        conn.execute("INSERT INTO watch_events(watch_id,event_key,payload,observed_at) VALUES(?, 'event:2','{}',101)", (row['id'],))
    stats_db.update_assistant_message(assistant, 'done', None, 'done')
    stats_db.insert_run_event(assistant, 4, 'done', None)
    watch_runs.settle(service, run_id)
    assert rows('watch_runs')[0]['state'] == 'succeeded'
    assert {e['event_key']: e['disposition'] for e in rows('watch_events')} == {'event:1': 'handled', 'event:2': 'pending'}
    assert claimed(setup) is not None


def test_run_budget_failure_attempts_and_partial_error(setup):
    service = setup[0]
    for attempt in (1, 2):
        run_id, (_, assistant) = queued(setup)
        assert watch_runs.begin(service, run_id)
        stats_db.update_assistant_message(assistant, 'partial output', None, 'done')
        stats_db.insert_run_event(assistant, 4, 'error', 'failed')
        watch_runs.settle(service, run_id)
        assert rows('watch_runs')[-1]['state'] == 'failed'
    assert claimed(setup) is None
    assert rows('watches')[0]['pause_reason'] == 'execution_attempts'
    assert rows('watch_events')[0]['disposition'] == 'pending'


def test_interrupted_started_run_pauses_without_retry(setup):
    run_id, _ = queued(setup)
    assert watch_runs.begin(setup[0], run_id)
    watch_runs.settle(setup[0], run_id, interrupted=True)
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watches')[0]['pause_reason'] == 'interrupted_outcome'
    assert claimed(setup) is None


def test_pre_start_recovery_and_revocation(setup):
    service, _, _, row, execution, _ = setup
    run_id, _ = queued(setup)
    watch_runs.settle(service, run_id, interrupted=True)
    assert rows('watch_runs')[0]['state'] == 'deferred'
    run_id, _ = queued(setup)
    watch_runs.revoke_execution(service, topic='squid', watch_id=row['id'], grant_id=execution['id'])
    assert not watch_runs.begin(service, run_id)
    assert rows('watch_events')[0]['disposition'] == 'pending'


def test_observation_only_and_agent_config_changes_do_not_dispatch(setup):
    service = setup[0]
    stats_db.upsert_agent('watch-agent', 'codex', 'openai', 'different-model')
    assert claimed(setup) is None
    assert rows('watches')[0]['pause_reason'] == 'execution_not_authorized'


@pytest.mark.asyncio
async def test_dispatch_through_normal_queue_refreshes_and_finishes(setup, monkeypatch):
    service = setup[0]
    entered = []
    async def runner(prompt, **kwargs):
        assert 'Fresh observation' in prompt
        assert 'event:1' in prompt and 'local isolated files only' in prompt
        entered.append(kwargs['msg_id'])
        yield 'Completed local inspection'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    run_id = claimed(setup)
    try:
        await asyncio.wait_for(runtime.dispatch(run_id), 5)
        assert len(entered) == 1
        assert rows('watch_runs')[0]['state'] == 'succeeded'
        assert rows('watch_events')[0]['disposition'] == 'handled'
        assert stats_db.get_message(entered[0])['source'] == 'watch'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_topic_gate_preserves_user_parallelism_and_prioritizes_waiting_users():
    dispatcher = TopicDispatcher()
    normal = QueueItem(0, 'squid', 'user', 'p', [])
    watch = QueueItem(0, 'squid', 'watch', 'p', [], watch_run_id='run')
    async with dispatcher.coordinate(normal) as admitted:
        assert admitted
        async with dispatcher.coordinate(normal) as other:
            assert other  # existing multi-agent user concurrency is preserved
        async with dispatcher.coordinate(watch) as denied:
            assert denied is False
    async with dispatcher.coordinate(watch) as admitted:
        assert admitted
        entered = asyncio.Event()
        async def user_work():
            async with dispatcher.coordinate(normal):
                entered.set()
        task = asyncio.create_task(user_work())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await task
    assert entered.is_set()


@pytest.mark.asyncio
async def test_execution_grants_are_human_only_and_idempotent(setup):
    service, _, _, row, execution, token = setup
    from agent.watch_api import router
    app = FastAPI()
    app.state.watch_service = service
    app.state.watch_control_digest = hashlib.sha256(b'operator').digest()
    app.include_router(router)
    payload = dict(agent='watch-agent', instructions='Inspect and fix local files', max_runs=3,
                   max_attempts=2, request_key='execution')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        human = '/watch-control/squid/watches/' + row['id']
        assert (await client.post(human + '/execution-grant', json=payload, headers={'Authorization': 'Bearer ' + token})).status_code == 403
        for _ in range(2):
            response = await client.post(human + '/execution-grant', json=payload, headers={'Authorization': 'Bearer operator'})
            assert response.status_code == 200
            assert response.json()['grant_id'] == execution['id']
        assert len(rows('watch_execution_grants')) == 1
        assert (await client.post(human + '/execution-grant', json=payload | {'max_runs': 100}, headers={'Authorization': 'Bearer operator'})).status_code == 403


def test_orphan_sweep_cannot_turn_uncertain_work_into_retryable_failure(setup):
    run_id, (_, assistant) = queued(setup)
    assert watch_runs.begin(setup[0], run_id)
    stats_db.mark_orphaned_pending()
    assert stats_db.get_message(assistant)['status'] == 'pending'
    watch_runs.settle(setup[0], run_id, interrupted=True)
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert stats_db.get_message(assistant)['status'] == 'error'


def test_pending_human_request_blocks_claim_before_it_reaches_queue(setup):
    user = stats_db.insert_user_message('squid', 'watch-agent', 'human task')
    assistant = stats_db.insert_assistant_message('squid', 'watch-agent', user)
    assert claimed(setup) is None
    stats_db.update_assistant_message(assistant, 'done', None, 'done')
    assert claimed(setup) is not None


def test_pending_user_defers_before_actual_start_without_consuming_budget(setup):
    run_id, _ = queued(setup)
    assert watch_runs.begin(setup[0], run_id, start=False)
    user = stats_db.insert_user_message('squid', 'watch-agent', 'human task')
    assistant = stats_db.insert_assistant_message('squid', 'watch-agent', user)
    assert not watch_runs.begin(setup[0], run_id)
    assert rows('watch_runs')[0]['started_at'] is None
    assert rows('watch_runs')[0]['state'] == 'deferred'
    assert rows('watches')[0]['status'] == 'active'
    stats_db.update_assistant_message(assistant, 'done', None, 'done')
    assert claimed(setup) is not None


@pytest.mark.asyncio
async def test_default_policy_and_observation_grants_cannot_authorize_execution(setup):
    service, _, _, row, execution, _ = setup
    watch_runs.revoke_execution(service, topic='squid', watch_id=row['id'], grant_id=execution['id'])
    service.configuration = replace(service.configuration, policy=replace(service.configuration.policy, execution_enabled=False))
    with pytest.raises(WatchDenied):
        watch_runs.execution_grant(service, topic='squid', watch_id=row['id'], agent='watch-agent',
            instructions='Inspect', max_runs=1, max_attempts=1, request_key='new', user_action='operator')


def test_sync_conflict_and_budget_are_not_acknowledged(setup):
    service = setup[0]
    run_id, (_, assistant) = queued(setup)
    assert watch_runs.begin(service, run_id)
    stats_db.update_assistant_message(assistant, 'done', None, 'done',
        context=json.dumps([{'name': 'WorktreeSync', 'status': 'conflict'}]))
    watch_runs.settle(service, run_id)
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert rows('watches')[0]['pause_reason'] == 'sync_conflict'


@pytest.mark.asyncio
async def test_source_closure_before_start_completes_without_agent(setup):
    service = setup[0]
    class Adapter:
        async def poll(self, watch):
            return watches.Observation('closed', closed=True)
    service.configuration = replace(service.configuration, adapters={'local': Adapter()})
    runtime = WatchDispatch(service, TopicDispatcher())
    run_id, _ = queued(setup)
    assert await runtime.before_execute(run_id) is None
    assert rows('watches')[0]['status'] == 'completed'
    assert rows('watch_runs')[0]['started_at'] is None
    assert rows('watch_runs')[0]['outcome'] == 'source_closed'


@pytest.mark.asyncio
async def test_source_refresh_failure_pauses_but_is_not_ambiguous_started_work(setup):
    service = setup[0]
    class Adapter:
        async def poll(self, watch):
            raise RuntimeError('provider-secret')
    service.configuration = replace(service.configuration, adapters={'local': Adapter()})
    runtime = WatchDispatch(service, TopicDispatcher())
    run_id, _ = queued(setup)
    assert await runtime.before_execute(run_id) is None
    assert rows('watches')[0]['pause_reason'] == 'source_refresh_failed'
    assert rows('watch_runs')[0]['state'] == 'cancelled'
    assert 'provider-secret' not in str(rows('watch_runs'))


def test_done_message_without_completion_proof_requires_reconciliation(setup):
    run_id, (_, assistant) = queued(setup)
    assert watch_runs.begin(setup[0], run_id)
    stats_db.update_assistant_message(assistant, 'partial', None, 'done')
    watch_runs.settle(setup[0], run_id, interrupted=True)
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watch_events')[0]['disposition'] == 'pending'


def test_pre_execution_failures_consume_attempt_budget(setup):
    for _ in range(2):
        run_id, _ = queued(setup)
        watch_runs.settle(setup[0], run_id, queue_error=True)
    assert claimed(setup) is None
    assert rows('watches')[0]['pause_reason'] == 'execution_attempts'


@pytest.mark.asyncio
async def test_shutdown_cancels_source_refresh_without_killing_shared_worker(setup):
    service = setup[0]
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def poll(self, watch):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    service.configuration = replace(service.configuration, adapters={'local': Adapter()})
    runtime = WatchDispatch(service, TopicDispatcher())
    run_id, _ = queued(setup)
    task = asyncio.create_task(runtime.before_execute(run_id))
    await asyncio.wait_for(entered.wait(), 2)
    await runtime.stop()
    assert await task is None
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_watch_preparation_does_not_change_human_sticky_agent(setup, monkeypatch):
    stats_db.upsert_topic('squid', 'human-agent', last_prompt='human request')
    async def runner(prompt, **kwargs):
        yield 'local result'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    service = setup[0]
    runtime = WatchDispatch(service, TopicDispatcher())
    service.dispatch_runtime = runtime
    run_id = claimed(setup)
    try:
        await runtime.dispatch(run_id)
        topic = stats_db.get_topic('squid')
        assert topic['agent'] == 'human-agent'
        assert next(row for row in stats_db.get_topics_summary() if row['name'] == 'squid')['last_prompt'] == 'human request'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_revocation_during_preparation_blocks_actual_runner_start(setup, monkeypatch):
    import threading
    service, _, _, row, execution, _ = setup
    entered, release = threading.Event(), threading.Event()
    def trackers(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return []
    called = []
    async def runner(prompt, **kwargs):
        called.append(True)
        yield 'must not run'
    monkeypatch.setattr('agent.git_changes.prepare_trackers', trackers)
    monkeypatch.setattr('agent.runners.run_codex', runner)
    runtime = WatchDispatch(service, TopicDispatcher())
    service.dispatch_runtime = runtime
    run_id = claimed(setup)
    task = asyncio.create_task(runtime.dispatch(run_id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.to_thread(watch_runs.revoke_execution, service, topic='squid',
                                watch_id=row['id'], grant_id=execution['id'])
        release.set()
        await task
        assert not called
        assert rows('watch_events')[0]['disposition'] == 'pending'
        assert rows('watch_runs')[0]['started_at'] is None
    finally:
        release.set()
        await task
        await runtime.stop()


def test_execution_defaults_and_topic_narrowing():
    disabled = load_watch_configuration({'watches': {'enabled': True}})
    assert disabled.effective_policy('squid').execution_enabled is False
    narrowed = load_watch_configuration({'watches': {'enabled': True, 'policy': {
        'execution_enabled': True, 'max_runs': 3, 'max_attempts': 2}, 'topics': {
        'squid': {'execution_enabled': False, 'max_runs': 1, 'max_attempts': 1}}}})
    assert narrowed.effective_policy('other').execution_enabled is True
    assert narrowed.effective_policy('squid').execution_enabled is False
    assert narrowed.effective_policy('squid').max_runs == 1


def test_replayed_execution_revoke_preserves_new_explicit_grant(setup):
    service, control, _, row, old, _ = setup
    watch_runs.revoke_execution(service, topic='squid', watch_id=row['id'], grant_id=old['id'])
    watch_runs.execution_grant(service, topic='squid', watch_id=row['id'], agent='watch-agent',
        instructions='New explicit local scope', max_runs=1, max_attempts=1, request_key='replacement', user_action='operator:2')
    observation = rows('watch_grants')[0]['id']
    token = control.mint(turn='resume', topic='squid', operations=OPERATIONS, grants=[observation], expires_at=1000)
    paused = rows('watches')[0]
    service.transition(token, row['id'], version=paused['version'], action='resume', grant_id=observation)
    watch_runs.revoke_execution(service, topic='squid', watch_id=row['id'], grant_id=old['id'])
    assert rows('watches')[0]['status'] == 'active'


@pytest.mark.asyncio
async def test_stop_dispatch_blocks_starts_before_waiting_for_maintenance_cleanup(setup):
    service = setup[0]
    cancelling, release = asyncio.Event(), asyncio.Event()
    class Runtime:
        accepting = True
        async def stop(self):
            pass
    async def maintenance():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise
    runtime = Runtime()
    service.dispatch_runtime = runtime
    service._dispatch_task = asyncio.create_task(maintenance())
    await asyncio.sleep(0)
    task = asyncio.create_task(service.stop_dispatch())
    try:
        await asyncio.wait_for(cancelling.wait(), 2)
        assert runtime.accepting is False
        assert not task.done()
    finally:
        release.set()
        await task


def test_delete_topic_with_watch_history(setup):
    queued(setup)
    stats_db.delete_topic('squid')
    assert rows('watch_runs') == []
    assert rows('watches') == []
    assert not [m for m in rows('chat_messages') if m['source'] != 'monitor']


@pytest.mark.parametrize('adhoc', [None, False, True])
def test_delete_agent_history_detaches_watch_messages(setup, adhoc):
    queued(setup)
    stats_db.delete_topic_agent('squid', 'watch-agent', adhoc=adhoc)
    with stats_db._connect() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.asyncio
async def test_recovery_repairs_terminal_pending_message(setup):
    run_id, (_, assistant) = queued(setup)
    watch_runs.set_state(run_id, 'deferred', 'user_work_priority')
    await WatchDispatch(setup[0], None).recover()
    assert rows('chat_messages')[-1]['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_defer_updates_run_and_message(setup):
    run_id, _ = queued(setup)
    await WatchDispatch(setup[0], None).defer(run_id)
    assert rows('watch_runs')[0]['state'] == 'deferred'
    assert rows('chat_messages')[-1]['status'] == 'cancelled'


def test_claim_skips_large_busy_prefix(setup):
    # Every candidate has a grant; filtering only observation watches is insufficient.
    with stats_db._connect() as conn:
        template = dict(conn.execute('SELECT * FROM watches').fetchone())
        grant = dict(conn.execute('SELECT * FROM watch_execution_grants').fetchone())
        for n in range(105):
            row = dict(template, id='busy-' + str(n), topic='busy', next_poll=0,
                       request_key='busy-' + str(n))
            columns = list(row)
            conn.execute('INSERT INTO watches (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', list(row.values()))
            g = dict(grant, id='grant-' + str(n), watch_id=row['id'])
            conn.execute('INSERT INTO watch_execution_grants (' + ','.join(g) + ') VALUES (' + ','.join('?' for _ in g) + ')', list(g.values()))
            conn.execute("INSERT INTO watch_events(watch_id,event_key,payload,observed_at) VALUES(?,'event','{}',100)", (row['id'],))
    assert watch_runs.claim_batch(setup[0], lambda topic: topic == 'busy') is not None


@pytest.mark.asyncio
async def test_reject_atomically_pauses_and_cancels(setup):
    run_id, _ = queued(setup)
    await WatchDispatch(setup[0], None).reject(run_id, 'authorization_changed')
    assert rows('watch_runs')[0]['state'] == 'cancelled'
    assert rows('watches')[0]['status'] == 'paused'
    assert rows('chat_messages')[-1]['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_worktree_deferral_waits_across_runtime_restart(setup):
    run_id = claimed(setup)
    await WatchDispatch(setup[0], None).defer(run_id, 'worktree_blocked')
    assert rows('watch_runs')[0]['outcome'] == 'worktree_blocked'
    assert rows('watch_runs')[0]['completed_at'] == setup[2][0]
    assert claimed(setup) is None
    setup[2][0] += watch_runs.WORKTREE_RETRY_SECONDS - 1
    await WatchDispatch(setup[0], None).recover()
    assert claimed(setup) is None
    setup[2][0] += 1
    retry = claimed(setup)
    assert retry is not None
    assert rows('watch_runs')[-1]['attempt'] == 2
    assert rows('watch_events')[0]['disposition'] == 'pending'
    await WatchDispatch(setup[0], None).defer(retry, 'worktree_blocked')
    assert claimed(setup) is None
    assert len(rows('watch_runs')) == 2


@pytest.mark.asyncio
async def test_dispatch_preserves_worktree_blocker_reason(setup, monkeypatch):
    from fastapi.responses import JSONResponse
    async def blocked(**kwargs):
        return JSONResponse({'error': 'watch_worktree_blocked'}, status_code=409)
    monkeypatch.setattr(server, '_prepare_chat_turn', blocked)
    run_id = claimed(setup)
    await WatchDispatch(setup[0], None).dispatch(run_id)
    assert rows('watch_runs')[0]['state'] == 'deferred'
    assert rows('watch_runs')[0]['outcome'] == 'worktree_blocked'
    assert rows('watches')[0]['status'] == 'active'
    assert claimed(setup) is None
    assert not [m for m in rows('chat_messages') if m['source'] != 'monitor']


@pytest.mark.asyncio
async def test_execution_refresh_rechecks_grant_between_reads(setup):
    service, _, _, row, execution, _ = setup
    run_id, _ = queued(setup)
    reads = []
    class Adapter:
        async def poll_authorized(self, watch, authorize):
            await authorize()
            reads.append('first')
            watch_runs.revoke_execution(service, topic='squid', watch_id=row['id'], grant_id=execution['id'])
            await authorize()
            reads.append('second')
            return watches.Observation('fresh')
    service.configuration = replace(service.configuration, adapters={'local': Adapter()})
    runtime = WatchDispatch(service, object())
    assert await runtime.before_execute(run_id) is None
    assert reads == ['first']
    assert rows('watch_runs')[0]['started_at'] is None


def flow_grant(setup, *, route='#squid@watch-agent>@review-agent', max_runs=1):
    service, _, _, row, _, _ = setup
    config = stats_db.get_agent('watch-agent')
    stats_db.upsert_agent('review-agent', 'codex', 'openai', None, cwd=config['cwd'])
    with stats_db._connect() as conn:
        conn.execute('UPDATE watch_execution_grants SET revoked_at=99')
    return watch_runs.execution_grant(service, topic='squid', watch_id=row['id'], flow_route=route,
        instructions='Implement then review local changes', max_runs=max_runs, max_attempts=1,
        request_key='flow', user_action='human:flow')


def test_flow_grant_explicit_scope_and_member_revision(setup):
    grant = flow_grant(setup)
    service, _, _, watch, _, _ = setup
    assert grant['flow_route'] == '#squid@watch-agent>@review-agent'
    again = watch_runs.execution_grant(service, topic='squid', watch_id=watch['id'], flow_route=grant['flow_route'],
        instructions=grant['instructions'], max_runs=1, max_attempts=1, request_key='flow', user_action='retry')
    assert again == grant
    config = stats_db.get_agent('review-agent')
    stats_db.upsert_agent('review-agent', 'codex', 'openai', 'changed', cwd=config['cwd'])
    assert claimed(setup) is None
    assert rows('watches')[0]['pause_reason'] == 'execution_not_authorized'


@pytest.mark.parametrize('route', ['#other@watch-agent>@review-agent',
    '#squid@watch-agent=:1s>@review-agent', '#squid@watch-agent=2>@review-agent',
    '#squid@watch-agent', '#squid@watch-agent>@missing'])
def test_unsupported_watch_flow_rejected(setup, route):
    with pytest.raises(ValueError):
        flow_grant(setup, route=route)


@pytest.mark.asyncio
@pytest.mark.parametrize('roundtrip', [False, True])
async def test_flow_dispatch_two_agents_and_single_run_budget(setup, monkeypatch, roundtrip):
    from agent import flow
    grant = flow_grant(setup, route='#squid@watch-agent<>@review-agent' if roundtrip
                       else '#squid@watch-agent>@review-agent')
    service = setup[0]
    entered, native = [], []
    async def unexpected(*args, **kwargs):
        native.append(True)
        return False
    monkeypatch.setattr(flow, '_dispatch_claimed_durable_step', unexpected)
    async def runner(prompt, **kwargs):
        message = stats_db.get_message(kwargs['msg_id'])
        if message['agent'] == 'review-agent':
            assert 'Implementation complete' in prompt
            assert 'Previous granted flow steps' in prompt
        if roundtrip and len(entered) == 2:
            assert 'Review complete' in prompt
        assert 'local isolated files only' in prompt
        entered.append(message['agent'])
        yield 'Implementation complete' if message['agent'] == 'watch-agent' else 'Review complete'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    run_id = claimed(setup)
    try:
        await asyncio.wait_for(runtime.dispatch(run_id), 10)
        expected = ['watch-agent', 'review-agent'] + (['watch-agent'] if roundtrip else [])
        assert entered == expected
        run = rows('watch_runs')[0]
        assert run['state'] == 'succeeded'
        assert run['flow_run_id'] == 'watch-' + run_id
        assert [s['status'] for s in stats_db.get_flow_steps(run['flow_run_id'])] == ['done'] * len(expected)
        assert rows('watch_events')[0]['disposition'] == 'handled'
        assert len(rows('watch_flow_turns')) == len(expected)
        assert all(t['started_at'] is not None for t in rows('watch_flow_turns'))
        assert not native
        with stats_db._connect() as conn:
            conn.execute("INSERT INTO watch_events VALUES(?,'later','{}',101,'pending')", (setup[3]['id'],))
        assert claimed(setup) is None
        assert rows('watches')[0]['pause_reason'] == 'execution_budget'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_flow_revocation_stops_downstream_and_does_not_ack(setup, monkeypatch):
    grant = flow_grant(setup)
    service, _, _, watch, _, _ = setup
    entered = []
    async def runner(prompt, **kwargs):
        entered.append(stats_db.get_message(kwargs['msg_id'])['agent'])
        watch_runs.revoke_execution(service, topic='squid', watch_id=watch['id'], grant_id=grant['id'])
        yield 'First turn completed after revocation'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    dispatcher = TopicDispatcher()
    runtime = WatchDispatch(service, dispatcher)
    service.dispatch_runtime = runtime
    try:
        await asyncio.wait_for(runtime.dispatch(claimed(setup)), 10)
        assert entered == ['watch-agent']
        assert rows('watch_events')[0]['disposition'] == 'pending'
        assert rows('watch_runs')[0]['state'] == 'ambiguous'
        assert rows('flow_runs')[0]['status'] == 'cancelled'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_partial_watch_flow_recovery_cannot_ack_or_dispatch_natively(setup):
    from agent import flow, watch_flows
    flow_grant(setup)
    service = setup[0]
    run_id = claimed(setup)
    flow_id, plan = watch_flows.initialize(run_id, '#squid@watch-agent>@review-agent', 'squid')
    step = plan[0]
    stats_db.claim_flow_step(flow_id, step['step_id'], stats_db._utc_now_iso())
    _, assistant = watch_runs.prepare_messages(run_id, topic='squid', agent='watch-agent', content='watch',
        flow_run_id=flow_id, flow_route='#squid@watch-agent>@review-agent', flow_step_id=step['step_id'])
    watch_runs.set_state(run_id, 'queued')
    assert watch_runs.begin(service, run_id)
    stats_db.update_assistant_message(assistant, 'prefix done', None, 'done')
    stats_db.insert_run_event(assistant, 1, 'done', None)
    watch_flows.mark_step(flow_id, step['step_id'], 'running')
    watch_flows.mark_step(flow_id, step['step_id'], 'done')
    assert await flow._dispatch_ready_durable_steps(flow_id) == 0
    runtime = WatchDispatch(service, TopicDispatcher())
    await runtime.recover()
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert rows('flow_runs')[0]['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_failed_flow_step_stops_downstream(setup, monkeypatch):
    from agent.runners import CLIError
    flow_grant(setup)
    service = setup[0]
    entered = []
    async def runner(prompt, **kwargs):
        entered.append(stats_db.get_message(kwargs['msg_id'])['agent'])
        raise CLIError('fixture failure')
        yield ''
    monkeypatch.setattr('agent.runners.run_codex', runner)
    runtime = WatchDispatch(service, TopicDispatcher())
    service.dispatch_runtime = runtime
    await asyncio.wait_for(runtime.dispatch(claimed(setup)), 10)
    assert entered == ['watch-agent']
    assert rows('watch_runs')[0]['state'] == 'failed'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert all(s['status'] in {'error', 'cancelled'} for s in stats_db.get_flow_steps(rows('watch_runs')[0]['flow_run_id']))
    await runtime.stop()


@pytest.mark.asyncio
async def test_flow_human_priority_after_prefix_pauses_without_replay(setup, monkeypatch):
    flow_grant(setup)
    service = setup[0]
    entered = []
    async def runner(prompt, **kwargs):
        entered.append(stats_db.get_message(kwargs['msg_id'])['agent'])
        user = stats_db.insert_user_message('squid', 'watch-agent', 'human work')
        stats_db.insert_assistant_message('squid', 'watch-agent', user)
        yield 'prefix done'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    runtime = WatchDispatch(service, TopicDispatcher())
    service.dispatch_runtime = runtime
    await asyncio.wait_for(runtime.dispatch(claimed(setup)), 10)
    assert entered == ['watch-agent']
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    await runtime.stop()


@pytest.mark.asyncio
async def test_flow_queue_deferral_after_start_is_ambiguous(setup):
    from agent import watch_flows
    flow_grant(setup)
    service = setup[0]
    run_id = claimed(setup)
    flow_id, plan = watch_flows.initialize(run_id, '#squid@watch-agent>@review-agent', 'squid')
    step = plan[0]
    stats_db.claim_flow_step(flow_id, step['step_id'], stats_db._utc_now_iso())
    watch_runs.prepare_messages(run_id, topic='squid', agent='watch-agent', content='watch',
        flow_run_id=flow_id, flow_route='#squid@watch-agent>@review-agent', flow_step_id=step['step_id'])
    watch_runs.set_state(run_id, 'queued')
    assert watch_runs.begin(service, run_id)
    runtime = WatchDispatch(service, TopicDispatcher())
    await runtime.defer(run_id)
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert rows('watches')[0]['pause_reason'] == 'flow_interrupted_by_user'
    assert rows('watch_events')[0]['disposition'] == 'pending'


def prepared_flow_prefix(setup):
    from agent import watch_flows
    flow_grant(setup)
    service = setup[0]
    stats_db.upsert_topic('squid', 'watch-agent')
    run_id = claimed(setup)
    route = '#squid@watch-agent>@review-agent'
    flow_id, plan = watch_flows.initialize(run_id, route, 'squid')
    first = plan[0]
    stats_db.claim_flow_step(flow_id, first['step_id'], stats_db._utc_now_iso())
    user, assistant = watch_runs.prepare_messages(run_id, topic='squid', agent='watch-agent', content='watch',
        flow_run_id=flow_id, flow_route=route, flow_step_id=first['step_id'])
    watch_runs.set_state(run_id, 'queued')
    assert watch_runs.begin(service, run_id)
    stats_db.update_assistant_message(assistant, 'prefix done', None, 'done')
    stats_db.insert_run_event(assistant, 1, 'done', None)
    watch_flows.mark_step(flow_id, first['step_id'], 'running')
    watch_flows.mark_step(flow_id, first['step_id'], 'done')
    return run_id, flow_id, plan, user, assistant


@pytest.mark.asyncio
async def test_topic_delete_preserves_ownership_and_blocks_native_dispatch(setup, monkeypatch):
    from agent import flow, watch_flows
    run_id, flow_id, plan, _, _ = prepared_flow_prefix(setup)
    assert stats_db.delete_topic('squid')
    assert rows('watch_runs') == []
    assert rows('watches') == []
    assert watch_flows.owned(flow_id)
    assert stats_db.get_flow_run(flow_id)['status'] == 'cancelled'
    assert [s['status'] for s in stats_db.get_flow_steps(flow_id)] == ['done', 'cancelled']
    async def unexpected(*args):
        pytest.fail('deleted watch flow must never dispatch through native Flow')
    monkeypatch.setattr(flow, '_dispatch_claimed_durable_step', unexpected)
    assert await flow._dispatch_ready_durable_steps(flow_id) == 0
    await flow.recover_durable_flows(startup=True)
    with pytest.raises(ValueError):
        stats_db.create_flow_run(flow_id, '#squid@watch-agent>@review-agent', None, plan,
                                 execution_mode='durable', if_not_exists=True)
    assert stats_db.get_topic('squid') is None


@pytest.mark.parametrize('adhoc', [None, False, True])
def test_delete_flow_history_detaches_evidence_and_pauses(setup, adhoc):
    from agent import watch_flows
    run_id, flow_id, _, _, assistant = prepared_flow_prefix(setup)
    if adhoc is True:
        with stats_db._connect() as conn:
            conn.execute('UPDATE chat_messages SET adhoc=1 WHERE id=?', (assistant,))
    stats_db.delete_topic_agent('squid', 'watch-agent', adhoc=adhoc)
    assert stats_db.get_message(assistant) is None
    evidence = rows('watch_flow_turns')[0]
    assert evidence['msg_id'] is None and evidence['started_at'] is not None
    run = rows('watch_runs')[0]
    assert run['id'] == run_id and run['state'] == 'ambiguous'
    assert run['outcome'] == 'flow_history_deleted'
    assert rows('watches')[0]['pause_reason'] == 'flow_history_deleted'
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert stats_db.get_flow_run(flow_id)['status'] == 'cancelled'
    assert watch_flows.owned(flow_id)
    assert not WatchDispatch(setup[0], TopicDispatcher()).authorized(run_id)
    with stats_db._connect() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_deleting_prefix_history_stops_queued_reviewer(setup):
    from agent import watch_flows
    run_id, flow_id, plan, _, assistant = prepared_flow_prefix(setup)
    step = plan[1]
    stats_db.claim_flow_step(flow_id, step['step_id'], stats_db._utc_now_iso())
    _, reviewer = watch_runs.prepare_messages(run_id, topic='squid', agent='review-agent', content='review',
        flow_run_id=flow_id, flow_route='#squid@watch-agent>@review-agent', flow_step_id=step['step_id'])
    watch_runs.set_state(run_id, 'queued')
    watch_flows.mark_step(flow_id, step['step_id'], 'running')
    stats_db.delete_topic_agent('squid', 'watch-agent')
    assert stats_db.get_message(assistant) is None
    assert stats_db.get_message(reviewer)['status'] == 'cancelled'
    assert rows('watch_runs')[0]['assistant_msg_id'] == reviewer
    assert rows('watch_runs')[0]['state'] == 'ambiguous'
    assert not watch_runs.begin(setup[0], run_id)
    assert stats_db.get_flow_run(flow_id)['status'] == 'cancelled'


def test_delete_completed_flow_history_preserves_terminal_outcome(setup):
    from agent import watch_flows
    run_id, flow_id, plan, _, _ = prepared_flow_prefix(setup)
    step = plan[1]
    stats_db.claim_flow_step(flow_id, step['step_id'], stats_db._utc_now_iso())
    _, reviewer = watch_runs.prepare_messages(run_id, topic='squid', agent='review-agent', content='review',
        flow_run_id=flow_id, flow_route='#squid@watch-agent>@review-agent', flow_step_id=step['step_id'])
    watch_runs.set_state(run_id, 'queued')
    assert watch_runs.begin(setup[0], run_id)
    watch_flows.mark_step(flow_id, step['step_id'], 'running')
    stats_db.update_assistant_message(reviewer, 'review done', None, 'done')
    stats_db.insert_run_event(reviewer, 1, 'done', None)
    watch_flows.mark_step(flow_id, step['step_id'], 'done')
    watch_runs.settle(setup[0], run_id)
    assert rows('watch_runs')[0]['state'] == 'succeeded'
    stats_db.delete_topic_agent('squid', 'watch-agent')
    assert rows('watch_runs')[0]['state'] == 'succeeded'
    assert stats_db.get_flow_run(flow_id)['status'] == 'completed'
    assert rows('watches')[0]['status'] == 'active'
    assert rows('watch_events')[0]['disposition'] == 'handled'
    assert len(rows('watch_flow_turns')) == 2


def test_topic_deletion_cancellation_rolls_back_atomically(setup):
    import sqlite3
    _, flow_id, _, _, _ = prepared_flow_prefix(setup)
    with stats_db._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_topic_delete BEFORE DELETE ON topics
            BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='fixture rollback'):
        stats_db.delete_topic('squid')
    assert len(rows('watch_runs')) == 1
    assert rows('watches')[0]['status'] == 'active'
    assert stats_db.get_flow_run(flow_id)['status'] == 'running'
    assert [s['status'] for s in stats_db.get_flow_steps(flow_id)] == ['done', 'pending']


def test_existing_flow_schema_migration_preserves_evidence(setup):
    from agent import watch_flows
    _, flow_id, _, _, assistant = prepared_flow_prefix(setup)
    with stats_db._connect() as conn:
        conn.execute('ALTER TABLE flow_runs DROP COLUMN watch_owned')
        conn.execute('''CREATE TABLE watch_flow_turns_legacy (
            run_id TEXT NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL, msg_id INTEGER NOT NULL REFERENCES chat_messages(id),
            started_at REAL, PRIMARY KEY(run_id,step_id), UNIQUE(msg_id))''')
        conn.execute('INSERT INTO watch_flow_turns_legacy SELECT * FROM watch_flow_turns')
        conn.execute('DROP TABLE watch_flow_turns')
        conn.execute('ALTER TABLE watch_flow_turns_legacy RENAME TO watch_flow_turns')
    stats_db.init_db()
    stats_db.init_db()
    assert watch_flows.owned(flow_id)
    assert rows('watch_flow_turns')[0]['msg_id'] == assistant
    stats_db.delete_topic_agent('squid', 'watch-agent')
    assert rows('watch_flow_turns')[0]['msg_id'] is None
    assert rows('watch_flow_turns')[0]['started_at'] is not None


@pytest.mark.asyncio
async def test_upgrade_cancels_legacy_orphaned_watch_flow(setup):
    from agent import watch_flows, flow
    _, flow_id, _, _, _ = prepared_flow_prefix(setup)
    with stats_db._connect() as conn:
        conn.execute('DELETE FROM watch_runs')
        conn.execute('DELETE FROM watches')
        conn.execute('ALTER TABLE flow_runs DROP COLUMN watch_owned')
    stats_db.init_db()
    assert watch_flows.owned(flow_id)
    assert stats_db.get_flow_run(flow_id)['status'] == 'cancelled'
    assert await flow._dispatch_ready_durable_steps(flow_id) == 0


@pytest.mark.parametrize('change', ['level', 'template', 'removed'])
def test_effort_change_invalidates_watch_execution_grant(setup, monkeypatch, change):
    from agent import harnesses
    service, _, _, watch, _, _ = setup
    config = stats_db.get_agent('watch-agent')
    stats_db.upsert_agent('watch-agent', 'codex', 'openai', None, cwd=config['cwd'], effort='high')
    with stats_db._connect() as conn:
        conn.execute('UPDATE watch_execution_grants SET revoked_at=99')
    watch_runs.execution_grant(service, topic='squid', watch_id=watch['id'], agent='watch-agent',
        instructions='Inspect and fix local files', max_runs=3, max_attempts=2,
        request_key='effort-execution', user_action='operator:effort')
    run_id, _ = queued(setup)
    if change == 'level':
        stats_db.upsert_agent('watch-agent', 'codex', 'openai', None, cwd=config['cwd'], effort='low')
    elif change == 'removed':
        monkeypatch.setitem(harnesses._HARNESS_CONFIG['codex'], 'effort', {})
    else:
        monkeypatch.setitem(harnesses._HARNESS_CONFIG['codex'], 'effort', {
            'args': ['--different-effort', '{value}'], 'levels': {'high': 'High'},
        })
    assert not watch_runs.begin(service, run_id)
    assert rows('watch_runs')[0]['started_at'] is None
    assert rows('watch_events')[0]['disposition'] == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_failure', ['No conversation found', 'prompt is too long'])
@pytest.mark.parametrize('revoke', [False, True])
async def test_watch_session_retry_rechecks_grant_without_consuming_another_run(setup, monkeypatch, resume_failure, revoke):
    from agent.runners import CLIError
    service, _, _, watch, grant, _ = setup
    configuration = stats_db.get_agent('watch-agent')
    session = f'retry-{resume_failure.replace(" ", "-")}-{revoke}'
    stats_db.set_topic_session('squid', 'watch-agent', session, configuration['cwd'], None)
    attempts = []
    async def runner(prompt, **kwargs):
        attempts.append(kwargs.get('resume_session_id'))
        if len(attempts) == 1:
            if revoke:
                watch_runs.revoke_execution(service, topic='squid', watch_id=watch['id'], grant_id=grant['id'])
            raise CLIError(resume_failure)
        yield 'Completed local work in a fresh session'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    runtime = WatchDispatch(service, TopicDispatcher())
    service.dispatch_runtime = runtime
    run_id = claimed(setup)
    try:
        await asyncio.wait_for(runtime.dispatch(run_id), 5)
        assert attempts == ([session] if revoke else [session, None])
        runs = rows('watch_runs')
        assert len(runs) == 1 and runs[0]['started_at'] is not None
        assert runs[0]['state'] == ('failed' if revoke else 'succeeded')
        assert rows('watch_events')[0]['disposition'] == ('pending' if revoke else 'handled')
    finally:
        await runtime.stop()


@pytest.mark.parametrize('context', ['{', '{}', 'null', '"invalid tool evidence"', '[null]'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_watch_settlement_quarantines_invalid_completion_context(setup, context, interrupted):
    run_id, (_, assistant) = queued(setup)
    assert watch_runs.begin(setup[0], run_id)
    stats_db.update_assistant_message(assistant, 'Local output', None, 'done', context=context)
    stats_db.insert_run_event(assistant, 4, 'done', None)
    watch_runs.settle(setup[0], run_id, interrupted=interrupted)
    run = watch_runs.details(run_id)[0]
    assert (run['state'], run['outcome']) == ('ambiguous', 'completion_unverified')
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert rows('watches')[0]['status'] == 'paused'
    assert claimed(setup) is None
