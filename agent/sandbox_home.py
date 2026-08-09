"""Per-agent sandboxed $HOME for harness subprocesses.

Implements the "Blank Home" mode from docs/decisions/0036-sandboxed-home-per-agent.md:
an opt-in mode that points a harness subprocess's HOME at an empty,
persistent-per-agent directory (isolating plugins/skills/settings/
history), with its credential file symlinked in from the real $HOME so the
agent is authenticated immediately.

Lives under /tmp, not ~/.squid/ -- same reasoning as ADR-0012's context-sync
directory. A directory nested under the real $HOME risks upward config
discovery (CLAUDE.md, etc.) eventually reaching the real $HOME itself,
defeating the isolation. See SQUID_HOMES in agent/config.py.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import stats_db
from .config import SQUID_HOMES

_HOMES = Path(SQUID_HOMES)

# Credential/generated-config files to symlink into a Blank Home sandbox,
# relative to $HOME, keyed by backend_id (matches the strings runners.py
# passes as `backend`). Fixed table, never inferred by scanning $HOME -- see
# ADR-0036. Most backends need only their credential file; pi additionally
# needs models.json, the squid-generated custom-provider registry
# (agent/resolve.py's PI_MODELS_FILE) -- without it a sandboxed pi has no
# record of non-standard providers like Ollama and fails with "Model ...
# not found" even though auth is otherwise fine.
_CREDENTIAL_RELPATHS: dict[str, tuple[str, ...]] = {
    "claudecode": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "cursor": (".cursor/cli-config.json",),
    "pi": (".pi/agent/auth.json", ".pi/agent/models.json"),
    # opencode keeps everything -- sessions, messages, and any native-login
    # credential -- in one SQLite DB rather than loose files (confirmed by
    # inspecting it directly: `session`/`message`/`part`/`credential` tables).
    # One symlink covers both gaps that used to affect it: sessions were
    # unresumable ("Error: Session not found") and native-login auth was
    # unavailable under Blank Home. Custom/gateway providers (Ollama,
    # DeepSeek, etc.) don't need this at all -- opencode takes those via the
    # OPENCODE_CONFIG_CONTENT env var (agent/resolve.py), never a file, so
    # they already worked under Blank Home before this fix. Verified the
    # WAL-mode sidecar files (-wal/-shm) resolve safely through a symlink --
    # SQLite computes their names from the realpath()-resolved target, not
    # the symlink's own directory, so a real and a sandboxed process reading
    # the same DB share the same WAL state rather than diverging.
    "opencode": (".local/share/opencode/opencode.db",),
}

_XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")

# On macOS, Claude Code's *live* OAuth token is refreshed into this Keychain
# item, not (reliably) into ~/.claude/.credentials.json -- confirmed by
# testing directly: a fresh Keychain-sourced token written standalone into a
# sandbox authenticates correctly, while the plain-symlinked file goes stale
# within hours once the real environment's background refreshes stop landing
# in it (see ADR-0036). claudecode is handled separately from
# _CREDENTIAL_RELPATHS/_reconcile_one for this reason.
_MACOS_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def sandbox_home_path(agent: str) -> Path:
    return _HOMES / agent


def _reconcile_one(real_path: Path, sandbox_path: Path) -> None:
    if not real_path.exists():
        return  # nothing to link yet -- user hasn't logged in on real HOME
    if sandbox_path.is_symlink():
        if sandbox_path.resolve() == real_path.resolve():
            return
        sandbox_path.unlink()
    elif sandbox_path.exists():
        shutil.copyfile(sandbox_path, real_path)
        sandbox_path.unlink()
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox_path.symlink_to(real_path)


def _read_macos_keychain_credential(service: str) -> Optional[str]:
    """Read a generic-password Keychain item by service name, or None if
    unavailable -- not macOS, item missing, keychain locked/inaccessible, or
    the `security` CLI itself is missing. Never raises: Blank Home
    reconciliation must fail open (see current_home_mode's docstring) rather
    than block a turn on a Keychain hiccup."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.rstrip("\n")


def _reconcile_opencode_db(home: Path) -> None:
    """opencode-specific reconciliation: never let a sandbox-local DB
    overwrite the real one.

    _reconcile_one's "regular file present -> copy it onto the real path"
    branch is correct for a small single-value credential token (a
    temp+rename refresh inside the sandbox left a fresher copy there). It is
    wrong for opencode.db: a multi-session, multi-agent, potentially
    many-MB SQLite file shared by every opencode agent. A regular file
    sitting at the sandbox path here almost always means this agent ran
    before the symlink fix existed and grew its own isolated local DB --
    copying that onto the real path would silently destroy every other
    agent's session history. Preserve it as a `.pre-symlink.bak` instead of
    deleting or overwriting, then always link to the real (canonical) path.
    """
    rel = _CREDENTIAL_RELPATHS["opencode"][0]
    real_path = Path.home() / rel
    sandbox_path = home / rel
    if not real_path.exists():
        return  # nothing to link yet -- opencode never run on real HOME
    if sandbox_path.is_symlink():
        if sandbox_path.resolve() == real_path.resolve():
            return
        sandbox_path.unlink()
    elif sandbox_path.exists():
        backup = sandbox_path.with_name(sandbox_path.name + ".pre-symlink.bak")
        if backup.exists():
            sandbox_path.unlink()
        else:
            sandbox_path.rename(backup)
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox_path.symlink_to(real_path)


def _reconcile_claude_credential(home: Path) -> None:
    """claudecode-specific reconciliation: write the current Keychain token
    into the sandbox on every call instead of symlinking the file (see
    _MACOS_CLAUDE_KEYCHAIN_SERVICE for why). Falls back to the plain
    symlink otherwise -- still correct on non-macOS (no Keychain divergence
    to begin with) and better than nothing if Keychain is momentarily
    unreadable."""
    rel = _CREDENTIAL_RELPATHS["claudecode"][0]
    sandbox_path = home / rel
    token = _read_macos_keychain_credential(_MACOS_CLAUDE_KEYCHAIN_SERVICE)
    if token is None:
        _reconcile_one(Path.home() / rel, sandbox_path)
        return
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    if sandbox_path.is_symlink() or sandbox_path.exists():
        sandbox_path.unlink()
    sandbox_path.write_text(token)
    sandbox_path.chmod(0o600)


def reconcile_credential_link(backend_id: str, home: Path) -> None:
    """Self-healing credential reconciliation -- see ADR-0036 "Storage and
    spawn mechanics". Idempotent, safe to call before every turn.

    claudecode gets its own path (see _reconcile_claude_credential); opencode
    gets its own path too (see _reconcile_opencode_db -- its DB is too big
    and too shared to trust with the copy-sandbox-onto-real credential-token
    healing below). Every other backend symlinks each of its relpaths (see
    _CREDENTIAL_RELPATHS):

    1. Already a symlink to the real path -- nothing to do.
    2. A regular file -- a temp+rename refresh severed the link. The
       sandbox copy is the freshest token (written most recently), so copy
       it back onto the real path first, then delete it and relink.
    3. Missing -- first provisioning, just link.
    """
    if backend_id == "claudecode":
        _reconcile_claude_credential(home)
        return
    if backend_id == "opencode":
        _reconcile_opencode_db(home)
        return
    for rel in _CREDENTIAL_RELPATHS.get(backend_id, ()):
        _reconcile_one(Path.home() / rel, home / rel)


def ensure_sandbox_home(agent: str, backend_id: str) -> Path:
    """Create the sandbox HOME dir if missing and reconcile its credential link."""
    home = sandbox_home_path(agent)
    home.mkdir(parents=True, exist_ok=True)
    reconcile_credential_link(backend_id, home)
    return home


def current_home_mode(agent: str) -> str:
    """'user_home' (default) or 'blank_home' for this agent.

    Fails open to 'user_home' if the setting can't be read (e.g. DB not
    initialized) -- sandboxing is opt-in and shouldn't block a turn.
    """
    if not agent:
        return "user_home"
    try:
        return stats_db.get_agent_home_mode(agent)
    except Exception:
        return "user_home"


def home_override_env(agent: str, backend_id: str) -> dict[str, Optional[str]]:
    """Extra env entries to sandbox this subprocess's HOME, or {} if not opted in.

    Merges directly into the extra_env dict _child_env() applies: string
    values set the var, None pops it (clearing XDG so it can't leak a real
    path through the sandboxed HOME -- see ADR-0036).
    """
    if current_home_mode(agent) != "blank_home":
        return {}
    home = ensure_sandbox_home(agent, backend_id)
    env: dict[str, Optional[str]] = {"HOME": str(home)}
    for var in _XDG_VARS:
        env[var] = None
    return env
