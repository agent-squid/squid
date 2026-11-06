"""Authorized monitoring only: lifecycle, leases, and durable ingestion."""
import asyncio
from dataclasses import replace
import hashlib
import sys

from fastapi import FastAPI
import httpx
import pytest

from agent import stats_db, watches
from agent.watch_api import router
from agent.watch_config import load_watch_configuration
from agent.watch_policy import OPERATIONS
from agent.watch_service import WatchControl, WatchDenied, WatchService


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    now = [100.0]
    configuration = load_watch_configuration({'watches': {'enabled': True, 'registry': {
        'probes': {'local': {'argv': [sys.executable, '-c', 'print("unused")']}}}}})
    service = WatchService(configuration, clock=lambda: now[0])
    control = WatchControl(service)
    token = control.mint(turn='test', topic='squid', operations=OPERATIONS, expires_at=1000)
    row = service.create(token, adapter='local', target='task:1', request_key='request', expires_at=900)
    grant = control.record_grant(topic='squid', watch_id=row['id'], user_action='human:1', expires_at=900)
    granted = control.mint(turn='granted', topic='squid', operations=OPERATIONS, grants=[grant], expires_at=1000)
    return service, control, now, row, grant, granted


def rows(table):
    conn = stats_db._connect()
    try:
        return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]
    finally:
        conn.close()


def activate(setup):
    service, _, _, row, grant, token = setup
    return service.transition(token, row['id'], version=0, action='activate', grant_id=grant)


def test_claims_require_current_grants_and_policy(setup):
    service, _, now, row, grant, token = setup
    activate(setup)
    # Simulate a revoked grant surviving a crash before a lifecycle reconciliation.
    conn = stats_db._connect()
    with conn:
        conn.execute('UPDATE watch_grants SET revoked_at=100 WHERE id=?', (grant,))
    conn.close()
    assert service.claim_due(now=100, lease_seconds=60, limit=4) == []
    assert rows('watches')[0]['pause_reason'] == 'grant_required'


@pytest.mark.parametrize('change', ['disabled', 'adapter', 'interval', 'lifetime', 'topic'])
def test_policy_narrowing_blocks_claim(setup, change):
    service, _, _, row, _, _ = setup
    activate(setup)
    config = service.configuration
    if change == 'disabled':
        config = replace(config, enabled=False)
    elif change == 'adapter':
        config = replace(config, policy=replace(config.policy, adapters=frozenset()))
    elif change == 'interval':
        config = replace(config, policy=replace(config.policy, min_poll_seconds=60))
    elif change == 'lifetime':
        config = replace(config, policy=replace(config.policy, max_lifetime_seconds=100))
    else:
        config = replace(config, topics={'squid': replace(config.policy, enabled=False)})
    service.configuration = config
    assert service.claim_due(now=100, lease_seconds=60, limit=4) == []
    assert rows('watches')[0]['pause_reason'] == 'policy_changed'


@pytest.mark.parametrize('boundary', ['prepare', 'finish'])
def test_grant_and_policy_rechecked_after_claim(setup, boundary):
    service, _, _, row, _, _ = setup
    activate(setup)
    claim = service.claim_due(now=100, lease_seconds=60, limit=4)[0]
    service.configuration = replace(service.configuration, enabled=False)
    if boundary == 'prepare':
        assert not service.prepare_poll(claim, now=101)
    else:
        assert not service.finish_poll(claim, now=101, observation=watches.Observation('late', (('event', {}),)))
    assert rows('watch_events') == []
    assert rows('watches')[0]['cursor'] is None
    assert rows('watches')[0]['status'] == 'paused'


def test_expiry_uses_transaction_clock_and_supersedes_pending(setup):
    service, _, now, row, _, _ = setup
    activate(setup)
    claim = service.claim_due(now=100, lease_seconds=60, limit=4)[0]
    assert service.finish_poll(claim, now=101, observation=watches.Observation('a', (('event', {}),)))
    claim = service.claim_due(now=132, lease_seconds=1000, limit=4)[0]
    now[0] = 901
    assert not service.finish_poll(claim, now=133, observation=watches.Observation('too-late'))
    assert rows('watches')[0]['status'] == 'expired'
    assert rows('watch_events')[0]['disposition'] == 'superseded'


@pytest.mark.asyncio
async def test_revocation_during_io_rejects_result(setup):
    service, control, _, row, grant, _ = setup
    activate(setup)
    entered, release = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def poll(self, watch):
            entered.set()
            await release.wait()
            return watches.Observation('late', (('late', {}),))
    loop = watches.WatchLoop({'local': Adapter()}, clock=service.clock,
                             claim=service.claim_due, finish=service.finish_poll, prepare=service.prepare_poll)
    task = asyncio.create_task(loop.tick())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.to_thread(control.revoke_grant, topic='squid', grant_id=grant)
        release.set()
        await task
        assert rows('watch_events') == []
        assert rows('watches')[0]['cursor'] is None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_restart_reclaims_lease_and_deduplicates_events(setup):
    service, control, now, row, _, token = setup
    activate(setup)
    abandoned = service.claim_due(now=100, lease_seconds=60, limit=4)[0]
    service.close()
    recovered = WatchService(service.configuration, clock=service.clock)
    assert recovered.claim_due(now=150, lease_seconds=60, limit=4) == []
    now[0] = 161
    claim = recovered.claim_due(now=161, lease_seconds=60, limit=4)[0]
    assert not recovered.finish_poll(abandoned, now=162, observation=watches.Observation('stale'))
    event = watches.Observation('revision:1', (('event:1', {'summary': 'changed'}),))
    assert recovered.finish_poll(claim, now=162, observation=event)
    now[0] = 193
    claim = recovered.claim_due(now=193, lease_seconds=60, limit=4)[0]
    assert recovered.finish_poll(claim, now=194, observation=event)
    assert len(rows('watch_events')) == 1
    assert rows('watch_events')[0]['disposition'] == 'pending'
    with pytest.raises(WatchDenied):
        recovered.show(token, row['id'])


@pytest.mark.asyncio
async def test_http_activation_resume_and_shutdown_availability(setup):
    service, control, _, row, grant, token = setup
    app = FastAPI()
    app.state.watch_service = service
    app.state.watch_control_digest = hashlib.sha256(b'operator').digest()
    app.include_router(router)
    service.start_monitoring()
    path = '/watch-control/squid/watches/' + row['id']
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                    headers={'Authorization': 'Bearer operator'}) as client:
            denied = await client.post(path + '/activate', json={'version': 0, 'grant_id': 'unknown'})
            assert denied.status_code == 403
            active = (await client.post(path + '/activate', json={'version': 0, 'grant_id': grant})).json()
            assert active['status'] == 'active'
            paused = (await client.post(path + '/pause', json={'version': active['version']})).json()
            assert paused['status'] == 'paused'
            resumed = (await client.post(path + '/resume', json={'version': paused['version'], 'grant_id': grant})).json()
            assert resumed['status'] == 'active'
            # An agent with the grant in its credential has equivalent scope.
            assert service.discover(token)['monitoring_enabled'] is True
            assert service.discover(token)['dispatch_enabled'] is False
            await service.stop_monitoring()
            assert (await client.post(path + '/resume', json={'version': resumed['version'], 'grant_id': grant})).status_code == 409
    finally:
        await service.stop_monitoring()
        service.close()


@pytest.mark.asyncio
async def test_disabled_configuration_does_not_start_worker(setup):
    service, _, _, _, _, _ = setup
    service.configuration = replace(service.configuration, enabled=False)
    service.start_monitoring()
    assert service._monitoring_task is None


@pytest.mark.parametrize('execution', [False, True, 'flow'])
@pytest.mark.parametrize('generic_plan', [False, True])
def test_real_host_restart_keeps_cursor_events_and_monitoring(tmp_path, execution, generic_plan):
    if generic_plan:
        pytest.importorskip('mcp')
    import json
    import os
    from pathlib import Path
    import socket
    import sqlite3
    import subprocess
    import time
    import yaml

    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'host'
    (home / '.squid').mkdir(parents=True)
    state = home / 'source.json'
    def source(revision, changed=False):
        temporary = state.with_suffix('.new')
        snapshot = {'revision': revision, 'changed': changed, 'summary': revision}
        temporary.write_text(json.dumps({'prs': {'owner/repo#1': snapshot}} if generic_plan else snapshot))
        temporary.replace(state)
    source('v1')
    probe = home / 'probe.py'
    probe.write_text('import pathlib,sys\nprint(pathlib.Path(sys.argv[1]).read_text())\n')
    database = home / 'watch.db'
    environment = dict(os.environ, SQUID_DB_PATH=str(database), SQUID_LOG_DIR=str(home / 'logs'),
                       SQUID_WATCH_CONTROL_TOKEN='test-operator-' * 4, PYTHONPATH=str(root))
    process = None
    with socket.socket() as listener, (home / 'process.log').open('w') as log:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        configuration = yaml.safe_load((root / 'config' / 'squid.yaml.example').read_text())
        configuration['server']['host'] = '127.0.0.1'
        configuration['server']['port'] = port
        configuration['test_harness_enabled'] = True
        configuration['watches'] = {'enabled': True, 'policy': {'execution_enabled': bool(execution), 'max_runs': 2}, 'registry': {'probes': {
            'local': {'argv': [sys.executable, str(probe), str(state)]}}}}
        if generic_plan:
            configuration['watches'].pop('registry')
            configuration['watches']['connections'] = {'github': {
                'connection': {'transport': 'stdio', 'argv': [sys.executable,
                    str(root / 'tests/fixtures/watch_mcp_server.py'), '--state', str(state)]},
                'read_tools': ['read_pr']}}
        (home / '.squid' / 'squid.yaml').write_text(yaml.safe_dump(configuration))
        def start():
            return subprocess.Popen([sys.executable, str(root / 'tests/fixtures/watch_host.py'),
                                     str(home), str(listener.fileno()), '--fast-echo'], env=environment,
                                    pass_fds=(listener.fileno(),), stdout=log, stderr=log)
        def stop():
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise
        def wait_for(check, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail('Disposable watch host exited; see ' + str(home / 'process.log'))
                try:
                    result = check()
                    if result:
                        return result
                except (httpx.HTTPError, sqlite3.Error):
                    pass
                time.sleep(.05)
            pytest.fail('Timed out waiting for disposable watch host')
        def db(sql):
            with sqlite3.connect(database) as conn:
                return conn.execute(sql).fetchall()
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=.5, trust_env=False,
                          headers={'Authorization': 'Bearer ' + environment['SQUID_WATCH_CONTROL_TOKEN']}) as client:
            try:
                process = start()
                first_boot = wait_for(lambda: client.get('/health').json().get('boot_time'))
                path = '/watch-control/squid/watches'
                payload = {'adapter': 'local', 'target': 'task:1', 'request_key': 'create'}
                if generic_plan:
                    payload.pop('adapter')
                    payload['plan'] = dict(version=1, initial='quiet', steps=[dict(server='github',
                        tool='read_pr', arguments={'repository': 'owner/repo', 'number': 1},
                        identity=['/revision'], when={'path': '/changed', 'equals': True})])
                # Plan creation includes MCP negotiation and real read validation.
                response = client.post(path, json=payload, timeout=10)
                assert response.status_code == 200
                row = response.json()
                path += '/' + row['id']
                grant = client.post(path + '/grant', json={'expires_at': row['expires_at']}).json()['grant_id']
                assert client.post(path + '/activate', json={'version': 0, 'grant_id': grant}).json()['status'] == 'active'
                wait_for(lambda: client.get(path).json().get('cursor') == ('[]' if generic_plan else 'v1'))
                assert db('SELECT COUNT(*) FROM watch_events')[0][0] == 0
                def reschedule():
                    current = client.get(path).json()
                    paused = client.post(path + '/pause', json={'version': current['version']}).json()
                    resumed = client.post(path + '/resume', json={'version': paused['version'], 'grant_id': grant})
                    assert resumed.status_code == 200
                if execution:
                    agent_response = client.post('/config/agents', json={'name': 'watch-echo', 'harness': 'echo', 'provider': 'echo'})
                    assert agent_response.status_code == 200
                    target = dict(agent='watch-echo')
                    if execution == 'flow':
                        assert client.post('/config/agents', json={'name': 'watch-review', 'harness': 'echo', 'provider': 'echo'}).status_code == 200
                        target = dict(flow_route='#squid@watch-echo>@watch-review')
                    execution_response = client.post(path + '/execution-grant', json=dict(**target,
                        instructions='Inspect the event and report locally', max_runs=2, max_attempts=1, request_key='execute'))
                    assert execution_response.status_code == 200
                source('v2', True)
                reschedule()
                wait_for(lambda: db('SELECT COUNT(*) FROM watch_events')[0][0] == 1)
                assert db('SELECT COUNT(*) FROM watch_events')[0][0] == 1
                if execution:
                    wait_for(lambda: db("SELECT COUNT(*) FROM watch_runs WHERE state='succeeded'")[0][0] == 1)
                saved_expiry = client.get(path).json()['expires_at']
                monitor_id = db('SELECT msg_id FROM watch_messages')[0][0]
                stop()
                # Exercise overdue startup recovery without a 30-second sleep.
                with sqlite3.connect(database) as conn:
                    conn.execute('UPDATE watches SET next_poll=0')
                while time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) == first_boot:
                    time.sleep(.05)
                process = start()
                second_boot = wait_for(lambda: (value if value != first_boot else None)
                                       if (value := client.get('/health').json().get('boot_time')) else None)
                assert second_boot != first_boot
                assert db('SELECT msg_id FROM watch_messages') == [(monitor_id,)]
                assert db("SELECT COUNT(*) FROM chat_messages WHERE source='monitor'")[0][0] == 1
                wait_for(lambda: db('SELECT next_poll FROM watches')[0][0] > 0)
                current = client.get(path).json()
                assert current['status'] == 'active' and current['cursor'] == ('[]' if generic_plan else 'v2')
                assert current['expires_at'] == saved_expiry
                assert db('SELECT COUNT(*) FROM watch_events')[0][0] == 1
                source('v3', True)
                reschedule()
                wait_for(lambda: db('SELECT COUNT(*) FROM watch_events')[0][0] == 2)
                if execution:
                    wait_for(lambda: db("SELECT COUNT(*) FROM watch_runs WHERE state='succeeded'")[0][0] == 2)
                    assert db("SELECT COUNT(*) FROM watch_events WHERE disposition='handled'")[0][0] == 2
                    assert len(client.get(path + '/runs').json()) == 2
                    source('v4', True)
                    reschedule()
                    wait_for(lambda: client.get(path).json().get('pause_reason') == 'execution_budget')
                    assert db("SELECT COUNT(*) FROM watch_runs")[0][0] == 2
                else:
                    assert db("SELECT COUNT(*) FROM watch_events WHERE disposition='pending'")[0][0] == 2
                assert db("SELECT COUNT(*) FROM chat_messages WHERE role='assistant' AND source='watch'")[0][0] == (4 if execution == 'flow' else 2 if execution else 0)
                if execution == 'flow':
                    assert db("SELECT COUNT(*) FROM flow_steps WHERE status='done'")[0][0] == 4
                    assert db("SELECT COUNT(*) FROM watch_flow_turns WHERE started_at IS NOT NULL")[0][0] == 4
                    assert all(r[0] for r in db('SELECT flow_run_id FROM watch_runs'))
                cancelled = client.post(path + '/cancel', json={'version': client.get(path).json()['version']})
                assert cancelled.json()['status'] == 'cancelled'
            finally:
                stop()


def test_lease_expiry_uses_current_clock_at_ingestion(setup):
    service, _, now, _, _, _ = setup
    activate(setup)
    claim = service.claim_due(now=100, lease_seconds=60, limit=4)[0]
    now[0] = 161
    assert not service.finish_poll(claim, now=101, observation=watches.Observation('late'))
    assert rows('watches')[0]['cursor'] is None


@pytest.mark.asyncio
async def test_stopping_loop_cancels_inflight_probe_and_clears_availability(setup):
    service, _, _, _, _, _ = setup
    activate(setup)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def poll(self, watch):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    service.configuration = replace(service.configuration, adapters={'local': Adapter()})
    service.start_monitoring()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await service.stop_monitoring()
        assert cancelled.is_set()
        assert service.monitoring_enabled is False
        assert rows('watches')[0]['lease_until'] is not None
        assert rows('watch_events') == []
    finally:
        await service.stop_monitoring()
