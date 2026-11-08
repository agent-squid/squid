"""History tools reuse keyword search without exposing other topics or run metadata."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from agent import continuations as c, history_tools as h, stats_db as db, shared_tools
from agent.route_api import router


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_DB_PATH', tmp_path / 'history.db')
    monkeypatch.setattr('agent.memory.topic_memory_squid_config', lambda _: {'code_roots': []})
    db.init_db()
    c.configure(True)
    db.upsert_agent('codex', 'codex', 'openai', None, cwd=str(tmp_path))
    def message(topic, prompt, response):
        user = db.insert_user_message(topic, 'codex', prompt)
        answer = db.insert_assistant_message(topic, 'codex', user)
        db.update_assistant_message(answer, response, None, 'done')
        return user, answer
    user, answer = message('squid', 'release question', 'release answer ' + 'x' * 9000)
    _, other = message('private', 'release secret', 'release private')
    request = db.insert_user_message('squid', 'codex', 'search history')
    parent = db.insert_assistant_message('squid', 'codex', request)
    yield c.mint(parent), user, answer, other
    c.configure(False)


def test_search_fetch_and_scope(history):
    token, user, answer, other = history
    result = h.invoke(token, 'search', {'query': 'release'})
    assert [i['message_id'] for i in result['items']] == [answer]
    assert len(result['items'][0]['excerpt']) <= 800
    assert result['items'][0]['jump_command'] == f'/jump {answer}'
    assert 'context' not in result['items'][0]
    assert h.invoke(token, 'search', {'query': 'question', 'target': 'prompts'})['items'][0]['message_id'] == answer
    page = h.invoke(token, 'fetch', {'message_id': answer})
    assert len(page['content']) == 8000 and page['next_offset'] == 8000
    assert h.invoke(token, 'fetch', {'message_id': answer, 'offset': 8000})['next_offset'] is None
    assert h.invoke(token, 'fetch', {'message_id': user})['content'] == 'release question'
    for identifier in (other, 999999):
        with pytest.raises(ValueError, match='history_not_found'):
            h.invoke(token, 'fetch', {'message_id': identifier})
    with pytest.raises(PermissionError):
        h.invoke('invalid-token', 'search', {'query': 'release'})
    with db._connection() as conn:
        conn.execute('DELETE FROM chat_messages WHERE id=?', (answer,))
    assert h.invoke(token, 'search', {'query': 'release'})['items'] == []
    with pytest.raises(ValueError, match='history_not_found'):
        h.invoke(token, 'fetch', {'message_id': answer})
    c.revoke(token)
    with pytest.raises(PermissionError):
        h.invoke(token, 'search', {'query': 'release'})


@pytest.mark.parametrize('action,body', [
    ('search', {'query': 'x', 'topic': 'private'}), ('search', {'query': ''}),
    ('search', {'query': 'x', 'limit': True}), ('search', {'query': 'x', 'limit': 21}),
    ('search', {'query': 'x', 'target': []}), ('fetch', {'message_id': True}),
    ('fetch', {'message_id': 1, 'offset': -1}), ('fetch', []), ('unknown', {}),
])
def test_strict_input(history, action, body):
    with pytest.raises(ValueError, match='invalid_history_request'):
        h.invoke(history[0], action, body)


def test_http_and_cli_transport(history, monkeypatch, capsys):
    app = FastAPI()
    app.include_router(router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for operation, body in [('history_search', {'query': 'release'}), ('history_fetch', {'message_id': history[2]})]:
                method, path, _ = shared_tools.OPERATIONS[operation]
                result = await client.request(method, path, headers={'Authorization': 'Bearer ' + history[0]}, json=body)
                assert result.status_code == 200
                assert result.json() == h.invoke(history[0], operation.removeprefix('history_'), body)
            assert (await client.post('/agent-history/search', json={'query': 'release'})).status_code == 403
            assert (await client.post('/agent-history/search', headers={'Authorization': 'Bearer ' + history[0]}, content=b'x' * 8193)).status_code == 400
    asyncio.run(run())
    from agent import history_cli
    seen = []
    monkeypatch.setattr(history_cli, 'request', lambda op, body, **kw: seen.append((op, body)) or {'items': []})
    assert history_cli.main(['--query', 'release']) == 0
    assert seen[-1] == ('history_search', {'query': 'release', 'target': 'responses', 'limit': 10, 'scope': 'exact'})
    assert history_cli.main(['--message', str(history[2])]) == 0
    assert seen[-1][0] == 'history_fetch'


@pytest.mark.parametrize('topic', ['squid', 'squid.child_%'])
def test_subtree_boundaries_and_fetch(history, topic):
    token = history[0]
    # Mint a credential anchored at a child to prove parents/siblings stay excluded.
    user = db.insert_user_message(topic, 'codex', 'search')
    parent = db.insert_assistant_message(topic, 'codex', user)
    token = c.mint(parent)
    allowed = []
    denied = []
    for name in (topic, topic + '.child', topic + '.child.deep',
                 topic + 'suffix', 'squid.sibling', 'squid_parent', 'squidXchildXX.child',
                 topic.rsplit('.', 1)[0] if '.' in topic else 'parent'):
        prompt = db.insert_user_message(name, 'codex', 'subtree needle')
        answer = db.insert_assistant_message(name, 'codex', prompt)
        db.update_assistant_message(answer, 'subtree needle', None, 'done')
        (allowed if name == topic or name.startswith(topic + '.') else denied).append((prompt, answer))
    for target in ('responses', 'prompts'):
        exact = h.invoke(token, 'search', dict(query='needle', target=target))
        assert [i['message_id'] for i in exact['items']] == [allowed[0][1]]
        result = h.invoke(token, 'search', dict(query='needle', target=target, scope='subtree'))
        assert {i['message_id'] for i in result['items']} == {a for _, a in allowed}
        assert result['scope'] == 'subtree'
    for prompt, answer in allowed:
        for identifier in (prompt, answer):
            assert h.invoke(token, 'fetch', dict(message_id=identifier, scope='subtree'))['message_id'] == identifier
            if (prompt, answer) != allowed[0]:
                with pytest.raises(ValueError, match='history_not_found'):
                    h.invoke(token, 'fetch', dict(message_id=identifier))
    for prompt, answer in denied:
        for identifier in (prompt, answer):
            with pytest.raises(ValueError, match='history_not_found'):
                h.invoke(token, 'fetch', dict(message_id=identifier, scope='subtree'))
    for scope in ('parent', 'all', None, [], True):
        for action, body in [('search', dict(query='needle')), ('fetch', dict(message_id=allowed[0][1]))]:
            with pytest.raises(ValueError, match='invalid_history_request'):
                h.invoke(token, action, body | dict(scope=scope))


def test_subtree_cli(history, monkeypatch):
    from agent import history_cli
    seen = []
    monkeypatch.setattr(history_cli, 'request', lambda op, body, **kw: seen.append((op, body)) or {'items': []})
    assert history_cli.main(['--query', 'release', '--scope', 'subtree']) == 0
    assert seen[-1][1]['scope'] == 'subtree'
    assert history_cli.main(['--message', str(history[2]), '--scope', 'subtree']) == 0
    assert seen[-1][1]['scope'] == 'subtree'


@pytest.mark.parametrize('operation,body', [
    ('history_search', {'query': 'release\x00'}),
    ('history_fetch', {'message_id': 2**63}),
    ('history_fetch', {'message_id': 10**100}),
])
def test_malformed_inputs_return_structured_errors(history, operation, body, monkeypatch):
    from agent import shared_mcp
    app = FastAPI()
    app.include_router(router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            _, path, _ = shared_tools.OPERATIONS[operation]
            response = await client.post(path, headers={'Authorization': 'Bearer ' + history[0]}, json=body)
            assert response.status_code == 400
            assert response.json() == {'error': 'invalid_history_request'}
    asyncio.run(run())
    with pytest.raises(ValueError, match='invalid_history_request'):
        h.invoke(history[0], operation.removeprefix('history_'), body)
    # Exercise native adapter error handling as well as the HTTP boundary.
    def request(op, arguments):
        return h.invoke(history[0], op.removeprefix('history_'), arguments)
    monkeypatch.setattr(shared_tools, 'request', request)
    assert asyncio.run(shared_mcp.invoke(operation, body)) == {'error': 'shared_client_error'}


def test_unicode_query_fits_http_boundary(history):
    import json
    app = FastAPI()
    app.include_router(router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            query = '\U0001f419' * 512
            payload = json.dumps(dict(query=query, scope='subtree', target='responses', limit=20))
            assert len(payload) > 4096
            response = await client.post('/agent-history/search',
                headers={'Authorization': 'Bearer ' + history[0], 'Content-Type': 'application/json'},
                content=payload)
            assert response.status_code == 200
            assert response.json()['items'] == []
    asyncio.run(run())



def test_prompt_search_rejects_cross_topic_reply_source(history):
    token, _, _, _ = history
    prompt = db.insert_user_message('private', 'codex', 'confidentialneedle')
    answer = db.insert_assistant_message('squid', 'codex', prompt)
    db.update_assistant_message(answer, 'public reply', None, 'done')
    assert h.invoke(token, 'search', {'query': 'confidentialneedle', 'target': 'prompts'})['items'] == []
    with pytest.raises(ValueError, match='history_not_found'):
        h.invoke(token, 'fetch', {'message_id': prompt})


@pytest.mark.parametrize('query,prefix', [('"needle"', 'x ' * 1000), ('needle', 'ß' * 1000)])
def test_excerpt_preserves_original_offsets_and_normalizes_quotes(history, query, prefix):
    prompt = db.insert_user_message('squid', 'codex', 'excerpt source')
    answer = db.insert_assistant_message('squid', 'codex', prompt)
    db.update_assistant_message(answer, prefix + ' needle matched', None, 'done')
    result = h.invoke(history[0], 'search', {'query': query})
    assert result['items'][0]['message_id'] == answer
    assert 'needle matched' in result['items'][0]['excerpt']
