import asyncio
import sys

import pytest

from agent.watch_config import load_watch_configuration


def test_defaults_and_disabled_registry_are_unavailable():
    assert not load_watch_configuration({}).enabled
    assert load_watch_configuration({}).adapters == {}
    loaded = load_watch_configuration({'watches': {'registry': {
        'probes': {'local': {'argv': ['/does/not/exist']}}}}})
    assert not loaded.enabled
    assert loaded.adapters == {}


@pytest.mark.parametrize('section', [
    None, [], {'enabled': 'false'}, {'enabled': 1}, {'publish': True},
    {'registry': None}, {'registry': {'unexpected': {}}},
    {'registry': {'probes': {'bad': {'argv': 'shell text'}}}},
    {'registry': {'probes': {'bad': {'server': 'missing', 'tool': 'read'}}}},
])
def test_invalid_configuration_fails_even_when_disabled(section):
    with pytest.raises(ValueError):
        load_watch_configuration({'watches': section})


def test_enabled_registry_is_lazy_and_uses_existing_probe_contract():
    config = {'watches': {'enabled': True, 'registry': {'probes': {
        'local': {'argv': [sys.executable, '-c',
                           'print(\'{"revision":"v1"}\')']},
        'missing': {'argv': ['/does/not/exist']},
    }}}}
    loaded = load_watch_configuration(config)
    assert loaded.enabled
    config['watches']['registry']['probes']['local']['argv'].clear()
    result = asyncio.run(loaded.adapters['local'].poll({'target': 'task', 'cursor': None}))
    assert result.cursor == 'v1'
    assert not result.events


def test_invalid_host_configuration_prevents_startup_before_tasks(monkeypatch):
    from agent import server

    monkeypatch.setattr(server, '_cfg', {'watches': {'enabled': 'yes'}})
    started = []
    monkeypatch.setattr(server._realtime_notifier, 'start', lambda loop: started.append(True))

    async def start():
        async with server._lifespan(server.app):
            pytest.fail('invalid watch configuration allowed startup')

    with pytest.raises(ValueError, match='must be a boolean'):
        asyncio.run(start())
    assert started == []


def test_config_editor_rejects_invalid_registry():
    from agent.server import _validate_config_content

    content = '''
server: {host: 127.0.0.1, port: 8000}
agent: {first_byte_timeout: 600, response_timeout: 3600}
watches: {enabled: false, registry: {unknown: {}}}
'''
    with pytest.raises(ValueError, match='configuration fields'):
        _validate_config_content(content)


def test_topic_policy_cannot_expand_global_scope():
    config = load_watch_configuration({'watches': {'enabled': True,
        'registry': {'probes': {'local': {'argv': ['/missing']}}},
        'policy': {'operations': ['list', 'show'], 'min_poll_seconds': 60,
                   'max_lifetime_seconds': 3600},
        'topics': {'squid': {'operations': ['list', 'create'], 'min_poll_seconds': 30,
                             'max_lifetime_seconds': 86400},
                   'disabled': {'enabled': False}}}})
    policy = config.effective_policy('squid')
    assert policy.operations == {'list'}
    assert policy.min_poll_seconds == 60
    assert policy.max_lifetime_seconds == 3600
    assert not config.effective_policy('disabled').enabled
    assert config.effective_policy('other').operations == {'list', 'show'}


@pytest.mark.parametrize('extra', [
    {'policy': {'publish': True}}, {'policy': {'enabled': 1}},
    {'policy': {'operations': ['publish']}}, {'policy': {'operations': 'list'}},
    {'policy': {'adapters': ['missing']}}, {'policy': {'min_poll_seconds': 29}},
    {'policy': {'max_lifetime_seconds': True}}, {'topics': []},
    {'topics': {'squid': {'unknown': 1}}},
])
def test_invalid_policy_rejected(extra):
    with pytest.raises(ValueError):
        load_watch_configuration({'watches': extra})
