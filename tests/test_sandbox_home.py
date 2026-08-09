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


def test_reconcile_credential_link_symlinks_opencode_sqlite_db(tmp_path, monkeypatch):
    # opencode keeps sessions + native-login credentials in one SQLite DB
    # rather than loose files -- one symlink must cover both.
    real_home = tmp_path / "real_home"
    db_dir = real_home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    real_db = db_dir / "opencode.db"
    real_db.write_text("fake-db-content")

    monkeypatch.setattr(Path, "home", lambda: real_home)

    sandbox = tmp_path / "sandbox"
    sandbox_home.reconcile_credential_link("opencode", sandbox)

    p = sandbox / ".local" / "share" / "opencode" / "opencode.db"
    assert p.is_symlink()
    assert p.resolve() == real_db.resolve()


def test_reconcile_opencode_db_preserves_preexisting_sandbox_db_instead_of_overwriting_real(tmp_path, monkeypatch):
    # A sandbox that ran opencode before the symlink fix existed grew its own
    # local DB at the sandbox path. Unlike a small credential token, this is
    # a large multi-agent-shared DB -- copying it onto the real path the way
    # _reconcile_one does for tokens would silently destroy every other
    # agent's session history. It must be preserved as a backup, never
    # copied over the real (canonical) DB.
    real_home = tmp_path / "real_home"
    db_dir = real_home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    real_db = db_dir / "opencode.db"
    real_db.write_text("real-shared-db-with-everyones-sessions")

    monkeypatch.setattr(Path, "home", lambda: real_home)

    sandbox = tmp_path / "sandbox"
    sandbox_db_dir = sandbox / ".local" / "share" / "opencode"
    sandbox_db_dir.mkdir(parents=True)
    stray_db = sandbox_db_dir / "opencode.db"
    stray_db.write_text("stray-sandbox-local-db")

    sandbox_home.reconcile_credential_link("opencode", sandbox)

    p = sandbox_db_dir / "opencode.db"
    assert p.is_symlink()
    assert p.resolve() == real_db.resolve()
    assert real_db.read_text() == "real-shared-db-with-everyones-sessions"  # untouched

    backup = sandbox_db_dir / "opencode.db.pre-symlink.bak"
    assert backup.exists()
    assert not backup.is_symlink()
    assert backup.read_text() == "stray-sandbox-local-db"


def test_reconcile_opencode_db_is_idempotent_once_symlinked(tmp_path, monkeypatch):
    real_home = tmp_path / "real_home"
    db_dir = real_home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    real_db = db_dir / "opencode.db"
    real_db.write_text("real-db")

    monkeypatch.setattr(Path, "home", lambda: real_home)

    sandbox = tmp_path / "sandbox"
    sandbox_home.reconcile_credential_link("opencode", sandbox)
    sandbox_home.reconcile_credential_link("opencode", sandbox)  # second call, already linked

    p = sandbox / ".local" / "share" / "opencode" / "opencode.db"
    assert p.is_symlink()
    assert p.resolve() == real_db.resolve()
