import os
import subprocess
import sys

import httpx
import pytest

from agent import config, server
from agent.features import shore_enabled


def test_shore_requires_explicit_opt_in(monkeypatch, capsys):
    monkeypatch.setitem(config._cfg, "shore", {})
    assert not shore_enabled()
    server._print_lifecycle_usage()
    assert "pair" not in capsys.readouterr().out
    monkeypatch.setitem(config._cfg, "shore", {"enabled": True})
    assert shore_enabled()
    server._print_lifecycle_usage()
    assert "pair" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_disabled_shore_blocks_all_routes_even_with_existing_connection(monkeypatch):
    monkeypatch.setitem(config._cfg, "shore", {})
    monkeypatch.setattr(server, "_shore_connection", object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://localhost") as client:
        for method, path in [("GET", "/shore/devices"), ("POST", "/shore/pairing/begin"),
                             ("POST", "/shore/devices/revoke"), ("GET", "/shore/pairing/requests")]:
            response = await client.request(method, path)
            assert response.status_code == 404


@pytest.mark.parametrize("enabled", [False, True])
def test_shore_schema_visibility_matches_startup_flag(enabled):
    # Route schema visibility is set at startup, like the daemon connection.
    env = dict(os.environ)
    env.pop("SQUID_SHORE_ENABLED", None)
    result = subprocess.run(
        [sys.executable, "-c", """
from fastapi.testclient import TestClient
from agent import config
config._cfg["shore"] = {"enabled": %r}
from agent.server import app
client = TestClient(app)
assert client.get('/docs').status_code == 200
schema = client.get('/openapi.json').json()
paths = [path for path in schema['paths'] if path.startswith('/shore/')]
assert bool(paths) == %r
assert ('ShoreApprovePairingRequest' in schema.get('components', {}).get('schemas', {})) == %r
""" % (enabled, enabled, enabled)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("settings, expected", [({}, False), ({"shore": {}}, False),
    ({"shore": {"enabled": False}}, False), ({"shore": {"enabled": True}}, True)])
def test_shore_yaml_flag(settings, expected):
    assert config.shore_enabled(settings) is expected


@pytest.mark.parametrize("value", ["true", "false", 1, None])
def test_shore_yaml_flag_rejects_non_boolean(value):
    with pytest.raises(ValueError, match="shore.enabled must be a boolean"):
        config.shore_enabled({"shore": {"enabled": value}})


def test_shore_environment_no_longer_enables_feature(monkeypatch):
    monkeypatch.setitem(config._cfg, "shore", {})
    monkeypatch.setenv("SQUID_SHORE_ENABLED", "1")
    assert not shore_enabled()
