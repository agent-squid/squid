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
    assert len(rows('chat_messages')) == 2
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
        assert rows('chat_messages')[0]['source'] == 'watch'
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
    assert rows('chat_messages') == []


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
    assert rows('chat_messages') == []
