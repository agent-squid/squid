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


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(runners, 'AGY_PATH', None)
    async def run():
        return [c async for c in runners.run_antigravity('prompt')]
    with pytest.raises(runners.CLINotFoundError, match='antigravity.google/cli/install.sh'):
        asyncio.run(run())


def test_harness_registration_and_protocol_restrictions(monkeypatch):
    monkeypatch.setitem(harnesses._HARNESS_PATHS, 'antigravity', '/test/agy')
    assert harnesses.is_installed('antigravity')
    assert harnesses.default_provider_for('antigravity') == 'antigravity'
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
    stats_db.init_db()
    with sqlite3.connect(tmp_path / 'squid.db') as conn:
        assert conn.execute("SELECT name,harness,provider,model FROM agents WHERE name='agy'").fetchall() == [
            ('agy', 'antigravity', 'antigravity', None)]
        conn.execute("UPDATE agents SET model='chosen-model' WHERE name='agy'")
    stats_db.init_db()
    with sqlite3.connect(tmp_path / 'squid.db') as conn:
        assert conn.execute("SELECT model FROM agents WHERE name='agy'").fetchall() == [('chosen-model',)]


@pytest.mark.parametrize('setup', ['fresh', 'legacy', 'custom'])
def test_fresh_and_existing_configs_expose_native_provider_and_harness(tmp_path, setup):
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
            config['providers']['antigravity'] = {
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
print(json.dumps({'harnesses': list_harnesses(), 'providers': public_providers(),
                  'resolved': resolve_agent('antigravity').public_dict()}))
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('SQUID_')}
    env.update(HOME=str(tmp_path), USER='agy-harness-test', LOGNAME='agy-harness-test')
    proc = subprocess.run([sys.executable, '-c', code], env=env, cwd=Path.cwd(),
                          capture_output=True, text=True, check=True, timeout=20)
    data = json.loads(proc.stdout)
    harness = next(h for h in data['harnesses'] if h['id'] == 'antigravity')
    provider = data['providers']['antigravity']
    assert harness['compatible_providers'] == ['antigravity']
    assert harness['supported_apis'] == ['/native/antigravity']
    assert provider['gauge']['type'] == 'static'
    assert data['resolved']['protocol'] == 'oneshot-cli'
    if setup != 'fresh':
        assert set(data['providers']) == {'anthropic', 'antigravity'}
        assert data['providers']['anthropic']['label'] == 'Existing Claude'
    if setup != 'custom':
        assert provider['label'] == 'agy'
        assert harness['label'] == 'agy'
    if setup == 'custom':
        assert provider['label'] == 'Custom AGY'
        assert provider['color'] == '#123456'
        assert provider['gauge']['text'] == 'Custom'


def test_login_guidance_uses_native_cli():
    from agent.auth_sessions import NoLoginCommand, _login_argv
    with pytest.raises(NoLoginCommand, match='run agy in a terminal'):
        _login_argv('antigravity')


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
