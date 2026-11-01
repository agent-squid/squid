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


def test_file_credential_survives_cold_start_and_reexec():
    from agent.watch_auth import control_token_file, file_token
    digest = load_digest({}, create=True)
    path = control_token_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(file_token()) == 64
    assert load_digest({}) == digest
    assert load_digest(restart_environment({}, digest), create=True) == digest


def test_disabled_start_does_not_create_credential():
    from agent.watch_auth import control_token_file
    assert load_digest({}) is None
    assert not control_token_file().exists()


def test_unsafe_file_permissions_and_symlinks_fail_closed(tmp_path):
    from agent.watch_auth import control_token_file, file_token
    path = control_token_file()
    path.write_text('a' * 64)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        file_token()
    path.unlink()
    target = tmp_path / 'other-token'
    target.write_text('b' * 64)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(OSError):
        file_token()


@pytest.mark.parametrize('existing_file', [False, True])
def test_enabled_restart_preserves_environment_override(existing_file):
    from agent.watch_auth import control_token_file, file_token
    path = control_token_file()
    if existing_file:
        file_token(create=True)
    original = path.read_bytes() if existing_file else None
    secret = 'explicit-operator-secret-' * 2
    digest = load_digest({'SQUID_WATCH_CONTROL_TOKEN': secret}, create=True)
    for _ in range(2):
        environment = restart_environment({}, digest)
        assert load_digest(environment, create=True) == digest
        assert RESTART_DIGEST not in environment
        assert (path.read_bytes() if path.exists() else None) == original


def test_invalid_restart_verifier_cannot_fall_back_to_file():
    from agent.watch_auth import file_token
    file_token(create=True)
    with pytest.raises(ValueError, match='restart verifier'):
        load_digest({RESTART_DIGEST: 'invalid'}, create=True)
