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
import logging
import os
import subprocess
from pathlib import Path

from .config import SQUID_HOME

log = logging.getLogger(__name__)

CONTEXT_DIR = str(Path(__file__).parent.parent / "context")
_last_sync_mtime: float = 0.0


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
