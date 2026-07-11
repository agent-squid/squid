"""
worktree.py — Git worktree isolation per (topic, agent) session.

Worktrees live at ~/.squid/worktrees/<repo_hash>/sqd-<session_key>/
so they never appear in the user's project directory.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger(__name__)

_WORKTREES_HOME = Path.home() / ".squid" / "worktrees"

# Minimum time since a worktree was last touched before cleanup_worktrees()
# will remove it. A turn's own CLI process is tracked and excluded via
# get_active_msg_ids(), but a background command it spawned (e.g. a bash
# tool run in the background) isn't — this grace period gives such
# processes a chance to finish before their cwd is deleted out from under
# them, without blocking removal indefinitely.
_CLEANUP_GRACE_SECONDS = 30

# Serializes commit/merge/rebase against a given repo_root's .git, since it's
# shared mutable state across every turn (including parallel adhoc turns) of
# a topic, unlike the per-turn worktrees which never overlap.
_repo_locks_guard = threading.Lock()
_repo_locks: dict[str, threading.Lock] = {}


def _lock_for(repo_root: Path) -> threading.Lock:
    key = str(repo_root.resolve())
    with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[key] = lock
        return lock

# How deep under repo_root to search for dependency directories to symlink.
_DEPENDENCY_SCAN_MAX_DEPTH = 4


def _link_dependency_dirs(repo_root: Path, wt: Path) -> None:
    """
    Symlink installed-dependency directories (node_modules, .venv, etc. —
    see config.DEPENDENCY_DIRS) from repo_root into wt, since `git worktree
    add` only materializes tracked files and these are always gitignored.
    Matched by directory name; not recursed into once matched.
    """
    repo_root = repo_root.resolve()
    wt = wt.resolve()
    if wt == repo_root:
        log.error("refusing to link dependency dirs: worktree path equals repo_root (%s)", repo_root)
        return

    names = set(config.DEPENDENCY_DIRS)
    root_depth = str(repo_root).count(os.sep)
    for dirpath, dirnames, _files in os.walk(repo_root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        depth = str(dirpath).count(os.sep) - root_depth
        if depth >= _DEPENDENCY_SCAN_MAX_DEPTH:
            dirnames.clear()
            continue
        matched = [d for d in dirnames if d in names]
        for d in matched:
            dirnames.remove(d)  # dependency dirs don't nest further matches
            src = Path(dirpath) / d
            dst = wt / src.relative_to(repo_root)
            if src == dst:
                continue  # defense in depth; unreachable given the wt == repo_root check above
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() and not dst.is_symlink():
                dst.symlink_to(src, target_is_directory=True)


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def repo_root_for(path: str) -> Optional[Path]:
    """Return the main git repo root for path, or None if not in a git repo."""
    try:
        p = Path(path).expanduser().resolve()
        out = _run_git(p, "rev-parse", "--show-toplevel").stdout.strip()
        return Path(out).resolve() if out else None
    except (OSError, subprocess.CalledProcessError):
        return None


def _session_key(topic: str, agent: str) -> str:
    raw = f"{topic}:{agent}"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", f"{topic}-{agent}")[:30].strip("-")
    suffix = hashlib.md5(raw.encode()).hexdigest()[:6]
    return f"{slug}-{suffix}"


def branch_name(topic: str, agent: str) -> str:
    return f"sqd-{_session_key(topic, agent)}"


def worktree_path(repo_root: Path, topic: str, agent: str) -> Path:
    repo_hash = hashlib.md5(str(repo_root).encode()).hexdigest()[:8]
    return _WORKTREES_HOME / repo_hash / branch_name(topic, agent)


def ensure_worktree(repo_root: Path, topic: str, agent: str) -> Path:
    """Get or create the worktree for (topic, agent). Returns the worktree path."""
    wt = worktree_path(repo_root, topic, agent)
    br = branch_name(topic, agent)

    if wt.exists():
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)
    existing_branch = _run_git(repo_root, "branch", "--list", br, check=False).stdout.strip()
    if existing_branch:
        _run_git(repo_root, "worktree", "add", str(wt), br)
    else:
        _run_git(repo_root, "worktree", "add", "-b", br, str(wt), "HEAD")
    _link_dependency_dirs(repo_root, wt)
    log.info("worktree created: %s branch=%s", wt, br)
    return wt


def remove_worktree(repo_root: Path, topic: str, agent: str) -> None:
    """Remove the worktree directory and delete its branch."""
    wt = worktree_path(repo_root, topic, agent)
    br = branch_name(topic, agent)
    _run_git(repo_root, "worktree", "remove", "--force", str(wt), check=False)
    _run_git(repo_root, "branch", "-D", br, check=False)
    log.info("worktree removed: %s", wt)


def merge_worktree(repo_root: Path, topic: str, agent: str) -> list[str]:
    """
    Merge the session branch into the current HEAD of repo_root.
    Returns a list of conflicted file paths (empty = clean merge or nothing to merge).
    Caller must call abort_merge() if conflicts are returned.

    Raises RuntimeError if the merge command itself fails without producing
    conflict markers (e.g. it couldn't even start — lock contention from a
    concurrent git operation on repo_root). That case must never be treated
    as a clean merge: doing so silently drops the turn's changes.
    """
    br = branch_name(topic, agent)
    if not _run_git(repo_root, "branch", "--list", br, check=False).stdout.strip():
        return []

    ahead = _run_git(
        repo_root, "rev-list", f"HEAD..{br}", "--count", check=False
    ).stdout.strip()
    if ahead == "0":
        return []

    result = _run_git(repo_root, "merge", "--no-ff", br, check=False)
    if result.returncode == 0:
        return []

    conflicts = [
        f for f in (
            _run_git(repo_root, "diff", "--name-only", "--diff-filter=U", check=False)
            .stdout.strip()
            .splitlines()
        )
        if f
    ]
    if not conflicts:
        raise RuntimeError(
            f"git merge --no-ff {br} failed in {repo_root} without producing "
            f"conflict markers (exit {result.returncode}, stderr: {result.stderr.strip()!r})"
        )
    log.warning("merge conflicts in %s: %s file(s)", repo_root, len(conflicts))
    return conflicts


def abort_merge(repo_root: Path) -> None:
    _run_git(repo_root, "merge", "--abort", check=False)


def _has_changes(wt_path: Path) -> bool:
    return bool(_run_git(wt_path, "status", "--porcelain", check=False).stdout.strip())


def commit_worktree(wt_path: Path, msg: str) -> bool:
    """Stage all changes and commit. Returns True if a commit was made."""
    if not _has_changes(wt_path):
        return False
    _run_git(wt_path, "add", "-A", check=False)
    _run_git(wt_path, "commit", "-m", msg, check=False)
    return True


def _build_commit_message(
    msg_id: Optional[int], request_text: Optional[str], response_text: Optional[str]
) -> str:
    subject = f"squid: turn {msg_id}" if msg_id else "squid: auto-commit"
    body_parts = []
    if request_text and request_text.strip():
        body_parts.append(f"Request:\n{request_text.strip()}")
    if response_text and response_text.strip():
        body_parts.append(f"Response:\n{response_text.strip()}")
    if not body_parts:
        return subject
    return subject + "\n\n" + "\n\n".join(body_parts)


def sync_after_turn(
    repo_root: Path, topic: str, agent: str, msg_id: Optional[int] = None,
    request_text: Optional[str] = None, response_text: Optional[str] = None,
) -> list[str]:
    """
    Called at end of each turn: commit worktree changes, merge to main, rebase worktree.
    Returns conflict file paths (empty = success). On conflict, merge is aborted and
    the worktree is left intact for the next turn (changes are not lost). Also left
    intact (via a propagated exception) if the merge fails outright rather than
    conflicting — see merge_worktree.
    """
    wt = worktree_path(repo_root, topic, agent)
    if not wt.exists():
        return []

    commit_msg = _build_commit_message(msg_id, request_text, response_text)
    commit_worktree(wt, commit_msg)

    with _lock_for(repo_root):
        conflicts = merge_worktree(repo_root, topic, agent)
        if conflicts:
            abort_merge(repo_root)
            return conflicts

        main_head = _run_git(repo_root, "rev-parse", "HEAD", check=False).stdout.strip()
        if main_head:
            _run_git(wt, "rebase", main_head, check=False)
    return []


def _seconds_since(iso_ts: str) -> float:
    dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def cleanup_worktrees(topic: str) -> dict[str, list[str]]:
    """
    Best-effort sweep: sync and remove worktrees left over from finished turns.
    Called at the start of each new turn dispatch (see TopicDispatcher.dispatch)
    and at session close (clear / session-delete). Never blocks turn
    admission — callers should treat failures as non-fatal.

    Skips worktrees whose turn is still running (registered msg_id), and ones
    touched too recently to safely assume a background process they spawned
    has wound down. Worktrees with unresolved merge conflicts are kept alive.
    """
    from .runners import get_active_msg_ids
    from .stats_db import get_all_worktrees_for_topic, delete_worktree

    active = get_active_msg_ids()
    records = await asyncio.to_thread(get_all_worktrees_for_topic, topic)
    conflicts: dict[str, list[str]] = {}

    for rec in records:
        repo_root = Path(rec["repo_root"])
        wt_key = rec["agent"]
        try:
            if int(wt_key) in active:
                log.debug("worktree skip — turn still active: topic=%s key=%s", topic, wt_key)
                continue
        except (ValueError, TypeError):
            pass  # non-numeric wt_key: pre-dates per-turn keying, treat as orphan
        if _seconds_since(rec["last_used_at"]) < _CLEANUP_GRACE_SECONDS:
            continue
        try:
            conflict_files = await asyncio.to_thread(
                sync_after_turn, repo_root, topic, wt_key, None
            )
            if conflict_files:
                conflicts[rec["repo_root"]] = conflict_files
            else:
                await asyncio.to_thread(remove_worktree, repo_root, topic, wt_key)
                await asyncio.to_thread(delete_worktree, topic, wt_key, rec["repo_root"])
        except Exception:
            log.exception("worktree cleanup failed for %s topic=%s key=%s", repo_root, topic, wt_key)

    return conflicts


def map_to_worktree(path: str, worktree_map: dict[Path, Path]) -> Optional[str]:
    """Remap an absolute path to its equivalent inside a worktree."""
    p = Path(path).expanduser().resolve()
    for repo_root, wt_path in worktree_map.items():
        try:
            rel = p.relative_to(repo_root)
            return str(wt_path / rel)
        except ValueError:
            continue
    return None
