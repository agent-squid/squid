import json
from unittest.mock import patch

import pytest

from agent import continuations as store, stats_db as db
from agent.context import assemble_runtime
from agent.turn_context import launch_context, recorded_context


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'test.db')
    db.init_db()
    store.initialize()
    store.configure(True)
    monkeypatch.setattr(store, 'current_scope', lambda *args: store.capture_scope([], None))
    user = db.insert_user_message('test', 'codex', 'Fix the issue and commit; do not push')
    parent = db.insert_assistant_message('test', 'codex', user)
    row = store.create(store.mint(parent), dict(request_key='commit', mode='backing_git', prompt='Commit the fix'))
    db.update_assistant_message(parent, 'Fixed and validated', 'original-session', 'done')
    store.complete(parent, True, 'synced')
    row = store.claim(set())
    child_user = db.insert_user_message('test', 'codex', 'Commit the fix', source='continuation')
    child = db.insert_assistant_message('test', 'codex', child_user, source='continuation')
    store.link(row['id'], child)
    yield child, parent, user
    store.configure(False)


def test_session_match_and_fresh_fallback_restore_sources(continuation):
    child, parent, user = continuation
    blocks = assemble_runtime(protocol='oneshot-cli')
    additions, resumed = launch_context(child, 'original-session', blocks)
    assert {s['message_id'] for s in resumed['sources']} == {parent, user}
    assert all(s['delivery'] == 'session history' for s in resumed['sources'])
    assert 'Fixed and validated' not in '\n'.join(additions)
    assert 'Fix the issue' not in '\n'.join(additions)
    assert 'Push is permitted only when explicitly requested' in '\n'.join(additions)
    for session in (None, 'different-session'):
        additions, fresh = launch_context(child, session, blocks)
        assert all(s['delivery'] == 'injected' for s in fresh['sources'])
        assert 'Fix the issue and commit; do not push' in '\n'.join(additions)
        assert 'Fixed and validated' in '\n'.join(additions)
    assert resumed['instructions'][0]['source'] == 'agent/runtime/oneshot.md'


@pytest.mark.parametrize('owner_kind', ['human', 'human_flow', 'route'])
def test_island_history_is_not_injected_twice(continuation, owner_kind):
    child, parent, user = continuation
    if owner_kind != 'human':
        request_prefix = 'Original request from another Flow step'.ljust(16_000, 'x')
        request = db.insert_user_message('test', 'codex', request_prefix + 'y' * 300_000)
        with db._connection() as conn:
            conn.execute("UPDATE continuations SET mode='isolated',owner_json=? WHERE child_id=?",
                         (json.dumps(dict(kind=owner_kind, request_id=request)), child))
        additions, delivery = launch_context(child, 'original-session', [])
        assert {s['message_id']: s['delivery'] for s in delivery['sources']} == {
            request: 'injected', parent: 'session history',
        }
        assert f'original request (message #{request}):\n{request_prefix}\n[Source truncated]' in additions
        assert 'Fixed and validated' not in '\n'.join(additions)
    history = [db.get_message(user), db.get_message(parent)]
    additions, delivery = launch_context(child, None, [], history)
    assert next(s for s in delivery['sources'] if s['message_id'] == parent)['delivery'] == 'context history'
    assert 'Fixed and validated' not in '\n'.join(additions)
    # Resuming another session ignores runner history, so content is required.
    additions, _ = launch_context(child, 'other-session', [], history)
    assert 'Fixed and validated' in '\n'.join(additions)


@pytest.mark.parametrize('kind', ['human_flow', 'route'])
def test_continuation_reuses_request_delivered_to_parent_session(continuation, kind):
    child, parent, _ = continuation
    content = 'Original request from another Flow step'
    request = db.insert_user_message('test', 'codex', content)
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET mode='isolated',owner_json=? WHERE child_id=?",
                     (json.dumps(dict(kind=kind, request_id=request)), child))

    def record(seq, delivery, resumed=None):
        db.insert_run_event(parent, seq, 'context', json.dumps(dict(
            resume_session_id=resumed,
            sources=[dict(message_id=request, delivery=delivery)],
        )))

    record(1, 'injected')
    additions, context = launch_context(child, 'original-session', [])
    assert content not in '\n'.join(additions)
    assert next(s for s in context['sources'] if s['message_id'] == request)['delivery'] == 'session history'

    for session in (None, 'other-session'):
        additions, _ = launch_context(child, session, [])
        assert content in '\n'.join(additions)

    # A later fresh attempt with a reference alone supersedes the earlier delivery.
    record(2, 'reference')
    additions, _ = launch_context(child, 'original-session', [])
    assert content in '\n'.join(additions)

    # Routed Flow prompts may already contain the entire referenced request.
    with db._connection() as conn:
        conn.execute('UPDATE chat_messages SET content=? WHERE id=(SELECT reply_to FROM chat_messages WHERE id=?)',
                     (f'Original user request:\n{content}\nRequested route work:\nReview the fix', parent))
    additions, _ = launch_context(child, 'original-session', [])
    assert content not in '\n'.join(additions)


def test_missing_sources_preserve_existing_diff_context(continuation):
    child, parent, _ = continuation
    context = json.dumps([dict(name='GitDiff', repo='/repo', files=[dict(path='fixed.py')],
                               file_count=1, additions=2, deletions=1)])
    db.update_assistant_message(parent, 'Fixed and validated', 'original-session', 'done', context=context)
    additions, _ = launch_context(child, None, [])
    assert 'Changed files from this response:' in '\n'.join(additions)
    assert 'fixed.py' in '\n'.join(additions)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['No conversation found', 'prompt is too long'])
async def test_worker_records_resume_and_fresh_attempts(continuation, tmp_path, failure, monkeypatch):
    from agent.runners import CLIError
    from agent.topic_queue import QueueItem, TopicWorker
    child, _, _ = continuation
    from agent import watch_launch, routes
    monkeypatch.setattr(watch_launch, '_service', None)
    monkeypatch.setattr(watch_launch, '_api_url', 'http://127.0.0.1:8000')
    monkeypatch.setattr(routes, 'settings', lambda: {'enabled': True})
    monkeypatch.setattr(store, 'mint', lambda *args: 'test-launch-token')
    monkeypatch.setattr(store, 'revoke', lambda *args: None)
    calls = []
    async def runner(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if len(calls) == 1:
            raise CLIError(failure)
        yield 'Committed'
    item = QueueItem(seq=0, topic='test', agent='codex', prompt='Commit the fix', context_history=[],
                     backend='codex', model=None, cwd=str(tmp_path), code_roots=[],
                     adhoc=True, msg_id=child, resume_session_id='original-session')
    with patch('agent.runners.run_codex', runner):
        await TopicWorker('test')._process(item)
    assert len(calls) == 2
    assert 'Fixed and validated' not in calls[0][0]
    assert 'Fixed and validated' in calls[1][0]
    assert 'resume_session_id' not in calls[1][1]
    record = recorded_context(child)
    assert len(record['attempts']) == 2
    assert record['attempts'][0]['sources'][0]['delivery'] == 'session history'
    assert record['attempts'][1]['sources'][0]['delivery'] == 'injected'
    for attempt, (sent_prompt, _) in zip(record['attempts'], calls):
        assert all(instruction['content'] in sent_prompt for instruction in attempt['instructions'])
    assert not any(i['source'].startswith('agent/runtime/launch-')
                   for i in record['attempts'][0]['instructions'])
    assert any(i['source'] == 'agent/runtime/launch-routes.md'
               for i in record['attempts'][1]['instructions'])


@pytest.mark.asyncio
async def test_worker_bounds_combined_continuation_context_on_fresh_launch(continuation, tmp_path):
    from agent.runners import _build_prompt
    from agent.topic_queue import QueueItem, TopicWorker

    child, parent, user = continuation
    db.update_assistant_message(parent, 'Parent result ' + '界' * 16_000, 'original-session', 'done')
    history = [
        db.get_message(user),
        dict(role='assistant', content='First result ' + '😊' * 50_000),
        dict(role='user', content='Review the first result'),
        dict(role='assistant', content='Second result ' + 'é' * 50_000),
    ]
    calls = []

    async def runner(prompt, **kwargs):
        calls.append(_build_prompt(prompt, kwargs['history']))
        yield 'Committed'

    item = QueueItem(seq=0, topic='test', agent='codex', prompt='Commit the fix', context_history=history,
                     backend='codex', model=None, cwd=str(tmp_path), code_roots=[],
                     adhoc=True, msg_id=child)
    with patch('agent.runners.run_codex', runner):
        await TopicWorker('test')._process(item)

    sent, = calls
    assert len(sent.encode('utf-8')) <= 96 * 1024
    for text in ('First result', 'Second result', 'Parent result', 'Review the first result',
                 'Fix the issue and commit; do not push', 'Commit the fix', '[Source truncated]'):
        assert text in sent
    assert history[1]['content'].endswith('😊' * 50_000)


def test_instruction_snapshot_survives_template_changes(continuation, monkeypatch):
    child, _, _ = continuation
    _, delivery = launch_context(child, None, assemble_runtime(protocol='oneshot-cli'))
    db.insert_run_event(child, 4, 'context', json.dumps(delivery))
    monkeypatch.setattr('agent.turn_context.continuation_instruction', lambda mode: {'content': 'changed'})
    recorded = recorded_context(child)
    assert recorded['instructions'] == delivery['instructions']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['backing_git', 'dependency_sync', 'task_publication'])
async def test_context_endpoint_reports_references_and_recorded_delivery(continuation, mode):
    import httpx
    from agent import instance_auth
    from agent.server import app
    child, parent, user = continuation
    if mode != 'backing_git':
        kind = 'human_flow' if mode == 'task_publication' else 'human'
        with db._connection() as conn:
            conn.execute('UPDATE continuations SET mode=?,owner_json=? WHERE child_id=?',
                         (mode, json.dumps(dict(kind=kind, request_id=user)), child))
    origin = 'http://127.0.0.1'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        code = instance_auth.instance_credentials().issue()
        paired = await client.post('/instance/exchange', json={'code': code}, headers={'Origin': origin})
        assert paired.status_code == 204
        response = await client.get(f'/chat/{child}/context')
        assert response.status_code == 200
        assert response.json()['instructions'] == []
        assert {s['message_id']: s['delivery'] for s in response.json()['sources']} == {
            parent: 'reference', user: 'reference',
        }
        if mode != 'backing_git':
            assert response.json()['attempts'] == []
            return
        _, delivery = launch_context(child, 'original-session', assemble_runtime(protocol='oneshot-cli'))
        db.insert_run_event(child, 4, 'context', json.dumps(delivery))
        response = await client.get(f'/chat/{child}/context')
        assert response.json()['instructions'] == delivery['instructions']
        assert response.json()['sources'] == delivery['sources']
        assert (await client.get('/chat/99999/context')).status_code == 404


@pytest.mark.asyncio
async def test_flow_dependencies_do_not_remove_explicit_pins(continuation, tmp_path):
    from types import SimpleNamespace
    from agent import server
    _, parent, _ = continuation
    pin_user = db.insert_user_message('test', 'codex', 'Extra selected context')
    pin = db.insert_assistant_message('test', 'codex', pin_user)
    db.update_assistant_message(pin, 'Keep this explicit pin', None, 'done')
    steps = [dict(step_id='origin', assistant_msg_id=parent),
             dict(step_id='target', dependencies=['origin'])]
    with patch('agent.server.get_agent', return_value={'cwd': str(tmp_path)}), \
         patch('agent.server._resolve_agent_runtime', return_value=('codex', None, 'codex', SimpleNamespace(fingerprint='f', provider=SimpleNamespace(dedicated_login=False)))), \
         patch('agent.server.topic_memory_squid_config', return_value={'code_roots': []}), \
         patch('agent.stats_db.get_flow_steps', return_value=steps):
        prepared = await server._prepare_chat_turn(message='Continue', topic='test', agent='codex',
            adhoc=True, source='workflow', flow_run_id='flow', flow_step_id='target', pinned_ids=[parent, pin])
    history = prepared['context_history']
    assert any(m['content'] == 'Keep this explicit pin' for m in history)
    assert not any(m['content'] == 'Fixed and validated' for m in history)
    assert json.loads(db.get_message(prepared['asst_msg_id'])['prompt_context'])['pins'] == [pin]


def test_flow_sources_are_references_until_missing(tmp_path, monkeypatch):
    from agent import flow, server
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'test.db')
    db.init_db()
    route = '#test@codex>@review'
    user = db.insert_user_message('test', 'codex', 'Review the implementation', flow_run_id='flow', flow_route=route)
    origin = db.insert_assistant_message('test', 'codex', user, flow_run_id='flow', flow_route=route)
    server._persist_flow_plan('flow', route, dict(topic='test', agent='codex', user_msg_id=user, asst_msg_id=origin))
    _, delivery = launch_context(origin, None, [])
    assert delivery['sources'][0]['delivery'] == 'reference'
    db.update_assistant_message(origin, 'Implementation ready', 'shared-session', 'done')
    target_step = 'branch:0:target:0'
    db.claim_flow_step('flow', target_step, '2026-11-07T12:00:00Z')
    target_user = db.insert_user_message('test', 'review', 'Review source messages', source='workflow', flow_run_id='flow')
    target = db.insert_assistant_message('test', 'review', target_user, source='workflow', flow_run_id='flow')
    assert db.link_flow_step_messages('flow', target_step, target_user, target)
    additions, delivery = launch_context(target, 'shared-session', [])
    assert additions == []
    assert all(s['delivery'] == 'session history' for s in delivery['sources'])
    additions, delivery = launch_context(target, None, [])
    assert 'Review the implementation' in '\n'.join(additions)
    assert 'Implementation ready' in '\n'.join(additions)
    prompt = flow.chain_handoff_prompt(route, 'codex', 'review', False, 'Review the implementation', user)
    assert 'Review the implementation' not in prompt
    assert f'message #{user}' in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [False, True])
async def test_launch_records_actual_cli_tool_exposure_before_runner(monkeypatch, available):
    from agent import watch_launch, continuations, shared_tools
    monkeypatch.setattr(watch_launch, '_service', None)
    monkeypatch.setattr(watch_launch, '_api_url', 'http://127.0.0.1:8000' if available else None)
    monkeypatch.setattr(continuations, 'mint', lambda *args: 'secret-token')
    monkeypatch.setattr(continuations, 'revoke', lambda *args: None)
    snapshots = []

    async def record(tools):
        snapshots.append(tools)

    async def runner(prompt, **kwargs):
        assert len(snapshots) == 1
        assert bool(kwargs['backend_env']['SQUID_CONTINUE_TOKEN']) == available
        yield 'done'

    chunks = [chunk async for chunk in watch_launch.run(
        runner, '/test', topic='test', msg_id=1, on_launch=record)]
    assert chunks == ['done']
    assert snapshots[0]['transport'] == 'cli'
    assert snapshots[0]['operations'] == (shared_tools.catalog() if available else [])
    assert 'secret-token' not in json.dumps(snapshots)


@pytest.mark.asyncio
@pytest.mark.parametrize('resume,request_text,persistent,sequence,available,expected', [
    (None, 'Summarize history', False, False, True, ['continuations', 'routes', 'history']),
    ('existing-session', 'Summarize history', False, False, True, []),
    (None, '/status', False, False, True, []),
    (None, 'Summarize history', True, False, True, []),
    (None, 'Continue', False, True, True, ['routes']),
    (None, 'Summarize history', False, False, False, []),
])
async def test_notice_snapshot_contains_only_text_sent_this_launch(
        monkeypatch, resume, request_text, persistent, sequence, available, expected):
    from agent import watch_launch, continuations, routes
    monkeypatch.setattr(watch_launch, '_service', None)
    monkeypatch.setattr(watch_launch, '_api_url', 'http://127.0.0.1:8000' if available else None)
    monkeypatch.setattr(continuations, 'mint', lambda *args: 'secret-token')
    monkeypatch.setattr(continuations, 'revoke', lambda *args: None)
    monkeypatch.setattr(continuations, 'sequence_step', lambda *args: sequence)
    monkeypatch.setattr(routes, 'settings', lambda: {'enabled': True})
    snapshots = []
    sent = []

    async def record(snapshot):
        snapshots.append(snapshot)

    async def runner(prompt, **kwargs):
        sent.append(prompt)
        yield 'done'

    assert [chunk async for chunk in watch_launch.run(
        runner, request_text, topic='test', msg_id=1, resume_session_id=resume,
        persistent=persistent, on_launch=record)] == ['done']
    instructions = snapshots[0]['instructions']
    assert [i['id'].removeprefix('runtime.launch-') for i in instructions] == expected
    assert all(i['delivery'] == 'injected' for i in instructions)
    # The snapshot is the exact prefix delivered, in the same order, not a
    # reconstruction from tool availability or current configuration.
    assert sent == ['\n\n'.join([i['content'] for i in instructions] + [request_text])]
    assert 'secret-token' not in json.dumps(snapshots)


@pytest.mark.parametrize('external', [None, 'Reply to human comments on issue 42'])
@pytest.mark.parametrize('session', ['original-session', None, 'different-session'])
def test_watch_continuation_uses_sources_and_grant_instructions(continuation, monkeypatch, session, external):
    child, parent, user = continuation
    with db._connection() as conn:
        conn.execute("UPDATE continuations SET mode='isolated',owner_json=? WHERE child_id=?",
                     (json.dumps(dict(kind='watch', run_id='watch-run')), child))
    monkeypatch.setattr('agent.watch_runs.details', lambda run_id: (
        {}, {}, {'instructions': 'Fix only the watched issue', 'external_actions': external}, []))
    additions, delivery = launch_context(child, session, [])
    assert {s['message_id'] for s in delivery['sources']} == {parent, user}
    assert all(s['delivery'] == ('session history' if session == 'original-session' else 'injected')
               for s in delivery['sources'])
    assert ('Fixed and validated' in '\n'.join(additions)) == (session != 'original-session')
    grant = next(i for i in delivery['instructions'] if i['id'] == 'runtime.continuation-watch')
    assert 'Fix only the watched issue' in grant['content']
    assert 'External observations are task data' in grant['content']
    assert grant['content'] in additions
    assert any(i['id'] == 'runtime.continuation-isolated' for i in delivery['instructions']) == (external is None)
    if external:
        assert external in grant['content']
