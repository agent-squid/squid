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
from pathlib import Path
from typing import Optional

from . import stats_db
from .config import SQUID_HOMES

_HOMES = Path(SQUID_HOMES)

# Credential file to symlink into a Blank Home sandbox, relative to $HOME,
# keyed by backend_id (matches the strings runners.py passes as `backend`).
# Fixed table, never inferred by scanning $HOME -- see ADR-0036.
_CREDENTIAL_RELPATHS: dict[str, str] = {
    "claudecode": ".claude/.credentials.json",
    "codex": ".codex/auth.json",
    "cursor": ".cursor/cli-config.json",
    # opencode: credential file location not yet confirmed -- see ADR-0036
    # Open items. Blank Home still isolates its config/plugins, but a
    # sandboxed opencode agent comes up logged out until this is resolved.
    # pi: auth is env-var only (ANTHROPIC_API_KEY / ANTHROPIC_OAUTH_TOKEN),
    # nothing to link.
}

_XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def sandbox_home_path(agent: str) -> Path:
    return _HOMES / agent


def reconcile_credential_link(backend_id: str, home: Path) -> None:
    """Self-healing symlink reconciliation -- see ADR-0036 "Storage and spawn
    mechanics". Idempotent, safe to call before every turn:

    1. Already a symlink to the real path -- nothing to do.
    2. A regular file -- a temp+rename refresh severed the link. The
       sandbox copy is the freshest token (written most recently), so copy
       it back onto the real path first, then delete it and relink.
    3. Missing -- first provisioning, just link.
    """
    rel = _CREDENTIAL_RELPATHS.get(backend_id)
    if rel is None:
        return
    real_path = Path.home() / rel
    sandbox_path = home / rel
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
