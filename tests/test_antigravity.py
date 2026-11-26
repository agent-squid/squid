"""Antigravity's native CLI protocol, registry integration, and default seeding."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent import harnesses, runners, stats_db


def collect(monkeypatch, events, **kwargs):
    captured = {}

    async def stream(cmd, **options):
        captured.update(cmd=cmd, **options)
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield json.dumps(event)

    monkeypatch.setattr(runners, 'AGY_PATH', '/test/agy')
    monkeypatch.setattr(runners, '_stream_lines', stream)
    monkeypatch.setattr(runners, '_attach_session', lambda *args: None)

    async def run():
        return [chunk async for chunk in runners.run_antigravity('next', **kwargs)]
    return asyncio.run(run()), captured


def result(**fields):
    return {'event': 'result', 'result': {
        'status': 'SUCCESS', 'response': 'answer', 'conversation_id': 'conversation-1',
        'duration_seconds': 1.25,
        'usage': {'input_tokens': 25, 'output_tokens': 4, 'cache_read_tokens': 10},
        **fields,
    }}


def test_fresh_and_resumed_command_and_native_result(monkeypatch):
    history = [{'role': 'user', 'content': 'previous prompt'}]
    chunks, call = collect(monkeypatch, [result()], history=history, model='selected-model',
                           cwd='/repo', backend_env={'CUSTOM': 'value'}, backend_id='custom',
                           backend_args=('--effort', 'high'), response_timeout=600,
                           prompt_preview='preview', topic='topic', agent='agent', msg_id=4)
    cmd = call['cmd']
    assert cmd[:3] == ['/test/agy', '--effort', 'high']
    assert cmd[cmd.index('--output-format') + 1] == 'stream-json'
    assert cmd[cmd.index('--model') + 1] == 'selected-model'
    assert cmd[cmd.index('--print-timeout') + 1] == '600s'
    assert cmd.count('--dangerously-skip-permissions') == 1
    assert '--conversation' not in cmd
    assert 'previous prompt' in cmd[-1] and 'next' in cmd[-1]
    assert call['extra_env'] == {'CUSTOM': 'value'}
    assert call['cwd'] == '/repo' and call['backend'] == 'custom'
    assert call['prompt_preview'] == 'preview' and call['msg_id'] == 4
    assert chunks[0] == 'answer'
    stats = chunks[-1]['_stats']
    assert stats['session_id'] == 'conversation-1'
    assert (stats['input_tokens'], stats['output_tokens'], stats['cache_read_tokens']) == (25, 4, 10)
    assert stats['cost_usd'] is None and stats['duration_ms'] == 1250
    _, call = collect(monkeypatch, [result()], history=history, resume_session_id='conversation-1')
    assert call['cmd'][call['cmd'].index('--conversation') + 1] == 'conversation-1'
    assert call['cmd'][-2:] == ['-p', 'next']


def test_streamed_text_tool_dedup_and_session_attachment(monkeypatch):
    attachments = []
    async def stream(cmd, **kwargs):
        events = [
            {'event': 'init', 'conversation_id': 'conversation-1'},
            {'event': 'step_update', 'step_update': {
                'step_type': 'agent_response', 'state': 'ACTIVE', 'text_delta': 'an'}},
            {'event': 'step_update', 'step_update': {
                'step_type': 'agent_response', 'state': 'DONE', 'text_delta': 'swer'}},
        ]
        tool = {'event': 'step_update', 'step_update': {
            'step_type': 'tool', 'state': 'DONE', 'step_index': 3,
            'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'pwd'}}}}
        for event in [*events, tool, tool, result()]:
            yield json.dumps(event)
    monkeypatch.setattr(runners, 'AGY_PATH', '/test/agy')
    monkeypatch.setattr(runners, '_stream_lines', stream)
    monkeypatch.setattr(runners, '_attach_session', lambda *args: attachments.append(args))
    async def run():
        return [c async for c in runners.run_antigravity('prompt', msg_id=7)]
    chunks = asyncio.run(run())
    assert ''.join(c for c in chunks if isinstance(c, str)) == 'answer'
    assert [c for c in chunks if isinstance(c, dict) and '_tool' in c] == [
        {'_tool': {'name': 'Bash', 'command': 'pwd'}}]
    assert attachments[0] == (7, 'conversation-1')
    assert len([c for c in chunks if isinstance(c, dict) and '_stats' in c]) == 1


@pytest.mark.parametrize('status', ['ERROR', 'CANCELED', 'INTERRUPTED', 'INVALID', 'WAITING', 'RUNNING'])
def test_unsuccessful_results_raise(monkeypatch, status):
    with pytest.raises(runners.CLIError, match=status):
        collect(monkeypatch, [result(status=status)])


def test_missing_result_and_process_error_are_not_success(monkeypatch):
    with pytest.raises(runners.CLIError, match='without a result'):
        collect(monkeypatch, [{'event': 'init', 'conversation_id': 'c'}])
    with pytest.raises(runners.CLIError, match='CLI exited'):
        collect(monkeypatch, [result(), runners.CLIError('CLI exited 1')])


def test_configured_permission_flag_is_not_duplicated(monkeypatch):
    _, call = collect(monkeypatch, [result()], backend_args=('--dangerously-skip-permissions',))
    assert call['cmd'].count('--dangerously-skip-permissions') == 1


def test_tool_error_without_reply_names_the_failed_tool(monkeypatch):
    denied = {'event': 'step_update', 'step_update': {
        'step_type': 'tool', 'state': 'ERROR', 'step_index': 2, 'tool_name': 'run_command',
        'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'echo hi'},
                      'error': {'type': 'TOOL_ERROR', 'message': (
                          'permission check failed for unsandboxed "echo hi": user denied\n'
                          'Do not attempt to circumvent this denial')}}}}
    with pytest.raises(runners.CLIError, match=r'tool error \(run_command: permission check failed[^\n]*\)$'):
        collect(monkeypatch, [denied, result(response='')])
    # A tool error the agent recovered from still returns its reply.
    chunks, _ = collect(monkeypatch, [denied, result()])
    assert chunks[0] == 'answer'


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(runners, 'AGY_PATH', None)
    async def run():
        return [c async for c in runners.run_antigravity('prompt')]
    with pytest.raises(runners.CLINotFoundError, match='antigravity.google/cli/install.sh'):
        asyncio.run(run())


def test_harness_registration_and_protocol_restrictions(monkeypatch):
    monkeypatch.setitem(harnesses._HARNESS_PATHS, 'antigravity', '/test/agy')
    assert harnesses.is_installed('antigravity')
    assert harnesses.protocol_for('antigravity', None) == 'oneshot-cli'
    with pytest.raises(ValueError, match='does not support'):
        harnesses.protocol_for('antigravity', 'interactive-cli')
    resolved = SimpleNamespace(harness='antigravity', protocol='oneshot-cli')
    assert runners.runner_for_harness('antigravity') is runners.run_antigravity
    assert runners.runner_for_agent(resolved) is runners.run_antigravity
    assert runners.runner_for_agent(resolved, adhoc=True) is runners.run_antigravity
    monkeypatch.setitem(harnesses._HARNESS_PATHS, 'antigravity', None)
    assert not harnesses.is_installed('antigravity')


def test_installed_harness_seed_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'squid.db')
    monkeypatch.setattr(stats_db, 'SUPPORTED_HARNESSES', frozenset({'antigravity'}))
    monkeypatch.setattr(stats_db, 'is_installed', lambda harness: True)
    monkeypatch.setattr(stats_db, 'default_provider_for', lambda harness: 'google')
    monkeypatch.setattr('agent.providers.get_provider', lambda provider: SimpleNamespace(supported_apis={'/native/antigravity'}) if provider == 'google' else None)
    stats_db.init_db()
    with sqlite3.connect(tmp_path / 'squid.db') as conn:
        assert conn.execute("SELECT name,harness,provider,model FROM agents WHERE name='agy'").fetchall() == [
            ('agy', 'antigravity', 'google', None)]
        conn.execute("UPDATE agents SET model='chosen-model' WHERE name='agy'")
    stats_db.init_db()
    with sqlite3.connect(tmp_path / 'squid.db') as conn:
        assert conn.execute("SELECT model FROM agents WHERE name='agy'").fetchall() == [('chosen-model',)]


@pytest.mark.parametrize('setup', ['fresh', 'legacy', 'custom'])
def test_native_provider_respects_explicit_catalog(tmp_path, setup):
    if setup != 'fresh':
        import yaml
        config = {
            'server': {'host': '127.0.0.1', 'port': 8000},
            'agent': {'first_byte_timeout': 300, 'response_timeout': 1800},
            'providers': {'anthropic': {
                'label': 'Existing Claude', 'color': '#AE5332',
                'auth': {'type': 'subscription'}, 'gauge': 'claude',
                'supported_apis': ['/v1/messages'],
            }},
        }
        if setup == 'custom':
            config['harnesses'] = {'antigravity': {'default_provider': 'google', 'label': 'Custom Harness'}}
            config['providers']['google'] = {
                'label': 'Custom AGY', 'color': '#123456',
                'auth': {'type': 'subscription'}, 'gauge': {'type': 'static', 'text': 'Custom'},
                'supported_apis': ['/native/antigravity'],
            }
        directory = tmp_path / '.squid'
        directory.mkdir()
        (directory / 'squid.yaml').write_text(yaml.safe_dump(config))
    code = '''
import json
from agent.harnesses import list_harnesses
from agent.providers import public_providers
from agent.resolve import resolve_agent
providers = public_providers()
resolved = resolve_agent('antigravity').public_dict() if {'google', 'antigravity'} & providers.keys() else None
print(json.dumps({'harnesses': list_harnesses(), 'providers': providers, 'resolved': resolved}))
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('SQUID_')}
    env.update(HOME=str(tmp_path), USER='agy-harness-test', LOGNAME='agy-harness-test')
    proc = subprocess.run([sys.executable, '-c', code], env=env, cwd=Path.cwd(),
                          capture_output=True, text=True, check=True, timeout=20)
    data = json.loads(proc.stdout)
    harness = next(h for h in data['harnesses'] if h['id'] == 'antigravity')
    if setup == 'legacy':
        assert set(data['providers']) == {'anthropic'}
        assert harness['compatible_providers'] == []
        return
    provider_id = 'google'
    provider = data['providers'][provider_id]
    assert harness['default_provider'] == provider_id
    assert harness['compatible_providers'] == [provider_id]
    assert harness['supported_apis'] == ['/native/antigravity']
    assert provider['gauge']['type'] == ('static' if setup == 'custom' else 'antigravity')
    assert data['resolved']['protocol'] == 'oneshot-cli'
    if setup != 'fresh':
        assert set(data['providers']) == {'anthropic', provider_id}
        assert data['providers']['anthropic']['label'] == 'Existing Claude'
    if setup != 'custom':
        assert provider['label'] == 'Google'
        assert harness['label'] == 'Antigravity'
    if setup == 'custom':
        assert harness['label'] == 'Custom Harness'
        assert provider['label'] == 'Custom AGY'
        assert provider['color'] == '#123456'
        assert provider['gauge']['text'] == 'Custom'


def test_login_uses_native_cli(monkeypatch):
    from agent import auth_sessions
    monkeypatch.setattr(auth_sessions, 'AGY_PATH', '/test/agy')
    assert auth_sessions._login_argv('antigravity') == ['/test/agy', 'acp', '--login']

    monkeypatch.setattr(auth_sessions, 'AGY_PATH', None)
    with pytest.raises(auth_sessions.AuthSessionError, match='agy CLI not found'):
        auth_sessions._login_argv('antigravity')


def test_real_subprocess_uses_shared_cancellation_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, 'AGY_PATH', sys.executable)
    script = 'import json,time; print(json.dumps({"event":"init","conversation_id":"c"}),flush=True); time.sleep(30)'
    topic = 'antigravity-cancellation-test'

    async def run():
        async def collect():
            return [c async for c in runners.run_antigravity(
                'prompt', cwd=str(tmp_path), backend_args=('-c', script), topic=topic)]
        task = asyncio.create_task(collect())
        try:
            async def registered():
                while not any(info['topic'] == topic for info in runners._proc_registry.values()):
                    await asyncio.sleep(.01)
            await asyncio.wait_for(registered(), 5)
            assert runners.kill_procs_by_topic(topic) == 1
            with pytest.raises(runners.CLIError):
                await asyncio.wait_for(task, 5)
            assert not any(info['topic'] == topic for info in runners._proc_registry.values())
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_short_default_keeps_existing_antigravity_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'squid.db')
    monkeypatch.setattr(stats_db, 'SUPPORTED_HARNESSES', frozenset({'antigravity'}))
    monkeypatch.setattr(stats_db, 'is_installed', lambda harness: harness == 'antigravity')
    stats_db.init_db()
    stats_db.upsert_agent('antigravity', 'antigravity', 'antigravity', 'chosen-model', '/repo')
    stats_db.init_db()
    assert stats_db.get_agent('antigravity')['model'] == 'chosen-model'
    assert stats_db.get_agent('antigravity')['cwd'] == '/repo'
    assert stats_db.get_agent('agy')['harness'] == 'antigravity'


def test_legacy_native_gauge_upgrades_but_custom_gauge_survives():
    from agent.providers import _validate_provider
    raw = {
        'label': 'agy', 'color': '#4285F4',
        'auth': {'type': 'subscription'},
        'supported_apis': ['/native/antigravity'],
        'gauge': {'type': 'static', 'text': 'Native', 'title': 'Managed by Antigravity CLI'},
    }
    assert _validate_provider('antigravity', raw).gauge.type == 'antigravity'
    raw['gauge'] = {'type': 'static', 'text': 'Custom'}
    assert _validate_provider('antigravity', raw).gauge.type == 'static'


def test_capture_raw_events_resume_and_failed_turn(tmp_path, monkeypatch):
    from agent.agy_logs import session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    events = [
        {'event': 'init', 'conversation_id': 'conversation-1'},
        {'event': 'step_update', 'step_update': {'step_type': 'tool', 'state': 'DONE',
         'step_index': 1, 'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'pwd'},
                                     'output': '/workspace'}}},
        result(),
    ]
    collect(monkeypatch, events, agent='agy')
    path = session_log_path('agy', 'conversation-1')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]['message']['content'] == 'next'
    assert [json.loads(row['raw']) for row in rows[1:]] == events
    assert all(row['timestamp'] for row in rows)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(runners.CLIError):
        collect(monkeypatch, [result(status='ERROR')], agent='agy', resume_session_id='conversation-1')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-2]['event'] == 'squid_prompt'
    assert json.loads(rows[-1]['raw'])['result']['status'] == 'ERROR'
    assert session_log_path('other-agent', 'conversation-1') != path


def test_capture_retains_non_json_and_rejects_unsafe_ids(tmp_path, monkeypatch):
    from agent.agy_logs import Capture, session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    capture = Capture('agy', 'session-1', 'full prompt', 7)
    capture.record('not json')
    rows = [json.loads(line) for line in session_log_path('agy', 'session-1').read_text().splitlines()]
    assert rows[-1]['raw'] == 'not json'
    with pytest.raises(ValueError):
        session_log_path('agy', '../escape')


def test_session_log_finds_only_captured_agy_logs(tmp_path, monkeypatch):
    from agent import server
    from agent.agy_logs import Capture, session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    assert server._find_session_log('antigravity', 'older-session', '', 'agy') is None
    Capture('agy', 'session-1', 'prompt', 1)
    assert server._find_session_log('antigravity', 'session-1', '', 'agy') == session_log_path('agy', 'session-1')
    assert server._find_session_log('antigravity', '../escape', '', 'agy') is None


def test_capture_never_splits_a_turn_between_conversations(tmp_path, monkeypatch):
    from agent.agy_logs import Capture, session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    capture = Capture('agy', 'original', 'prompt', 7)
    capture.record(json.dumps({'event': 'init', 'conversation_id': 'different'}))
    capture.record(json.dumps(result()))
    assert capture.disabled
    assert not session_log_path('agy', 'different').exists()
    assert not session_log_path('agy', 'conversation-1').exists()
    rows = session_log_path('agy', 'original').read_text().splitlines()
    assert len(rows) == 1


def test_failed_result_attaches_log_without_init(tmp_path, monkeypatch):
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    attachments = []
    async def stream(*args, **kwargs):
        yield json.dumps(result(status='ERROR', error='failed'))
    monkeypatch.setattr(runners, 'AGY_PATH', '/test/agy')
    monkeypatch.setattr(runners, '_stream_lines', stream)
    monkeypatch.setattr(runners, '_attach_session', lambda *args: attachments.append(args))
    async def run():
        return [c async for c in runners.run_antigravity('prompt', agent='agy', msg_id=7)]
    with pytest.raises(runners.CLIError):
        asyncio.run(run())
    assert attachments == [(7, 'conversation-1')]


def test_capture_write_failure_does_not_fail_run(tmp_path, monkeypatch):
    root = tmp_path / 'not-a-directory'
    root.write_text('blocked')
    monkeypatch.setenv('SQUID_LOG_DIR', str(root))
    chunks, _ = collect(monkeypatch, [result()], agent='agy')
    assert chunks[0] == 'answer'


def test_agy_session_log_endpoint_and_file_read(tmp_path, monkeypatch):
    from tests.browser_client import PairedBrowserClient
    from agent import server
    from agent.agy_logs import Capture
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    stats_db.upsert_agent('agy-log-review', 'antigravity', 'antigravity', None, None)
    capture = Capture('agy-log-review', 'session-review', 'submitted prompt', 1)
    capture.record(json.dumps({'event': 'init', 'conversation_id': 'session-review'}))
    client = PairedBrowserClient(server.app)
    response = client.get('/session-log', params={'agent': 'agy-log-review', 'session_id': 'session-review'})
    assert response.status_code == 200
    body = response.json()
    assert body['source'] == 'agy-ndjson'
    transcript = client.get('/localfile', params={'path': body['path']})
    assert transcript.status_code == 200
    assert json.loads(transcript.text.splitlines()[0])['message']['content'] == 'submitted prompt'


def test_truncated_subprocess_capture_is_linked_from_step(tmp_path, monkeypatch):
    from agent.agy_logs import session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setattr(runners, 'AGY_PATH', sys.executable)
    attachments = []
    monkeypatch.setattr(runners, '_attach_session', lambda *args: attachments.append(args))
    event = {'event': 'step_update', 'step_update': {
        'conversation_id': 'truncated-session', 'step_type': 'agent_response',
        'state': 'ACTIVE', 'text_delta': 'partial answer'}}
    # Emit native NDJSON, then exit without init or result. This exercises
    # the real stdout reader, not the mocked _stream_lines helper.
    script = 'import sys; print(' + repr(json.dumps(event)) + ', flush=True); sys.exit(3)'
    chunks = []
    async def run():
        async for chunk in runners.run_antigravity(
                'full prompt', agent='agy', msg_id=99, cwd=str(tmp_path),
                backend_args=('-c', script)):
            chunks.append(chunk)
    with pytest.raises(runners.CLIError, match='CLI exited 3'):
        asyncio.run(run())
    assert chunks == ['partial answer']
    assert attachments == [(99, 'truncated-session')]
    rows = [json.loads(line) for line in session_log_path('agy', 'truncated-session').read_text().splitlines()]
    assert rows[0]['message']['content'] == 'full prompt'
    assert json.loads(rows[1]['raw']) == event
    assert not any(info['msg_id'] == 99 for info in runners._proc_registry.values())


def test_session_attachment_occurs_once_for_many_stream_steps(tmp_path, monkeypatch):
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    attachments = []
    async def stream(*args, **kwargs):
        yield json.dumps({'event': 'init', 'conversation_id': 'conversation-1'})
        for index in range(50):
            yield json.dumps({'event': 'step_update', 'step_update': {
                'conversation_id': 'conversation-1', 'step_type': 'agent_response',
                'state': 'ACTIVE', 'step_index': index, 'text_delta': 'x'}})
        yield json.dumps(result())
    monkeypatch.setattr(runners, 'AGY_PATH', '/test/agy')
    monkeypatch.setattr(runners, '_stream_lines', stream)
    monkeypatch.setattr(runners, '_attach_session', lambda *args: attachments.append(args))
    async def run():
        return [c async for c in runners.run_antigravity('prompt', agent='agy', msg_id=7)]
    asyncio.run(run())
    assert attachments == [(7, 'conversation-1')]


def test_resumed_failure_before_stdout_still_links_capture(tmp_path, monkeypatch):
    from agent.agy_logs import session_log_path
    monkeypatch.setenv('SQUID_LOG_DIR', str(tmp_path))
    attachments = []
    async def stream(*args, **kwargs):
        raise runners.CLIError('failed before stdout')
        yield  # async generator
    monkeypatch.setattr(runners, 'AGY_PATH', '/test/agy')
    monkeypatch.setattr(runners, '_stream_lines', stream)
    monkeypatch.setattr(runners, '_attach_session', lambda *args: attachments.append(args))
    async def run():
        return [c async for c in runners.run_antigravity(
            'prompt', agent='agy', msg_id=7, resume_session_id='conversation-1')]
    with pytest.raises(runners.CLIError, match='before stdout'):
        asyncio.run(run())
    assert attachments == [(7, 'conversation-1')]
    rows = session_log_path('agy', 'conversation-1').read_text().splitlines()
    assert json.loads(rows[0])['message']['content'] == 'prompt'
