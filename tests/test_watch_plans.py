import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent import stats_db, watch_mcp, watches
from agent.watch_api import router
from agent.watch_config import load_watch_configuration
from agent.watch_plans import PlanAdapter, PlanError, canonical
from agent.watch_policy import OPERATIONS
from agent.watch_service import WatchControl, WatchDenied, WatchService


def plan():
    return dict(version=1, initial='quiet', steps=[dict(
        server='work', tool='read_issue', arguments={'issueId': 'I-1'},
        items='/comments', identity=['/id', '/updatedAt'],
        when={'path': '/resolved', 'equals': False},
        complete={'path': '/nextPage', 'equals': None},
        stop={'path': '/closed', 'equals': True})])


@pytest.fixture
def source(tmp_path, monkeypatch):
    pytest.importorskip('jsonschema')
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    state = dict(comments=[dict(id='C1', updatedAt='v1', resolved=False, body='please fix')],
                 closed=False, nextPage=None)
    schema = dict(type='object', properties={'issueId': {'type': 'string'}},
                  required=['issueId'], additionalProperties=False)
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(nextCursor=None, tools=[SimpleNamespace(name='read_issue', inputSchema=schema),
                SimpleNamespace(name='delete_issue', inputSchema={})])

        async def call_tool(self, tool, arguments):
            calls.append((tool, arguments))
            return SimpleNamespace(isError=False, structuredContent=deepcopy(state), content=[])

    @asynccontextmanager
    async def connect(config, timeout=20):
        yield Session()

    monkeypatch.setattr(watch_mcp, '_connect', connect)
    raw = {'watches': {'enabled': True, 'connections': {'work': {
        'connection': {'transport': 'stdio', 'argv': ['/host/mcp']},
        'read_tools': ['read_issue']}}}}
    service = WatchService(load_watch_configuration(raw), clock=lambda: 100)
    control = WatchControl(service)
    token = control.mint(turn='request', topic='squid', operations=OPERATIONS, expires_at=900)
    return service, control, token, state, schema, calls, raw


@pytest.mark.asyncio
async def test_discovery_validation_and_persisted_polling(source):
    service, control, token, state, schema, calls, raw = source
    tools = await service.discover_observation_tools(token)
    assert [t['name'] for t in tools['operations']] == ['read_issue']
    assert tools['operations'][0]['input_schema'] == schema
    proposal = await service.create_plan(token, target='issue:I-1', plan=plan(), request_key='r')
    assert proposal['status'] == 'proposed'
    assert await service.create_plan(token, target='issue:I-1', plan=plan(), request_key='r') == proposal
    altered = plan()
    altered['initial'] = 'actionable'
    with pytest.raises(ValueError, match='request key'):
        await service.create_plan(token, target='issue:I-1', plan=altered, request_key='r')
    grant = control.record_grant(topic='squid', watch_id=proposal['id'], user_action='human', expires_at=proposal['expires_at'])
    active_token = control.mint(turn='activation', topic='squid', operations=OPERATIONS,
                               expires_at=900, grants=[grant])
    service.transition(active_token, proposal['id'], version=0, action='activate', grant_id=grant)
    control.revoke_turn('request')
    # A fresh service owns the same saved watch; no originating session/token remains.
    restarted = WatchService(load_watch_configuration(raw), clock=lambda: 100)
    adapter = restarted.configuration.adapters['plan']
    row = restarted.claim_due(now=100, lease_seconds=30, limit=1)[0]
    baseline = await adapter.poll(row)
    assert baseline.events == ()
    restarted.finish_poll(row, now=100, observation=baseline)
    state['comments'][0]['updatedAt'] = 'v2'
    row = restarted.claim_due(now=130, lease_seconds=30, limit=1)[0]
    changed = await adapter.poll(row)
    assert len(changed.events) == 1
    restarted.finish_poll(row, now=130, observation=changed)
    # Repeated observations and replays never duplicate durable events.
    row = restarted.claim_due(now=160, lease_seconds=30, limit=1)[0]
    restarted.finish_poll(row, now=160, observation=await adapter.poll(row))
    conn = watches._connect()
    assert conn.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0] == 1
    conn.close()
    state['comments'][0]['updatedAt'] = 'v1'
    row = restarted.claim_due(now=190, lease_seconds=30, limit=1)[0]
    assert not (await adapter.poll(row)).events
    state['closed'] = True
    assert (await adapter.poll(row)).closed
    assert all(call == ('read_issue', {'issueId': 'I-1'}) for call in calls)


@pytest.mark.asyncio
async def test_validation_failures_are_specific_and_do_not_create(source):
    service, _, token, state, schema, calls, _ = source
    invalid = plan()
    invalid['steps'][0]['arguments'] = {'target': 'wrong shape'}
    with pytest.raises(PlanError, match='plan_arguments_invalid'):
        await service.create_plan(token, target='I-1', plan=invalid, request_key='r')
    assert not calls
    invalid = plan()
    invalid['steps'][0]['tool'] = 'delete_issue'
    with pytest.raises(PlanError, match='plan_read_permission_required'):
        await service.validate_plan(token, target='I-1', plan=invalid)
    state['nextPage'] = 'more'
    with pytest.raises(PlanError, match='plan_incomplete_read'):
        await service.validate_plan(token, target='I-1', plan=plan())
    assert service.list(token) == []
    state['nextPage'] = None
    saved = await service.create_plan(token, target='I-1', plan=plan(), request_key='valid')
    schema['required'].append('newField')
    with pytest.raises(PlanError, match='plan_tool_schema_changed'):
        await service.configuration.adapters['plan'].poll(saved)


@pytest.mark.asyncio
async def test_plan_api_and_topic_denial(source):
    service, control, token, state, _, _, raw = source
    app = FastAPI()
    app.state.watch_service = service
    import hashlib
    app.state.watch_control_digest = hashlib.sha256(b'operator').digest()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://localhost',
                                headers={'Authorization': 'Bearer ' + token}) as client:
        assert (await client.get('/watch-tools')).json()['operations']
        human = {'Authorization': 'Bearer operator'}
        assert (await client.get('/watch-control/squid/tools', headers=human)).json()['observation_plans']
        assert (await client.get('/watch-control/squid/watch-tools', headers=human)).json()['operations']
        assert (await client.post('/watch-control/squid/watches/validate', headers=human,
                                  json={'target': 'I-1', 'plan': plan()})).status_code == 200
        preview = await client.post('/watches/validate', json={'target': 'I-1', 'plan': plan()})
        assert preview.status_code == 200
        assert 'validated' not in preview.json()
        assert service.list(token) == []
        state['comments'][0].pop('updatedAt')
        failed = await client.post('/watches', json={'target': 'I-1', 'plan': plan(), 'request_key': 'r'})
        assert failed.json()['error'] == 'plan_field_missing'
        assert 'hint' in failed.json()
        assert (await client.post('/watches', json={'target': 'I-1', 'adapter': 'plan', 'request_key': 'r'})).status_code == 400
    raw['watches']['topics'] = {'denied': {'adapters': []}}
    restricted = WatchService(load_watch_configuration(raw), clock=lambda: 100)
    denied = WatchControl(restricted).mint(turn='other', topic='denied', operations=OPERATIONS, expires_at=900)
    with pytest.raises(WatchDenied):
        await restricted.discover_observation_tools(denied)
    control.revoke_turn('request')
    with pytest.raises(WatchDenied):
        await service.validate_plan(token, target='I-1', plan=plan())


@pytest.mark.asyncio
async def test_different_github_shaped_tools_use_same_evaluator(source):
    service, _, token, state, schema, _, _ = source
    state.clear()
    state.update(head='abc', checkRunId=43, attempt=2, conclusion='failure')
    p = dict(version=1, initial='actionable', steps=[dict(server='work', tool='read_issue',
        arguments={'issueId': 'owner/repo/pull/1'}, identity=['/head', '/checkRunId', '/attempt'],
        when={'path': '/conclusion', 'equals': 'failure'})])
    saved = await service.create_plan(token, target='https://github.com/owner/repo/pull/1', plan=p, request_key='pr')
    adapter = service.configuration.adapters['plan']
    first = await adapter.poll(saved)
    assert len(first.events) == 1
    state['attempt'] = 3
    later = await adapter.poll(saved)
    assert first.events[0][0] != later.events[0][0]


@pytest.mark.parametrize('mutation,code', [
    (lambda p: p.update(version=True), 'plan_invalid_schema'),
    (lambda p: p['steps'][0].update(argv=['sh']), 'plan_invalid_step'),
    (lambda p: p['steps'][0].update(when={'eval': 'code'}), 'plan_unsupported_predicate'),
    (lambda p: p['steps'][0].update(items='not/a/pointer'), 'plan_invalid_path'),
    (lambda p: p['steps'][0].update(server='missing'), 'plan_connection_required'),
])
def test_reject_unsupported_plans_before_io(source, mutation, code):
    service = source[0]
    p = plan()
    mutation(p)
    with pytest.raises(PlanError, match=code):
        service.configuration.adapters['plan'].check(p)


def test_disabled_and_invalid_connections():
    assert not load_watch_configuration({'watches': {'connections': {'x': {
        'connection': {'transport': 'stdio', 'argv': ['/missing']}, 'read_tools': ['read']}}}}).adapters
    with pytest.raises(ValueError):
        load_watch_configuration({'watches': {'connections': {'x': {'read_tools': '*'}}}})


@pytest.mark.asyncio
async def test_real_stdio_discovery_and_different_provider_shapes(tmp_path):
    pytest.importorskip('mcp')
    import sys
    from pathlib import Path
    state_file = tmp_path / 'state.json'
    state = dict(issues={'I-1': dict(comments=[dict(id='c', updatedAt='edit1', resolved=False)],
                                   closed=False, nextPage=None)},
                 prs={'owner/repo#1': dict(checks=[dict(id=9, attempt=1, conclusion='failure')],
                                          merged=False)})
    state_file.write_text(json.dumps(state))
    adapter = PlanAdapter({'work': dict(connection=dict(transport='stdio', argv=(
        sys.executable, str(Path(__file__).parent / 'fixtures/watch_mcp_server.py'),
        '--state', str(state_file))), read_tools=frozenset({'read_issue', 'read_pr'}))})
    discovered = await adapter.discover()
    assert {t['name'] for t in discovered['operations']} == {'read_issue', 'read_pr'}
    issue = plan()
    quiet, _ = await adapter.evaluate(issue)
    assert not quiet.events
    state['issues']['I-1']['comments'][0]['updatedAt'] = 'edit2'
    state_file.write_text(json.dumps(state))
    updated, _ = await adapter.evaluate(issue, cursor=quiet.cursor)
    assert len(updated.events) == 1
    pr = dict(version=1, initial='actionable', steps=[dict(server='work', tool='read_pr',
        arguments={'repository': 'owner/repo', 'number': 1}, items='/checks',
        identity=['/id', '/attempt'], when={'path': '/conclusion', 'equals': 'failure'},
        stop={'path': '/merged', 'equals': True})])
    observed, _ = await adapter.evaluate(pr)
    assert len(observed.events) == 1
    assert observed.events[0][1]['data']['attempt'] == 1
    state['prs']['owner/repo#1']['merged'] = True
    state_file.write_text(json.dumps(state))
    assert (await adapter.evaluate(pr))[0].closed
    pr['steps'][0]['items'] = '/missing'
    with pytest.raises(PlanError, match='plan_field_missing'):
        await adapter.evaluate(pr)


@pytest.mark.asyncio
async def test_revocation_during_validation_and_connection_change(source, monkeypatch):
    service, control, token, state, schema, calls, raw = source
    saved = await service.create_plan(token, target='I-1', plan=plan(), request_key='r')
    raw['watches']['connections']['work']['connection']['argv'] = ['/different/host']
    changed = load_watch_configuration(raw).adapters['plan']
    with pytest.raises(PlanError, match='plan_connection_changed'):
        await changed.poll(saved)
    original = watch_mcp.discover_tools
    async def revoke(config):
        result = await original(config)
        control.revoke_turn('request')
        return result
    monkeypatch.setattr(watch_mcp, 'discover_tools', revoke)
    previous_calls = len(calls)
    with pytest.raises(WatchDenied):
        await service.validate_plan(token, target='I-1', plan=plan())
    assert len(calls) == previous_calls


@pytest.mark.asyncio
async def test_remote_schema_refs_and_excess_records_rejected(source):
    service, _, token, state, schema, _, _ = source
    schema['properties']['issueId'] = {'$ref': 'https://untrusted.invalid/schema'}
    with pytest.raises(PlanError, match='plan_schema_reference_unsupported'):
        await service.validate_plan(token, target='I-1', plan=plan())
    schema['properties']['issueId'] = {'type': 'string'}
    state['comments'] = [dict(id=str(i), updatedAt='v1', resolved=False) for i in range(51)]
    with pytest.raises(PlanError, match='plan_record_limit'):
        await service.validate_plan(token, target='I-1', plan=plan())


@pytest.mark.asyncio
async def test_text_json_result_and_sanitized_failure(monkeypatch):
    result = SimpleNamespace(isError=False, structuredContent=None,
                             content=[SimpleNamespace(type='text', text='{"issue":{"version":3}}')])
    class Session:
        async def call_tool(self, tool, arguments):
            return result
    @asynccontextmanager
    async def connect(config, timeout=20):
        yield Session()
    monkeypatch.setattr(watch_mcp, '_connect', connect)
    assert await watch_mcp.call_observation_tool({}, 'read', {}) == {'issue': {'version': 3}}
    result.content[0].text = 'private token and invalid JSON'
    with pytest.raises(watch_mcp.MCPProbeError, match='^MCP observation failed$'):
        await watch_mcp.call_observation_tool({}, 'read', {})


@pytest.mark.asyncio
async def test_poll_failure_retains_safe_actionable_reason(source):
    service, control, token, state, _, _, _ = source
    proposal = await service.create_plan(token, target='I-1', plan=plan(), request_key='r')
    grant = control.record_grant(topic='squid', watch_id=proposal['id'], user_action='human', expires_at=proposal['expires_at'])
    token = control.mint(turn='activation', topic='squid', operations=OPERATIONS, expires_at=900, grants=[grant])
    service.transition(token, proposal['id'], version=0, action='activate', grant_id=grant)
    state['comments'][0].pop('updatedAt')
    loop = watches.WatchLoop(service.configuration.adapters, clock=lambda: 100,
                             claim=service.claim_due, finish=service.finish_poll, prepare=service.prepare_poll)
    assert await loop.tick() == 1
    row = service.show(token, proposal['id'])
    assert row['last_error'] == 'plan_field_missing'
    assert row['last_error_hint']
    assert row['cursor'] is None
    assert row['failures'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
async def test_all_external_reference_keywords_blocked_without_http(source, keyword):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    service, _, token, _, schema, _, _ = source
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"type":"object"}')
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        schema.clear()
        schema[keyword] = f'http://127.0.0.1:{server.server_port}/schema'
        with pytest.raises(PlanError, match='plan_schema_reference_unsupported'):
            await service.validate_plan(token, target='I-1', plan=plan())
        assert hits == []
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
async def test_validator_registry_never_retrieves_rebased_local_reference():
    from agent.watch_schema import validate
    schema = {'$id': 'https://not-configured.invalid/root', '$ref': '#/missing'}
    assert validate(schema, {}) in ('plan_arguments_invalid', 'plan_schema_reference_unsupported')
    # Local references remain supported.
    assert validate({'$defs': {'arg': {'type': 'object'}}, '$ref': '#/$defs/arg'}, {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['revoke', 'cancel', 'expire'])
async def test_no_later_read_after_observation_authority_changes(source, monkeypatch, action):
    service, control, token, state, _, calls, _ = source
    p = plan()
    p['steps'].append(deepcopy(p['steps'][0]))
    row = await service.create_plan(token, target='I-1', plan=p, request_key='r')
    grant = control.record_grant(topic='squid', watch_id=row['id'], user_action='human', expires_at=row['expires_at'])
    token = control.mint(turn='activation', topic='squid', operations=OPERATIONS, expires_at=900, grants=[grant])
    service.transition(token, row['id'], version=0, action='activate', grant_id=grant)
    calls.clear()
    original = watch_mcp.call_observation_tool
    async def change_authority(*args):
        result = await original(*args)
        if action == 'revoke':
            control.revoke_grant(topic='squid', grant_id=grant, watch_id=row['id'])
        elif action == 'cancel':
            current = service.show(token, row['id'])
            service.transition(token, row['id'], version=current['version'], action='cancel')
        else:
            service.clock = lambda: row['expires_at'] + 1
        return result
    monkeypatch.setattr(watch_mcp, 'call_observation_tool', change_authority)
    loop = watches.WatchLoop(service.configuration.adapters, clock=lambda: 100,
                             claim=service.claim_due, finish=service.finish_poll, prepare=service.prepare_poll)
    assert await loop.tick() == 1
    assert len(calls) == 1
    conn = watches._connect()
    assert conn.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_schema_worker_timeout_does_not_block_loop(source, monkeypatch):
    from agent.watch_plans import validate_arguments
    # A catastrophic regex runs only in the child and is killed at the budget.
    schema = {'type': 'object', 'properties': {'value': {'type': 'string', 'pattern': '^(a+)+$'}}}
    heartbeat = asyncio.Event()
    async def tick():
        await asyncio.sleep(.05)
        heartbeat.set()
    ticker = asyncio.create_task(tick())
    with pytest.raises(PlanError, match='plan_schema_timeout'):
        await validate_arguments(schema, {'value': 'a' * 100 + '!'})
    assert heartbeat.is_set()
    await ticker


@pytest.mark.asyncio
async def test_connection_and_catalog_reused_within_observation(source, monkeypatch):
    service, _, token, _, _, _, _ = source
    counts = dict(connect=0, catalog=0)
    original_connect = watch_mcp._connect
    original_discover = watch_mcp.discover_tools
    @asynccontextmanager
    async def connect(*args, **kwargs):
        counts['connect'] += 1
        async with original_connect(*args, **kwargs) as session:
            yield session
    async def discover(*args):
        counts['catalog'] += 1
        return await original_discover(*args)
    monkeypatch.setattr(watch_mcp, '_connect', connect)
    monkeypatch.setattr(watch_mcp, 'discover_tools', discover)
    p = plan()
    p['steps'] *= 4
    await service.validate_plan(token, target='I-1', plan=p)
    assert counts == dict(connect=1, catalog=1)


@pytest.mark.asyncio
async def test_create_retry_works_with_provider_offline_or_schema_changed(source, monkeypatch):
    service, _, token, _, schema, calls, _ = source
    saved = await service.create_plan(token, target='I-1', plan=plan(), request_key='r')
    schema['required'].append('newField')
    async def unavailable(*args):
        pytest.fail('idempotent retry must not contact provider')
    monkeypatch.setattr(watch_mcp, 'discover_tools', unavailable)
    assert await service.create_plan(token, target='I-1', plan=plan(), request_key='r') == saved
    other = plan()
    other['initial'] = 'actionable'
    with pytest.raises(ValueError, match='request key'):
        await service.create_plan(token, target='I-1', plan=other, request_key='r')


def test_plan_adapter_name_reserved_without_connections():
    with pytest.raises(ValueError, match='reserved'):
        load_watch_configuration({'watches': {'registry': {'probes': {'plan': {'argv': ['/unused']}}}}})


@pytest.mark.asyncio
async def test_waiting_for_service_lock_keeps_event_loop_responsive(source):
    import threading
    service, _, token, _, _, _, _ = source
    held, release = threading.Event(), threading.Event()
    def hold():
        with service._lock:
            held.set()
            release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    held.wait()
    heartbeat = asyncio.Event()
    async def unlock():
        await asyncio.sleep(.05)
        heartbeat.set()
        release.set()
    ticker = asyncio.create_task(unlock())
    try:
        await service.discover_observation_tools(token)
        assert heartbeat.is_set()
    finally:
        release.set()
        thread.join()
        await ticker


@pytest.mark.asyncio
async def test_catalog_limit_returns_specific_setup_reason(source, monkeypatch):
    service, _, token, _, _, _, _ = source
    class Session:
        async def list_tools(self):
            return SimpleNamespace(nextCursor='next-page', tools=[])
    @asynccontextmanager
    async def connect(config, timeout=20):
        yield Session()
    monkeypatch.setattr(watch_mcp, '_connect', connect)
    discovered = await service.discover_observation_tools(token)
    assert discovered['unavailable'] == [{'server': 'work', 'error': 'plan_discovery_limit'}]
    with pytest.raises(PlanError, match='plan_discovery_limit'):
        await service.validate_plan(token, target='I-1', plan=plan())
