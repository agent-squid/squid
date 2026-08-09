"""Tests for agent/sandbox_home.py -- Blank Home credential reconciliation
(ADR-0036), focused on the claudecode/macOS-Keychain special case."""
from pathlib import Path

import pytest

from agent import sandbox_home


def test_read_macos_keychain_credential_returns_none_off_macos(monkeypatch):
    monkeypatch.setattr(sandbox_home.sys, "platform", "linux")
    assert sandbox_home._read_macos_keychain_credential("svc") is None


def test_read_macos_keychain_credential_returns_none_on_subprocess_failure(monkeypatch):
    monkeypatch.setattr(sandbox_home.sys, "platform", "darwin")

    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(sandbox_home.subprocess, "run", lambda *a, **k: FakeResult())
    assert sandbox_home._read_macos_keychain_credential("svc") is None


def test_read_macos_keychain_credential_returns_stripped_stdout_on_success(monkeypatch):
    monkeypatch.setattr(sandbox_home.sys, "platform", "darwin")

    class FakeResult:
        returncode = 0
        stdout = '{"claudeAiOauth":{"accessToken":"tok"}}\n'

    monkeypatch.setattr(sandbox_home.subprocess, "run", lambda *a, **k: FakeResult())
    assert sandbox_home._read_macos_keychain_credential("svc") == '{"claudeAiOauth":{"accessToken":"tok"}}'


def test_reconcile_claude_credential_writes_plain_file_from_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sandbox_home, "_read_macos_keychain_credential", lambda service: "fresh-keychain-token"
    )
    sandbox_home._reconcile_claude_credential(tmp_path)

    p = tmp_path / ".claude" / ".credentials.json"
    assert p.exists()
    assert not p.is_symlink()
    assert p.read_text() == "fresh-keychain-token"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_reconcile_claude_credential_overwrites_stale_symlink_with_fresh_copy(tmp_path, monkeypatch):
    real_home = tmp_path / "real_home"
    (real_home / ".claude").mkdir(parents=True)
    real_cred = real_home / ".claude" / ".credentials.json"
    real_cred.write_text("stale-file-token")

    sandbox = tmp_path / "sandbox"
    (sandbox / ".claude").mkdir(parents=True)
    (sandbox / ".claude" / ".credentials.json").symlink_to(real_cred)

    monkeypatch.setattr(
        sandbox_home, "_read_macos_keychain_credential", lambda service: "fresh-keychain-token"
    )
    sandbox_home._reconcile_claude_credential(sandbox)

    p = sandbox / ".claude" / ".credentials.json"
    assert not p.is_symlink()
    assert p.read_text() == "fresh-keychain-token"


def test_reconcile_claude_credential_falls_back_to_symlink_when_keychain_unavailable(tmp_path, monkeypatch):
    real_home = tmp_path / "real_home"
    (real_home / ".claude").mkdir(parents=True)
    real_cred = real_home / ".claude" / ".credentials.json"
    real_cred.write_text("real-file-token")

    monkeypatch.setattr(Path, "home", lambda: real_home)
    monkeypatch.setattr(sandbox_home, "_read_macos_keychain_credential", lambda service: None)

    sandbox = tmp_path / "sandbox"
    sandbox_home._reconcile_claude_credential(sandbox)

    p = sandbox / ".claude" / ".credentials.json"
    assert p.is_symlink()
    assert p.resolve() == real_cred.resolve()


def test_reconcile_credential_link_routes_claudecode_through_keychain_path(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        sandbox_home, "_reconcile_claude_credential", lambda home: called.setdefault("home", home)
    )
    sandbox_home.reconcile_credential_link("claudecode", tmp_path)
    assert called["home"] == tmp_path


def test_reconcile_credential_link_still_symlinks_other_backends(tmp_path, monkeypatch):
    real_home = tmp_path / "real_home"
    (real_home / ".codex").mkdir(parents=True)
    real_auth = real_home / ".codex" / "auth.json"
    real_auth.write_text("codex-token")

    monkeypatch.setattr(Path, "home", lambda: real_home)

    sandbox = tmp_path / "sandbox"
    sandbox_home.reconcile_credential_link("codex", sandbox)

    p = sandbox / ".codex" / "auth.json"
    assert p.is_symlink()
    assert p.resolve() == real_auth.resolve()
