import hashlib

import httpx
import pytest
from fastapi import FastAPI

from agent import stats_db, watch_launch
from agent.server import app as host_app
from agent.watch_api import router
from agent.watch_config import load_watch_configuration
from agent.watch_policy import OPERATIONS
from agent.watch_service import WatchControl, WatchDenied, WatchService


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    config = load_watch_configuration({'watches': {'enabled': True, 'registry': {
        'probes': {'local': {'argv': ['/missing']}}}}})
    now = [100.0]
    service = WatchService(config, clock=lambda: now[0])
    control = WatchControl(service)
    token = control.mint(turn='test', topic='squid', operations=OPERATIONS, expires_at=1000)
    app = FastAPI()
    app.state.watch_service = service
    app.state.watch_control_digest = hashlib.sha256(b'human-secret').digest()
    app.include_router(router)
    return app, service, control, token, now


def client(app, token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                             headers={'Authorization': 'Bearer ' + token})


PAYLOAD = dict(adapter='local', target='task:1', request_key='request')


@pytest.mark.asyncio
async def test_api_create_isolation_validation_and_no_false_monitoring(setup):
    app, service, control, token, now = setup
    async with client(app, token) as c:
        capabilities = (await c.get('/tools')).json()
        assert capabilities['monitoring_enabled'] is False
        row = (await c.post('/watches', json=PAYLOAD)).json()
        assert row['status'] == 'proposed'
        assert (await c.post('/watches', json=PAYLOAD)).json() == row
        assert (await c.get('/watches')).json() == [row]
        assert 'claim_token' not in row
        for extra in [{'topic': 'other'}, {'execution_agent': 'codex'}, {'grant': 'I approve'}]:
            assert (await c.post('/watches', json=PAYLOAD | extra)).status_code == 400
        assert (await c.get('/watches?limit=1000')).status_code == 400
        assert (await c.post('/watches', content='{' )).status_code == 400
        assert (await c.post('/watches', content=b'\xff')).status_code == 400
        assert (await c.post('/watches', content=' ' * 65537)).status_code == 400
        assert (await c.post('/watches', json=PAYLOAD | {'target': 'different'})).status_code == 409
        assert (await c.post('/watches/' + row['id'] + '/activate', json={'version': 0})).json() == {
            'error': 'watch_monitoring_unavailable'}
        assert service.show(token, row['id'])['status'] == 'proposed'
        other = control.mint(turn='other', topic='other', operations=OPERATIONS, expires_at=1000)
        assert (await c.get('/watches/' + row['id'], headers={'Authorization': 'Bearer ' + other})).status_code == 403
        assert (await c.post('/watches/' + row['id'] + '/cancel', json={'version': 9})).status_code == 409
        cancelled = await c.post('/watches/' + row['id'] + '/cancel', json={'version': 0, 'operation_key': 'cancel'})
        assert cancelled.json()['status'] == 'cancelled'
        assert (await c.post('/watches/' + row['id'] + '/cancel', json={'version': 0, 'operation_key': 'cancel'})).json() == cancelled.json()
        control.revoke_turn('test')
        assert (await c.get('/watches')).status_code == 403
        assert (await c.post('/watches/' + row['id'] + '/activate', json={'version': 0})).status_code == 403


@pytest.mark.asyncio
async def test_human_grant_boundary_and_exact_revoke(setup):
    app, service, control, token, now = setup
    row = service.create(token, **PAYLOAD)
    other = service.create(token, **(PAYLOAD | {'request_key': 'other'}))
    path = '/watch-control/squid/watches/' + row['id']
    async with client(app, token) as c:
        assert (await c.post(path + '/grant', json={'expires_at': row['expires_at']})).status_code == 403
        assert (await c.post('/watches/' + row['id'] + '/grant', json={})).status_code == 400
    async with client(app, 'human-secret') as c:
        grant = (await c.post(path + '/grant', json={'expires_at': row['expires_at']})).json()['grant_id']
        wrong = '/watch-control/squid/watches/' + other['id'] + '/revoke'
        assert (await c.post(wrong, json={'grant_id': grant})).status_code == 403
        assert (await c.post(path + '/revoke', json={'grant_id': grant})).json() == {'revoked': True}
        assert (await c.post(path + '/revoke', json={'grant_id': grant})).status_code == 200
        assert (await c.get('/watches')).status_code == 403
        assert (await c.get('/watch-control/other/watches/' + row['id'])).status_code == 403
    assert len(service._credentials) == 1  # temporary human credentials always revoked


@pytest.mark.asyncio
async def test_expiry_restart_and_insecure_transport(setup):
    app, service, control, token, now = setup
    async with client(app, token) as c:
        now[0] = 1000
        assert (await c.get('/tools')).status_code == 403
        app.state.watch_service = WatchService(service.configuration)
        assert (await c.get('/tools')).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('192.0.2.1', 123)),
                                base_url='http://example.com') as c:
        assert (await c.get('/tools', headers={'Authorization': 'Bearer ' + token})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'error', 'cancel', 'close'])
async def test_launch_credentials_revoked_on_every_exit(setup, outcome):
    app, service, control, token, now = setup
    watch_launch.configure(service, 'http://127.0.0.1:1234')
    delivered = []
    async def runner(prompt, **kwargs):
        delivered.append(kwargs['backend_env'])
        yield 'first'
        if outcome == 'error':
            raise RuntimeError('runner failed')
        if outcome == 'cancel':
            import asyncio
            raise asyncio.CancelledError()
        yield 'last'
    stream = watch_launch.run(runner, 'hello', topic='squid', msg_id=1)
    try:
        assert await anext(stream) == 'first'
        env = delivered[0]
        assert env['SQUID_API_URL'] == 'http://127.0.0.1:1234'
        assert env['SQUID_WATCH_CONTROL_TOKEN'] is None
        assert service.discover(env['SQUID_TURN_TOKEN'])['topic'] == 'squid'
        if outcome == 'success':
            assert [part async for part in stream] == ['last']
        elif outcome == 'close':
            await stream.aclose()
        else:
            import asyncio
            with pytest.raises(RuntimeError if outcome == 'error' else asyncio.CancelledError):
                await anext(stream)
        with pytest.raises(WatchDenied):
            service.discover(env['SQUID_TURN_TOKEN'])
    finally:
        await stream.aclose()
        watch_launch.configure(None)


@pytest.mark.asyncio
async def test_persistent_and_disabled_launch_get_no_credential(setup):
    _, service, _, _, _ = setup
    watch_launch.configure(service, 'http://127.0.0.1:1234')
    async def runner(prompt, **kwargs):
        assert kwargs['backend_env']['SQUID_TURN_TOKEN'] is None
        yield 'ok'
    try:
        assert [x async for x in watch_launch.run(runner, '', topic='squid', persistent=True)] == ['ok']
        watch_launch.configure(None)
        assert [x async for x in watch_launch.run(runner, '', topic='squid')] == ['ok']
    finally:
        watch_launch.configure(None)


@pytest.mark.asyncio
async def test_parallel_turns_and_instances_do_not_share_credentials(setup):
    _, service, control, _, _ = setup
    other = WatchService(service.configuration, clock=service.clock)
    watch_launch.configure(service, 'http://127.0.0.1:1111')
    received = {}
    async def runner(prompt, **kwargs):
        received[kwargs['topic']] = kwargs['backend_env']
        assert 'agentsquid tools --help' in prompt
        yield 'ready'
    first = watch_launch.run(runner, '', topic='squid')
    second = watch_launch.run(runner, '', topic='other')
    try:
        await anext(first)
        await anext(second)
        a = received['squid']['SQUID_TURN_TOKEN']
        b = received['other']['SQUID_TURN_TOKEN']
        assert a != b
        with pytest.raises(WatchDenied):
            other.discover(a)
        await first.aclose()
        with pytest.raises(WatchDenied):
            service.discover(a)
        assert service.discover(b)['topic'] == 'other'
    finally:
        await first.aclose()
        await second.aclose()
        watch_launch.configure(None)


def test_child_environment_scrubs_inherited_credentials(monkeypatch):
    from agent.runners import _child_env
    for name in ('SQUID_WATCH_CONTROL_TOKEN', 'SQUID_TURN_TOKEN', 'SQUID_API_URL'):
        monkeypatch.setenv(name, 'inherited-secret')
    env = _child_env()
    assert not any(name in env for name in ('SQUID_WATCH_CONTROL_TOKEN', 'SQUID_TURN_TOKEN', 'SQUID_API_URL'))
    env = _child_env({'SQUID_TURN_TOKEN': 'fresh', 'SQUID_API_URL': 'http://localhost:1234'})
    assert env['SQUID_TURN_TOKEN'] == 'fresh'
    assert 'SQUID_WATCH_CONTROL_TOKEN' not in env


@pytest.mark.asyncio
async def test_host_routes_precede_static_ui(setup, monkeypatch):
    host = host_app
    _, service, _, token, _ = setup
    monkeypatch.setattr(host.state, 'watch_service', service, raising=False)
    async with client(host, token) as c:
        assert (await c.get('/tools')).json()['scope'] == 'observation'
        row = (await c.post('/watches', json=PAYLOAD)).json()
        assert row['status'] == 'proposed'
        assert (await c.get('/watches/' + row['id'])).json() == row


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [[], {}, 42, True, None, '', ' ' * 10, 'x' * 4097])
async def test_invalid_grant_id_is_a_client_error(setup, bad):
    app, service, _, token, _ = setup
    row = service.create(token, **PAYLOAD)
    async with client(app, 'human-secret') as c:
        response = await c.post('/watch-control/squid/watches/' + row['id'] + '/revoke',
                                json={'grant_id': bad})
        assert response.status_code == 400
        assert response.json() == {'error': 'invalid_watch_request'}


@pytest.mark.asyncio
async def test_watch_lock_does_not_block_requests_or_launch_mint(setup, monkeypatch):
    import asyncio
    import threading
    app, service, _, token, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = service.create
    def contended(*args, **kwargs):
        with service._lock:
            entered.set()
            assert release.wait(3)
            return original(*args, **kwargs)
    monkeypatch.setattr(service, 'create', contended)
    watch_launch.configure(service, 'http://127.0.0.1:1234')
    async def runner(prompt, **kwargs):
        yield 'ready'
    stream = watch_launch.run(runner, '', topic='squid')
    async with client(app, token) as c:
        request = asyncio.create_task(c.post('/watches', json=PAYLOAD))
        launch = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            launch = asyncio.create_task(anext(stream))
            # This coroutine must progress while both API write and launch mint
            # are waiting for the same lock. No timing threshold is needed.
            await asyncio.sleep(0.02)
            assert not request.done() and not launch.done()
            release.set()
            assert (await request).status_code == 200
            assert await launch == 'ready'
        finally:
            release.set()
            await request
            if launch is not None:
                await launch
            await stream.aclose()
            watch_launch.configure(None)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['launch', 'human'])
async def test_cancel_during_worker_mint_waits_and_revokes(setup, monkeypatch, source):
    import asyncio
    import threading
    app, service, _, token, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = WatchControl.mint
    def delayed(self, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(self, **kwargs)
    monkeypatch.setattr(WatchControl, 'mint', delayed)
    watch_launch.configure(service, 'http://127.0.0.1:1234')
    async def runner(prompt, **kwargs):
        yield 'ready'
    stream = watch_launch.run(runner, '', topic='squid')
    async with client(app, 'human-secret') as c:
        task = asyncio.create_task(anext(stream) if source == 'launch' else c.post(
            '/watch-control/squid/watches', json=PAYLOAD))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation must not abandon the worker
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(service._credentials) == 1  # fixture credential only
        finally:
            release.set()
            await stream.aclose()
            watch_launch.configure(None)


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,resume', [('/compact', None), ('  /help', None), ('normal request', 'existing-session')])
async def test_launch_preserves_slash_and_resumed_prompts(setup, prompt, resume):
    _, service, _, _, _ = setup
    watch_launch.configure(service, 'http://127.0.0.1:1234')
    async def runner(actual, **kwargs):
        assert actual == prompt
        assert kwargs['backend_env']['SQUID_TURN_TOKEN']
        yield 'ok'
    try:
        assert [x async for x in watch_launch.run(runner, prompt, topic='squid', resume_session_id=resume)] == ['ok']
    finally:
        watch_launch.configure(None)


@pytest.mark.asyncio
async def test_nonloopback_plaintext_listener_skips_credentials_and_hint(setup, caplog):
    _, service, _, _, _ = setup
    watch_launch.configure(service, 'http://192.0.2.1:1234')
    async def runner(prompt, **kwargs):
        assert prompt == 'original'
        assert kwargs['backend_env']['SQUID_TURN_TOKEN'] is None
        yield 'ok'
    try:
        assert [x async for x in watch_launch.run(runner, 'original', topic='squid')] == ['ok']
        assert 'not loopback' in caplog.text
    finally:
        watch_launch.configure(None)


@pytest.mark.asyncio
async def test_grant_audit_records_operator_scope_without_secret(setup):
    import json
    app, service, _, token, _ = setup
    row = service.create(token, **PAYLOAD)
    async with client(app, 'human-secret') as c:
        response = await c.post('/watch-control/squid/watches/' + row['id'] + '/grant',
                                json={'expires_at': row['expires_at']})
        assert response.status_code == 200
    conn = stats_db._connect()
    try:
        audit = json.loads(conn.execute('SELECT user_action FROM watch_grants WHERE id=?',
                           (response.json()['grant_id'],)).fetchone()[0])
        assert audit['principal'].startswith('host-control:')
        assert audit['watch_id'] == row['id'] and audit['topic'] == 'squid'
        assert audit['action'] == 'grant_observation'
        assert 'human-secret' not in json.dumps(audit)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('display', ['/help', '  /compact', 'normal request', None])
async def test_queue_uses_original_request_for_discovery_hint(setup, monkeypatch, display):
    from agent.topic_queue import QueueItem, TopicWorker
    app, service, _, _, _ = setup
    original = display if display is not None else '/help'
    prepared = 'Persistent topic context\n\n' + original if display is not None else original
    captured = []
    async def runner(prompt, **kwargs):
        captured.append(prompt)
        assert kwargs['backend_env']['SQUID_TURN_TOKEN']
        assert 'request_text' not in kwargs
        yield 'done'
    monkeypatch.setattr('agent.runners.run_codex', runner)
    monkeypatch.setattr('agent.git_changes.prepare_trackers', lambda *a, **kw: [])
    monkeypatch.setattr('agent.stats_db.insert_run_event', lambda *a, **kw: None)
    monkeypatch.setattr('agent.stats_db.update_assistant_message', lambda *a, **kw: None)
    watch_launch.configure(service, 'http://127.0.0.2:1111')
    item = QueueItem(seq=0, topic='squid', agent='codex', prompt=prepared,
                     display_prompt=display, context_history=[], backend='codex',
                     model=None, adhoc=True, msg_id=None)
    try:
        await TopicWorker('squid')._process(item)
        assert len(captured) == 1
        assert 'This turn is a one-shot' in captured[0]  # production assembly ran
        assert prepared in captured[0]
        assert ('Squid shared tools:' in captured[0]) == (not original.lstrip().startswith('/'))
    finally:
        watch_launch.configure(None)
