import json

import httpx
import pytest

from agent import watch_cli


@pytest.fixture
def transport(monkeypatch):
    requests = []
    original_client = httpx.Client
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'status': 'proposed'})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setenv('SQUID_API_URL', 'http://127.0.0.1:1111')
    monkeypatch.setenv('SQUID_TURN_TOKEN', 'turn-secret')
    monkeypatch.setenv('SQUID_WATCH_CONTROL_TOKEN', 'human-secret')
    return requests


def test_instance_routing_and_separate_human_auth(transport, monkeypatch, capsys, tmp_path):
    assert watch_cli.main(['watch', 'list']) == 0
    monkeypatch.setenv('SQUID_API_URL', 'http://127.0.0.1:2222')
    assert watch_cli.main(['watch', 'list']) == 0
    path = tmp_path / 'grant.json'
    path.write_text(json.dumps({'expires_at': 2000}))
    assert watch_cli.main(['watch', 'grant', 'id', '--human-topic', 'squid', '--input', str(path)]) == 0
    assert [r.url.port for r in transport] == [1111, 2222, 2222]
    assert transport[0].headers['authorization'] == 'Bearer turn-secret'
    assert transport[2].headers['authorization'] == 'Bearer human-secret'
    assert transport[2].url.path == '/watch-control/squid/watches/id/grant'
    assert 'secret' not in str(capsys.readouterr())


@pytest.mark.parametrize('url', ['', 'http://remote.example', 'https://user:secret@example.com',
                               'http://localhost?token=secret', 'file:///secret'])
def test_invalid_url_has_no_request_or_secret_output(transport, monkeypatch, capsys, url):
    monkeypatch.setenv('SQUID_API_URL', url)
    assert watch_cli.main(['watch', 'list']) == 1
    assert transport == []
    assert capsys.readouterr().err == '{"error":"watch_client_error"}\n'


def test_no_config_fallback_or_agent_grants(transport, monkeypatch, capsys):
    monkeypatch.delenv('SQUID_TURN_TOKEN')
    assert watch_cli.main(['watch', 'list']) == 1
    monkeypatch.setenv('SQUID_TURN_TOKEN', 'turn-secret')
    assert watch_cli.main(['watch', 'grant', 'id', '--input', '-']) == 1
    assert transport == []
    assert 'secret' not in str(capsys.readouterr())


def test_http_errors_do_not_echo_remote_body_or_tokens(transport, monkeypatch, capsys):
    original_client = httpx.Client
    # Replace the fixture factory with an actual mock client returning hostile diagnostics.
    with original_client() as base:
        actual_type = type(base)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: actual_type(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, text='turn-secret human-secret')), **kw))
    assert watch_cli.main(['watch', 'list']) == 1
    assert json.loads(capsys.readouterr().err) == {'error': 'watch_request_failed', 'status': 403}


@pytest.mark.parametrize('url', ['http://127.0.0.2:1111', 'http://127.1.2.3:1111',
                               'http://[::1]:1111', 'http://localhost:1111',
                               'https://remote.example:1111'])
def test_supported_urls_are_shared_by_launch_and_cli(transport, monkeypatch, url):
    from agent import watch_launch
    monkeypatch.setenv('SQUID_API_URL', url)
    try:
        watch_launch.configure(None, url)
        assert watch_launch._api_url == url
        assert watch_cli.main(['tools']) == 0
        assert len(transport) == 1
    finally:
        watch_launch.configure(None)


@pytest.mark.parametrize('url', ['http://192.0.2.1', 'http://remote.example',
                               'http://127.0.0.1:bad', 'http://127.0.0.1:65536',
                               'http://@127.0.0.1', 'https://host/path', 'file:///tmp'])
def test_unsupported_urls_are_rejected_by_both_paths(transport, monkeypatch, url):
    from agent import watch_launch
    monkeypatch.setenv('SQUID_API_URL', url)
    try:
        watch_launch.configure(None, url)
        assert watch_launch._api_url is None
        assert watch_cli.main(['tools']) == 1
        assert transport == []
    finally:
        watch_launch.configure(None)
