"""
context_sync.py — keep /tmp/<user>/squid in sync with squid/context/.

The tmp dir cannot be a symlink to context/ because Claude Code resolves the
real path, which would load CLAUDE.md and MCP config from the wrong scope.
Instead we maintain it as a real directory and rsync into it.

Strategy (Option 4):
  - sync_now()   — blocking full sync; call once at daemon startup
  - maybe_sync() — async stat-check; call before each prompt; only rsyncs
                   when context/ has changed since the last sync
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import SQUID_HOME

log = logging.getLogger(__name__)

CONTEXT_DIR = str(Path.home() / ".squid" / "context")
# Bundled default context (topics/roles seed content), shipped alongside
# ui/ and config/ in the wheel (see pyproject.toml's force-include). Sibling
# to this package the same way UI_DIR is in agent/server.py.
_BUNDLED_CONTEXT = Path(__file__).parent.parent / "context"
_last_sync_mtime: float = 0.0


def _seed_context_if_empty() -> None:
    """Copy the bundled default context/ into ~/.squid/context on first run.

    bin/install.sh and bin/start.sh do this same "only if empty" copy for a
    source checkout, but a pip/pipx install never runs either script, so
    this has to also happen here — otherwise a fresh pipx install has no
    default topics/roles content and nothing seeds it.
    """
    context_dir = Path(CONTEXT_DIR)
    if context_dir.exists() and any(context_dir.iterdir()):
        return
    if not _BUNDLED_CONTEXT.exists():
        return
    context_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_BUNDLED_CONTEXT, context_dir, dirs_exist_ok=True)
_CODEX_MANAGED_BEGIN = "# BEGIN SQUID MANAGED MCP"
_CODEX_MANAGED_END = "# END SQUID MANAGED MCP"


def _rmdir_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and parent.is_relative_to(stop):
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _remove_json_server(path: Path, section: str, empty_shape: dict, context_dir: Path) -> None:
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(config, dict):
        return
    servers = config.get(section)
    if not isinstance(servers, dict) or "squid" not in servers:
        return
    servers.pop("squid", None)
    if config == empty_shape:
        path.unlink()
        _rmdir_empty_parents(path, context_dir)
        return
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _remove_codex_managed_block(path: Path, context_dir: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start = text.find(_CODEX_MANAGED_BEGIN)
    end = text.find(_CODEX_MANAGED_END)
    if start < 0 or end < start:
        return
    end += len(_CODEX_MANAGED_END)
    updated = (text[:start].rstrip() + "\n" + text[end:].lstrip()).strip()
    if not updated:
        path.unlink()
        _rmdir_empty_parents(path, context_dir)
        return
    path.write_text(updated + "\n", encoding="utf-8")


def _remove_legacy_mcp_context_files(context_dir: Path) -> None:
    _remove_json_server(context_dir / ".mcp.json", "mcpServers", {"mcpServers": {}}, context_dir)
    _remove_json_server(context_dir / ".cursor" / "mcp.json", "mcpServers", {"mcpServers": {}}, context_dir)
    _remove_json_server(
        context_dir / "opencode.json",
        "mcp",
        {"$schema": "https://opencode.ai/config.json", "mcp": {}},
        context_dir,
    )
    _remove_codex_managed_block(context_dir / ".codex" / "config.toml", context_dir)


def _tree_mtime(path: str) -> float:
    """Max mtime across all entries under path (handles nested .claude/ dirs)."""
    result = 0.0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                result = max(result, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return result


def _rsync() -> bool:
    result = subprocess.run(
        ["rsync", "-a", "--delete", f"{CONTEXT_DIR}/", SQUID_HOME],
        capture_output=True,
    )
    if result.returncode != 0:
        log.warning("context sync failed: %s", result.stderr.decode().strip())
        return False
    return True


def sync_now() -> None:
    """Full blocking sync at daemon startup. Creates SQUID_HOME if needed."""
    global _last_sync_mtime
    os.makedirs(SQUID_HOME, exist_ok=True)
    _seed_context_if_empty()
    _remove_legacy_mcp_context_files(Path(CONTEXT_DIR))
    if _rsync():
        _last_sync_mtime = _tree_mtime(CONTEXT_DIR)
        log.info("context synced → %s", SQUID_HOME)


async def maybe_sync() -> None:
    """Async stat-check before each prompt. No-op when nothing changed."""
    global _last_sync_mtime
    loop = asyncio.get_event_loop()
    current = await loop.run_in_executor(None, _tree_mtime, CONTEXT_DIR)
    if current <= _last_sync_mtime:
        return
    log.info("context changed, resyncing → %s", SQUID_HOME)
    proc = await asyncio.create_subprocess_exec(
        "rsync", "-a", "--delete", f"{CONTEXT_DIR}/", SQUID_HOME,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("context sync failed: %s", stderr.decode().strip())
    else:
        _last_sync_mtime = current
