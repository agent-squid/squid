"""Bounded routes use disposable state and the existing deterministic Flow executor."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from agent import routes as r, continuations as c, continuation_owners as owners, stats_db as db, flow
from agent.route_api import router
from agent import shared_tools, shared_mcp


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'routes.db')
    db.init_db()
    c.configure(True)
    monkeypatch.setattr('agent.config.WORKTREE_ISOLATION_ENABLED', True)
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **r.DEFAULTS))
    monkeypatch.setattr('agent.memory.topic_memory_squid_config', lambda _: {'code_roots': []})
    for agent in ('codex', 'review', 'test'):
        db.upsert_agent(agent, 'codex', 'openai', None, cwd=str(tmp_path))
    user = db.insert_user_message('squid', 'codex', 'Fix and review the requested change')
    parent = db.insert_assistant_message('squid', 'codex', user)
    yield parent, c.mint(parent)
    c.configure(False)


BODY = dict(request_key='review', route='#squid@codex>@review,@test', prompt='Review the fix')


def synced(parent, success=True):
    db.update_assistant_message(parent, 'done', None, 'done')
    db.insert_run_event(parent, 4, 'done' if success else 'error', None)
    c.complete(parent, success, 'completed_and_synced' if success else 'sync_failed')


def activate(setup, body=None):
    row = r.launch(setup[1], body or BODY)
    synced(setup[0])
    r.reconcile()
    return row


def prepare(row, agent='codex'):
    steps = db.get_flow_steps(row['id'])
    step = next(s for s in steps if s['agent'] == agent and s['status'] == 'pending')
    claimed = db.claim_flow_step(row['id'], step['step_id'], db._utc_now_iso())
    assert claimed
    user = db.insert_user_message('squid', agent, 'managed work', source='workflow',
                                  flow_run_id=row['id'], flow_route=BODY['route'], flow_step_id=step['step_id'])
    child = db.insert_assistant_message('squid', agent, user, source='workflow',
                                        flow_run_id=row['id'], flow_route=BODY['route'], flow_step_id=step['step_id'])
    assert db.link_flow_step_messages(row['id'], step['step_id'], user, child)
    assert db.transition_flow_step(row['id'], step['step_id'], 'claimed', 'running', db._utc_now_iso())
    return step, child, c.mint(child)


def test_route_gate_single_successor_and_idempotency(setup):
    row = r.launch(setup[1], BODY)
    assert r.launch(setup[1], BODY) == row
    assert row['state'] == 'waiting'
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso()) is None
    with pytest.raises(ValueError, match='route_conflict'):
        r.launch(setup[1], BODY | {'request_key': 'different'})
    with pytest.raises(ValueError, match='continuation_conflict'):
        c.create(setup[1], dict(request_key='extra', mode='isolated', prompt='more'))
    synced(setup[0])
    r.reconcile()
    assert r.inspect(setup[1], row['id'])[0]['state'] == 'active'
    step, child, token = prepare(row)
    assert token
    assert flow.execution_allowed(child)
    assert c.check_execution_scope(child, [], db.get_agent('codex')['cwd'])
    assert r.managed(row['id'])
    # Only the route scheduler owns origins or targets of this managed run.
    assert asyncio.run(flow._dispatch_ready_durable_steps(row['id'])) == 0


@pytest.mark.parametrize('route', ['#other@codex>@review', '#squid@codex=2>@review',
                                  '#squid@codex=:1s>@review', '#squid@codex<2>@review',
                                  '#squid@codex>@missing', '#squid@codex>@review>@test'])
def test_route_rejects_unsupported_combinations_without_records(setup, route):
    with pytest.raises((ValueError, PermissionError)):
        r.launch(setup[1], BODY | dict(route=route))
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM route_requests').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM route_roots').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM flow_runs').fetchone()[0] == 0


def test_discovery_and_disabled_rollout(setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=False, **r.DEFAULTS))
    value = shared_tools.discover(setup[1])
    assert value['routes']['supported'] and not value['routes']['enabled']
    assert value['routes']['blockers'] == ['routes_disabled']
    assert r.validate(setup[1], dict(route=BODY['route']))['executable'] is False
    with pytest.raises(PermissionError, match='routes_disabled'):
        r.launch(setup[1], BODY)
    assert value['operations'] == shared_tools.catalog()


def test_nested_routes_and_continuations_share_depth_and_fanout_budget(setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(max_turns=6, max_depth=2))))
    row = activate(setup)
    step, child, token = prepare(row)
    nested = r.launch(token, BODY | dict(route='#squid@review>@test'))
    assert nested['root_id'] == row['root_id'] and nested['depth'] == 2
    synced(child)
    # Parent Flow cannot finish until its nested route finishes.
    assert asyncio.run(flow.complete_durable_step(child))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'running'
    r.reconcile()
    _, grandchild, grandtoken = prepare(nested, 'review')
    assert grandtoken
    with pytest.raises(ValueError, match='route_depth_exhausted'):
        r.launch(grandtoken, BODY)
    with pytest.raises(ValueError, match='route_depth_exhausted'):
        c.create(grandtoken, dict(request_key='local', steps=[dict(mode='isolated', prompt='review')]))
    # Original fanout reserved 3, nested route 2: no new budget per route.
    with db._connection() as conn:
        assert conn.execute('SELECT reserved_turns FROM route_roots').fetchone()[0] == 5
        assert conn.execute('SELECT COUNT(*) FROM route_requests').fetchone()[0] == 2


def test_continuation_reservations_charge_same_root_and_block_git_modes(setup):
    row = activate(setup)
    _, child, token = prepare(row)
    plan = dict(request_key='local', steps=[dict(mode='isolated', prompt='check'), dict(mode='isolated', prompt='check again')])
    with pytest.raises(PermissionError):
        c.create(token, dict(request_key='git', mode='backing_git', prompt='commit'))
    continuation = c.create(token, plan)
    assert c.create(token, plan)['id'] == continuation['id']
    with db._connection() as conn:
        assert conn.execute('SELECT reserved_turns FROM route_roots').fetchone()[0] == 5
    synced(child)
    claimed = c.claim(set())
    assert claimed['id'] == continuation['id']
    user = db.insert_user_message('squid', 'codex', 'local continuation', source='continuation')
    grandchild = db.insert_assistant_message('squid', 'codex', user, source='continuation')
    c.link(claimed['id'], grandchild)
    with db._connection() as conn:
        member = r.membership(conn, grandchild)
        assert member['root_id'] == row['root_id'] and member['depth'] == 2
    assert c.mint(grandchild)
    assert 0 < c.launch_timeout(grandchild, 600) <= r.DEFAULTS['turn_seconds']


def test_atomic_fanout_budget_and_time_reservation(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(concurrency=1, max_seconds=2, turn_seconds=2))))
    row = activate(setup)
    step, child, _ = prepare(row)
    synced(child)
    asyncio.run(flow.complete_durable_step(child))
    targets = [s for s in db.get_flow_steps(row['id']) if s['leg'] == 'target']
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda s: db.claim_flow_step(row['id'], s['step_id'], db._utc_now_iso()), targets))
    assert sum(result is not None for result in results) == 1
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM route_charges WHERE active=1').fetchone()[0] == 1
        assert conn.execute('SELECT SUM(seconds) FROM route_charges').fetchone()[0] <= 2
    # Retries cannot renew a claimed turn's timeout.
    with db._connection() as conn:
        conn.execute('UPDATE route_charges SET started=started-3 WHERE active=1')
        member = r.membership(conn, child)
        assert member


@pytest.mark.parametrize('success', [True, False])
def test_restart_only_admits_proven_parent_sync_and_never_replays_claim(setup, success):
    row = r.launch(setup[1], BODY)
    synced(setup[0], success)
    c.recover()
    r.reconcile(startup=True)
    if not success:
        assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso()) is None
        return
    claimed = db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso())
    assert claimed
    asyncio.run(flow.recover_durable_flows(startup=True))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'error'
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT seconds FROM route_charges').fetchone()[0] == r.DEFAULTS['turn_seconds']


def test_scope_drift_cancellation_and_history_freeze(setup, monkeypatch):
    row = activate(setup)
    _, child, token = prepare(row)
    continuation = c.create(token, dict(request_key='local', steps=[dict(mode='isolated', prompt='check')]))
    r.inspect(setup[1], row['id'], cancel=True)
    r.reconcile()
    assert not flow.execution_allowed(child)
    assert not c.check_execution_scope(child, [], db.get_agent('codex')['cwd'])
    assert db.get_flow_run(row['id'])['status'] == 'cancelled'
    synced(child)
    assert c.claim(set()) is None


def test_api_cli_and_mcp_use_same_contract_and_issuing_instance(setup, monkeypatch, capsys, tmp_path):
    from agent import route_cli
    app = FastAPI()
    app.include_router(router)
    original = httpx.Client
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        assert str(request.url).startswith('http://127.0.0.1:8765/')
        assert request.headers['Authorization'] == 'Bearer ' + setup[1]
        return httpx.Response(200, json=dict(accepted=True))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setenv('SQUID_API_URL', 'http://127.0.0.1:8765')
    monkeypatch.setenv('SQUID_CONTINUE_TOKEN', setup[1])
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(BODY))
    assert route_cli.main(['launch', '--input', str(path)]) == 0
    assert asyncio.run(shared_mcp.invoke('route_launch', BODY)) == dict(accepted=True)
    assert calls[0] == calls[1] == ('POST', '/routes', BODY)
    assert setup[1] not in capsys.readouterr().out
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                     headers={'Authorization': 'Bearer ' + setup[1]}) as client:
            response = await client.post('/routes', json=BODY)
            assert response.status_code == 200
            assert (await client.get('/routes/' + response.json()['id'])).status_code == 200
            assert (await client.post('/routes', json=BODY | dict(prompt='different'))).status_code == 409
            assert (await client.post('/routes', json=BODY | dict(root_id=99))).status_code == 400
    asyncio.run(request())


def test_real_route_loop_runs_fanout_through_durable_executor(setup, monkeypatch):
    from agent import server
    row = activate(setup)
    prompts, started = [], []
    async def prepared(**kwargs):
        prompts.append(kwargs['message'])
        assert kwargs['adhoc'] is False and kwargs['source'] == 'workflow'
        assert 'Local inspection, edits and checks only' in kwargs['message']
        user = db.insert_user_message(kwargs['topic'], kwargs['agent'], kwargs['message'], source='workflow',
            flow_run_id=kwargs['flow_run_id'], flow_route=kwargs['flow_route'], flow_step_id=kwargs['flow_step_id'])
        child = db.insert_assistant_message(kwargs['topic'], kwargs['agent'], user, source='workflow',
            flow_run_id=kwargs['flow_run_id'], flow_route=kwargs['flow_route'], flow_step_id=kwargs['flow_step_id'])
        return dict(effective_message=kwargs['message'], topic='squid', agent=kwargs['agent'], backend='codex',
                    model=None, cwd=None, context_history=[], asst_msg_id=child, user_msg_id=user, response_timeout=600,
                    resume_session_id=None, adhoc=True, lookback=0, code_roots=[], display_prompt=kwargs['message'],
                    source_cwd=None, configured_cwd=db.get_agent(kwargs['agent'])['cwd'], harness='codex', provider='openai')
    async def stream(*args, **kwargs):
        agent, child = args[2], args[7]
        started.append(agent)
        assert c.mint(child)
        assert 0 < c.launch_timeout(child, 600) <= r.DEFAULTS['turn_seconds']
        if agent in {'review', 'test'}:
            # Both targets must start without waiting for the other to finish.
            for _ in range(100):
                if {'review', 'test'} <= set(started):
                    break
                await asyncio.sleep(.01)
            assert {'review', 'test'} <= set(started)
        synced(child)
        await flow.complete_durable_step(child)
        yield 'done'
    monkeypatch.setattr(server, '_prepare_chat_turn', prepared)
    monkeypatch.setattr(server, 'stream_response', stream)
    monkeypatch.setattr('agent.context_sync.maybe_sync', AsyncMock())
    async def run():
        task = asyncio.create_task(r.run())
        try:
            for _ in range(200):
                if r.inspect(setup[1], row['id'])[0]['state'] == 'completed':
                    return
                await asyncio.sleep(.02)
            raise AssertionError('managed route did not complete')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert sorted(started) == ['codex', 'review', 'test']
    assert len(prompts) == 3
    with db._connection() as conn:
        assert conn.execute('SELECT SUM(active) FROM route_charges').fetchone()[0] == 0


def test_nested_turn_limit_rolls_back_without_new_root(setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(max_turns=4))))
    row = activate(setup)
    _, child, token = prepare(row)
    with pytest.raises(ValueError, match='route_turns_exhausted'):
        r.launch(token, BODY | dict(route='#squid@review>@test'))
    with pytest.raises(ValueError, match='route_turns_exhausted'):
        c.create(token, dict(request_key='checks', steps=[dict(mode='isolated', prompt='one'), dict(mode='isolated', prompt='two')]))
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM route_roots').fetchone()[0] == 1
        assert conn.execute('SELECT reserved_turns FROM route_roots').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM flow_runs').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM continuations').fetchone()[0] == 0


def test_competing_launches_reserve_one_plan(setup):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: r.launch(setup[1], BODY), range(2)))
    assert results[0] == results[1]
    with db._connection() as conn:
        assert conn.execute('SELECT reserved_turns FROM route_roots').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM flow_steps').fetchone()[0] == 3


def test_route_scope_and_agent_revision_drift_stops_admission(setup):
    row = activate(setup)
    with db._connection() as conn:
        conn.execute("UPDATE agents SET model='changed' WHERE name='review'")
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso()) is None
    assert r.inspect(setup[1], row['id'])[0]['reason'] == 'route_scope_changed'


@pytest.mark.parametrize('kind', ['watch', 'human_flow'])
def test_watch_and_ordinary_flow_origins_cannot_launch_routes(setup, monkeypatch, kind):
    original = r.principal
    def principal(conn, token):
        parent, credential = original(conn, token)
        credential = dict(credential)
        owner = json.loads(credential['owner_json'])
        owner['kind'] = kind
        credential['owner_json'] = json.dumps(owner)
        return parent, credential
    monkeypatch.setattr(r, 'principal', principal)
    with pytest.raises(PermissionError, match='route_origin_unsupported'):
        r.launch(setup[1], BODY)
    with db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM route_requests').fetchone()[0] == 0


@pytest.mark.parametrize('bad', [True, 0, -1, 86401, 1.2, '3'])
def test_configuration_requires_finite_integer_limits(bad):
    with pytest.raises(ValueError):
        r.configuration(dict(routes=dict(max_turns=bad)))


def test_mcp_catalog_matches_shared_registry():
    pytest.importorskip('mcp')
    from mcp.types import ListToolsRequest
    app = shared_mcp.server()
    result = asyncio.run(app.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list')))
    assert [(t.name, t.inputSchema) for t in result.root.tools] == [(t['name'], t['input_schema']) for t in shared_tools.catalog()]


from tests.test_watch_dispatch import setup as watch_setup, queued as watch_queued


def test_recorded_watch_owner_still_cannot_expand_into_route(watch_setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **r.DEFAULTS))
    monkeypatch.setattr('agent.config.WORKTREE_ISOLATION_ENABLED', True)
    c.configure(True)
    owners.configure(watch_setup[0])
    try:
        _, (_, parent) = watch_queued(watch_setup)
        token = c.mint(parent)
        assert token
        with pytest.raises(PermissionError, match='route_origin_unsupported'):
            r.launch(token, BODY | dict(route='#squid@watch-agent>@watch-agent'))
        assert 'route_origin_unsupported' in shared_tools.discover(token)['routes']['blockers']
        with db._connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM route_roots').fetchone()[0] == 0
    finally:
        c.configure(False)
        owners.configure(None)


def test_global_and_topic_concurrency_ceilings_share_admission_transaction(setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(global_concurrency=1, topic_concurrency=1))))
    first = activate(setup)
    prepare(first)
    user = db.insert_user_message('squid', 'codex', 'Second independent request')
    parent = db.insert_assistant_message('squid', 'codex', user)
    second = activate((parent, c.mint(parent)))
    assert db.claim_flow_step(second['id'], 'origin:0', db._utc_now_iso()) is None
    with db._connection() as conn:
        assert conn.execute('SELECT SUM(active) FROM route_charges').fetchone()[0] == 1


def test_cumulative_time_exhaustion_and_retry_deadline(setup, monkeypatch):
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(max_seconds=1, turn_seconds=1))))
    row = activate(setup)
    _, child, _ = prepare(row)
    with db._connection() as conn:
        conn.execute('UPDATE route_charges SET started=started-2')
    with pytest.raises(RuntimeError, match='time exhausted'):
        c.launch_timeout(child, 600)
    # An unknown outcome keeps the whole charge; ending concurrency does not
    # refund time or permit another turn to manufacture a fresh allowance.
    with db._connection() as conn:
        r.finish_turn(conn, child, proven=False)
    synced(child)
    asyncio.run(flow.complete_durable_step(child))
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso()) is None
    assert r.inspect(setup[1], row['id'])[0]['reason'] == 'route_time_exhausted'


def test_prestart_failure_refunds_time_without_replaying_turn(setup, monkeypatch):
    row = activate(setup)
    step = db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso())
    monkeypatch.setattr('agent.server._prepare_chat_turn', AsyncMock(side_effect=RuntimeError('blocked')))
    assert not asyncio.run(flow._dispatch_claimed_durable_step(row['id'], step))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'error'
    with db._connection() as conn:
        assert tuple(conn.execute('SELECT seconds,active FROM route_charges').fetchone()) == (0, 0)


def test_history_deletion_and_cancel_stop_running_workers_and_join_shutdown(setup, monkeypatch):
    from agent.topic_queue import TopicDispatcher
    from agent import runners
    row = activate(setup)
    _, child, token = prepare(row)
    plan = c.create(token, dict(request_key='check', steps=[dict(mode='isolated', prompt='check')]))
    synced(child)
    claimed = c.claim(set())
    user = db.insert_user_message('squid', 'codex', 'continue', source='continuation')
    grandchild = db.insert_assistant_message('squid', 'codex', user, source='continuation')
    c.link(claimed['id'], grandchild)
    killed = []
    monkeypatch.setattr(runners, 'kill_proc_by_msg_id', killed.append)
    with db._connection() as conn:
        conn.execute('DELETE FROM chat_messages WHERE id=?', (setup[0],))
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    assert grandchild in killed
    assert db.get_message(grandchild)['status'] == 'cancelled'
    assert db.get_flow_run(row['id'])['status'] == 'cancelled'
    with db._connection() as conn:
        assert conn.execute('SELECT cancel_requested FROM continuations WHERE id=?', (plan['id'],)).fetchone()[0] == 1


def test_managed_route_scheduler_cancels_and_joins_dispatch_tasks(setup, monkeypatch):
    row = activate(setup)
    started, stopped = asyncio.Event(), asyncio.Event()
    async def dispatch(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(flow, '_dispatch_claimed_durable_step', dispatch)
    async def run():
        scheduler = asyncio.create_task(r.run())
        await asyncio.wait_for(started.wait(), 2)
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        assert stopped.is_set()
    asyncio.run(run())
    # Cancellation never resets a charged claim for automatic replay.
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso()) is None


def test_fresh_process_recovery_preserves_fanout_reservations_and_ambiguous_claim(setup, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    row = activate(setup)
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso())
    script = '''
import asyncio,json,sys
from agent import config,stats_db as db,continuations as c,flow,routes as r
config.ROUTES=dict(enabled=True,**r.DEFAULTS)
config.WORKTREE_ISOLATION_ENABLED=True
from agent import memory
memory.topic_memory_squid_config=lambda _: {'code_roots':[]}
db.init_db()
c.configure(True)
c.recover()
asyncio.run(flow.recover_durable_flows(startup=True))
r.reconcile(startup=True)
with db._connection() as conn:
    print(json.dumps(dict(steps=[dict(x) for x in conn.execute('SELECT step_id,status,attempt FROM flow_steps ORDER BY step_id')],
        roots=[dict(x) for x in conn.execute('SELECT id,reserved_turns FROM route_roots')],
        charges=[dict(x) for x in conn.execute('SELECT id,seconds,active FROM route_charges')],
        messages=conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0])))
'''
    env = os.environ | dict(SQUID_DB_PATH=str(db._DB_PATH))
    outputs = [subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).parents[1], env=env,
                               capture_output=True, text=True, check=True, timeout=15).stdout for _ in range(2)]
    assert json.loads(outputs[0]) == json.loads(outputs[1])
    value = json.loads(outputs[0])
    assert value['roots'][0]['reserved_turns'] == 3
    assert value['charges'][0]['seconds'] == r.DEFAULTS['turn_seconds']
    assert value['charges'][0]['active'] == 0
    assert value['messages'] == 2
    assert all(step['status'] in {'error', 'cancelled'} for step in value['steps'])


def test_restart_keeps_proven_parent_waiting_for_unstarted_nested_route(setup):
    row = activate(setup)
    _, child, token = prepare(row)
    nested = r.launch(token, BODY | dict(route='#squid@review>@test'))
    synced(child)
    c.recover()
    asyncio.run(flow.recover_durable_flows(startup=True))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'running'
    assert db.claim_flow_step(nested['id'], 'origin:0', db._utc_now_iso())
    assert db.claim_flow_step(nested['id'], 'origin:0', db._utc_now_iso()) is None


def test_expired_issuing_credential_does_not_revoke_durable_route(setup):
    row = r.launch(setup[1], BODY)
    c.revoke(setup[1])
    synced(setup[0])
    r.reconcile()
    assert db.claim_flow_step(row['id'], 'origin:0', db._utc_now_iso())
    with pytest.raises(PermissionError):
        r.inspect(setup[1])


def test_route_status_is_topic_scoped_and_body_limits_are_enforced(setup):
    row = r.launch(setup[1], BODY)
    user = db.insert_user_message('other', 'codex', 'Inspect other topic')
    parent = db.insert_assistant_message('other', 'codex', user)
    other_token = c.mint(parent)
    assert r.inspect(other_token) == []
    with pytest.raises(ValueError, match='route_not_found'):
        r.inspect(other_token, row['id'])
    app = FastAPI()
    app.include_router(router)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.post('/routes', content=' ' * 65537,
                                         headers={'Authorization': 'Bearer ' + setup[1]})
            assert response.status_code == 400
            assert (await client.get('/capabilities')).status_code == 403
    asyncio.run(check())


def test_queued_human_work_defers_route_admission_but_not_running_turn(setup):
    row = activate(setup)
    _, child, _ = prepare(row)
    user = db.insert_user_message('squid', 'codex', 'Human priority')
    human = db.insert_assistant_message('squid', 'codex', user)
    assert flow.execution_allowed(child)
    synced(child)
    asyncio.run(flow.complete_durable_step(child))
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso()) is None
    synced(human)
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso())


def test_deleted_running_route_member_is_still_stopped_by_saved_identity(setup, monkeypatch):
    from agent.topic_queue import TopicDispatcher
    row = activate(setup)
    _, child, _ = prepare(row)
    killed = []
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', killed.append)
    with db._connection() as conn:
        conn.execute('DELETE FROM chat_messages WHERE id=?', (child,))
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    assert child in killed
    assert db.get_flow_run(row['id'])['status'] == 'cancelled'
    with db._connection() as conn:
        assert conn.execute('SELECT active FROM route_charges').fetchone()[0] == 0


def test_native_mcp_stdio_calls_same_issuing_instance_api(monkeypatch):
    pytest.importorskip('mcp')
    import os
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers['Authorization'],
                          json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            data = b'{"id":"accepted","state":"waiting"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    env = os.environ | dict(SQUID_API_URL='http://127.0.0.1:' + str(http.server_port), SQUID_CONTINUE_TOKEN='test-turn-secret')
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-c', 'from agent.cli import main; main()', 'mcp'],
                                        env=env, cwd=str(Path(__file__).parents[1]))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert {tool.name for tool in catalog.tools} == set(shared_tools.OPERATIONS)
                result = await session.call_tool('route_launch', BODY)
                assert not result.isError
                assert json.loads(result.content[0].text) == dict(id='accepted', state='waiting')
    try:
        asyncio.run(run())
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
    assert calls == [('/routes', 'Bearer test-turn-secret', BODY)]


@pytest.mark.parametrize('success', [True, False])
def test_transcript_done_does_not_release_dependencies_before_worker_gate(setup, success):
    row = activate(setup)
    _, child, _ = prepare(row)
    db.update_assistant_message(child, 'Output saved before worker finalization', None, 'done')
    db.insert_run_event(child, 4, 'done', None)
    assert asyncio.run(flow.complete_durable_step(child))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'running'
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso()) is None
    # Transcript-only recovery during a live finalization gap must wait too.
    asyncio.run(flow.recover_durable_flows())
    assert db.get_flow_steps(row['id'])[0]['status'] == 'running'
    c.complete(child, success, 'completed_and_synced' if success else 'promotion_failed')
    assert asyncio.run(flow.complete_durable_step(child))
    assert db.get_flow_steps(row['id'])[0]['status'] == ('done' if success else 'error')
    claimed = db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso())
    assert bool(claimed) == success


def test_restart_does_not_infer_managed_sync_from_transcript_only(setup):
    row = activate(setup)
    _, child, _ = prepare(row)
    db.update_assistant_message(child, 'Done event without worker completion gate', None, 'done')
    db.insert_run_event(child, 4, 'done', None)
    asyncio.run(flow.recover_durable_flows(startup=True))
    assert db.get_flow_steps(row['id'])[0]['status'] == 'error'
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso()) is None


def test_stream_close_retains_capacity_until_independent_worker_stops(setup, monkeypatch):
    from agent.topic_queue import TopicDispatcher
    monkeypatch.setattr('agent.config.ROUTES', dict(enabled=True, **(r.DEFAULTS | dict(global_concurrency=1))))
    row = activate(setup)
    step, child, _ = prepare(row)
    r.dispatch_finished(row['id'], step['step_id'], started=True)
    user = db.insert_user_message('squid', 'codex', 'Other root')
    parent = db.insert_assistant_message('squid', 'codex', user)
    other = activate((parent, c.mint(parent)))
    assert db.claim_flow_step(other['id'], 'origin:0', db._utc_now_iso()) is None
    killed = []
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', killed.append)
    r.inspect(setup[1], row['id'], cancel=True)
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    assert child in killed
    assert db.claim_flow_step(other['id'], 'origin:0', db._utc_now_iso())


def test_one_stop_failure_does_not_starve_other_workers_or_release_failed_slot(setup, monkeypatch):
    from agent.topic_queue import TopicDispatcher
    row = activate(setup)
    _, origin, _ = prepare(row)
    synced(origin)
    asyncio.run(flow.complete_durable_step(origin))
    _, first, _ = prepare(row, 'review')
    _, second, _ = prepare(row, 'test')
    attempts = []
    def stop(child):
        attempts.append(child)
        if child == first:
            raise OSError('Temporary stop failure')
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', stop)
    r.inspect(setup[1], row['id'], cancel=True)
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    assert set(attempts) == {first, second}
    with db._connection() as conn:
        for child, active in ((first, 1), (second, 0)):
            key = r.membership(conn, child)['charge_key']
            assert conn.execute('SELECT active FROM route_charges WHERE id=?', (key,)).fetchone()[0] == active
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', attempts.append)
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    with db._connection() as conn:
        assert conn.execute('SELECT SUM(active) FROM route_charges').fetchone()[0] == 0


def test_failed_signal_return_and_pending_worker_cleanup_keep_capacity(setup, monkeypatch):
    from agent.topic_queue import TopicDispatcher
    row = activate(setup)
    _, child, _ = prepare(row)
    r.inspect(setup[1], row['id'], cancel=True)
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', lambda _: 0)
    monkeypatch.setattr('agent.runners.get_active_msg_ids', lambda: {child})
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    with db._connection() as conn:
        assert conn.execute('SELECT active FROM route_charges').fetchone()[0] == 1
    # A delivered signal deregisters the process, but its topic worker can
    # still be cleaning up. Only that worker's completion releases capacity.
    monkeypatch.setattr('agent.runners.kill_proc_by_msg_id', lambda _: 1)
    monkeypatch.setattr('agent.runners.get_active_msg_ids', lambda: set())
    db.insert_run_event(child, 2, 'processing', None)
    asyncio.run(r.maintain_stops(TopicDispatcher()))
    with db._connection() as conn:
        assert conn.execute('SELECT active FROM route_charges').fetchone()[0] == 1
    c.complete(child, False, 'cancelled')
    with db._connection() as conn:
        assert conn.execute('SELECT active FROM route_charges').fetchone()[0] == 0


def test_topic_worker_exit_releases_prelaunch_rejection_without_success_evidence(setup, monkeypatch):
    from agent.topic_queue import TopicWorker, QueueItem
    row = activate(setup)
    _, child, _ = prepare(row)
    worker = TopicWorker('squid')
    async def reject(item):
        db.update_assistant_message(item.msg_id, 'Preparation rejected', None, 'error')
        await item.out_q.put(None)
    monkeypatch.setattr(worker, '_process', reject)
    async def run():
        await worker.enqueue(QueueItem(seq=0, topic='squid', agent='codex', prompt='local', context_history=[], msg_id=child))
        await worker.q.put(None)
        await worker._run()
    asyncio.run(run())
    with db._connection() as conn:
        assert conn.execute('SELECT active FROM route_charges').fetchone()[0] == 0
        assert not conn.execute('SELECT 1 FROM turn_completion_gates WHERE parent_id=?', (child,)).fetchone()
    asyncio.run(flow.complete_durable_step(child, error='Preparation rejected'))
    assert db.claim_flow_step(row['id'], 'branch:0:target:0', db._utc_now_iso()) is None
