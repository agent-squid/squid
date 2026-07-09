"""
worktree.py — Git worktree isolation per (topic, agent) session.

Worktrees live at ~/.squid/worktrees/<repo_hash>/sqd-<session_key>/
so they never appear in the user's project directory.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_WORKTREES_HOME = Path.home() / ".squid" / "worktrees"


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

    conflicts = (
        _run_git(repo_root, "diff", "--name-only", "--diff-filter=U", check=False)
        .stdout.strip()
        .splitlines()
    )
    log.warning("merge conflicts in %s: %s file(s)", repo_root, len(conflicts))
    return [f for f in conflicts if f]


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


def sync_after_turn(repo_root: Path, topic: str, agent: str, msg_id: Optional[int] = None) -> list[str]:
    """
    Called at end of each turn: commit worktree changes, merge to main, rebase worktree.
    Returns conflict file paths (empty = success). On conflict, merge is aborted and
    the worktree is left intact for the next turn (changes are not lost).
    """
    wt = worktree_path(repo_root, topic, agent)
    if not wt.exists():
        return []

    commit_msg = f"squid: turn {msg_id}" if msg_id else "squid: auto-commit"
    commit_worktree(wt, commit_msg)

    conflicts = merge_worktree(repo_root, topic, agent)
    if conflicts:
        abort_merge(repo_root)
        return conflicts

    main_head = _run_git(repo_root, "rev-parse", "HEAD", check=False).stdout.strip()
    if main_head:
        _run_git(wt, "rebase", main_head, check=False)
    return []


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
