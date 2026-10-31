import asyncio
import hashlib
import os
import sys

import pytest

from agent.watch_auth import RESTART_DIGEST, load_digest, restart_environment


def test_restart_preserves_only_verifier_and_child_env_removes_it(monkeypatch):
    from agent.runners import _child_env
    secret = 'operator-secret-' * 3
    environment = {'SQUID_WATCH_CONTROL_TOKEN': secret, 'PATH': '/bin'}
    digest = load_digest(environment)
    assert digest == hashlib.sha256(secret.encode()).digest()
    assert 'SQUID_WATCH_CONTROL_TOKEN' not in environment
    for _ in range(2):
        environment = restart_environment(environment, digest)
        assert secret not in str(environment)
        digest = load_digest(environment)
        assert digest == hashlib.sha256(secret.encode()).digest()
        assert RESTART_DIGEST not in environment
    monkeypatch.setenv(RESTART_DIGEST, digest.hex())
    assert RESTART_DIGEST not in _child_env()


@pytest.mark.parametrize('value', ['bad', '00', 'x' * 64, '00' * 33])
def test_invalid_restart_verifier_fails_closed(value):
    with pytest.raises(ValueError, match='restart verifier'):
        load_digest({RESTART_DIGEST: value})


@pytest.mark.asyncio
async def test_builtin_restart_passes_control_verifier_to_exec(monkeypatch):
    from agent import server
    digest = hashlib.sha256(b'operator-secret').digest()
    monkeypatch.setattr(server.app.state, 'watch_control_digest', digest, raising=False)
    monkeypatch.setattr(server, 'kill_all_procs', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['agentsquid'])
    original_listdir = os.listdir
    monkeypatch.setattr(os, 'listdir', lambda path: [] if path == '/dev/fd' else original_listdir(path))
    captured = {}
    complete = asyncio.Event()
    def execve(executable, argv, environment):
        captured.update(environment)
        complete.set()
    monkeypatch.setattr(os, 'execve', execve)
    result = await server.run_cmd(server.CmdRequest(command='restart', topic='default', upgrade=False))
    assert result.status_code == 200
    await asyncio.wait_for(complete.wait(), 2)
    assert 'SQUID_WATCH_CONTROL_TOKEN' not in captured
    assert load_digest(captured) == digest
