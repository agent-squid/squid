"""
worktree.py — per-turn code-root isolation for (topic, agent) sessions.

Turn directories live at ~/.squid/worktrees/<repo_hash>/sqd-<session_key>/ so
they never appear in the user's project directory. Despite the module name,
a turn directory is a plain directory with no .git of its own — not a real
`git worktree` — so there is nothing in it for an agent to `git branch`/
`push`/`gh pr create` against (see ADR-0025, "Revised design", after a
2026-07-22 incident where an agent pushed a real per-turn worktree branch to
the real remote). The disposable integration worktree used for conflict
resolution in sync_after_turn is still a real `git worktree` — it is
Squid-internal only and never exposed to an agent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
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
_AUTO_LINK_DENY_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".squid",
    ".claude",
    "logs",
    "test-results",
    "playwright-report",
}


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

    linked_rels: list[Path] = []
    for rel in _linked_dir_rel_paths(repo_root):
        src = repo_root / rel
        dst = wt / rel
        if src == dst:
            continue  # defense in depth; unreachable given the wt == repo_root check above
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src, target_is_directory=True)
        if dst.is_symlink():
            linked_rels.append(rel)


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
        input=input,
    )


def _internal_commit_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Squid")
    env.setdefault("GIT_AUTHOR_EMAIL", "squid@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def _snapshot_tree(repo_root: Path) -> str:
    fd, index_path = tempfile.mkstemp(prefix="squid-worktree-index-")
    os.close(fd)
    os.unlink(index_path)
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        _run_git(repo_root, "read-tree", "HEAD", env=env)
        _run_git(repo_root, "add", "-A", "--", ".", env=env)
        for rel in set(_linked_dir_rel_paths(repo_root)) | set(_external_dir_symlink_rel_paths(repo_root)):
            _run_git(repo_root, "rm", "-r", "--cached", "--ignore-unmatch", "--", rel, env=env, check=False)
        return _run_git(repo_root, "write-tree", env=env).stdout.strip()
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def _materialize_tree(repo_root: Path, commit: str, dest: Path) -> None:
    """
    Extract commit's tree content into dest as plain files. dest is never a
    git repo — no .git, no branch, nothing for a turn to `git branch`/`push`/
    `gh pr create` against, regardless of what name it tries (ADR-0025).
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "archive", commit], cwd=str(repo_root), stdout=subprocess.PIPE,
    )
    extract = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout)
    archive.stdout.close()
    archive.wait()
    if archive.returncode != 0 or extract.returncode != 0:
        raise RuntimeError(
            f"materializing {commit} into {dest} failed "
            f"(archive exit {archive.returncode}, tar exit {extract.returncode})"
        )


def _snapshot_dir(repo_root: Path, work_tree: Path) -> str:
    """
    Build a git tree object for work_tree's current file content, using
    repo_root's object database. work_tree need not be a git repo itself —
    this is how a branchless turn directory (see _materialize_tree) gets
    captured. Unlike _snapshot_tree, this does not seed from `read-tree
    HEAD`: work_tree's content may have started from an older base than
    repo_root's current HEAD, and seeding from HEAD would make repo_root
    drift since the turn started look like a change the turn itself made.
    """
    fd, index_path = tempfile.mkstemp(prefix="squid-worktree-index-")
    os.close(fd)
    os.unlink(index_path)
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        env["GIT_WORK_TREE"] = str(work_tree)
        _run_git(repo_root, "add", "-A", "--", ".", env=env)
        for rel in set(_linked_dir_rel_paths(repo_root)) | set(_external_dir_symlink_rel_paths(work_tree)):
            _run_git(repo_root, "rm", "-r", "--cached", "--ignore-unmatch", "--", rel, env=env, check=False)
        return _run_git(repo_root, "write-tree", env=env).stdout.strip()
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def _linked_dir_rel_paths(repo_root: Path) -> list[Path]:
    rels = set(_configured_dependency_rel_paths(repo_root))
    if config.WORKTREE_AUTO_LINK_IGNORED_DIRS:
        rels.update(_ignored_dir_rel_paths(repo_root))
    return sorted(rels)


def _external_dir_symlink_rel_paths(repo_root: Path) -> list[Path]:
    """Find stale dependency symlinks that should never enter snapshot trees."""
    root = repo_root.resolve()
    root_depth = str(root).count(os.sep)
    rels: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root, followlinks=False):
        depth = str(dirpath).count(os.sep) - root_depth
        if depth >= _DEPENDENCY_SCAN_MAX_DEPTH:
            dirnames.clear()
            continue
        for dirname in list(dirnames):
            path = Path(dirpath) / dirname
            if not path.is_symlink():
                continue
            dirnames.remove(dirname)
            try:
                target = path.resolve(strict=True)
            except OSError:
                continue
            if target.is_dir():
                try:
                    target.relative_to(root)
                except ValueError:
                    rels.append(path.relative_to(root))
    return rels


def _configured_dependency_rel_paths(repo_root: Path) -> list[Path]:
    names = set(config.DEPENDENCY_DIRS)
    root_depth = str(repo_root).count(os.sep)
    rels: list[Path] = []
    for dirpath, dirnames, _files in os.walk(repo_root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        depth = str(dirpath).count(os.sep) - root_depth
        if depth >= _DEPENDENCY_SCAN_MAX_DEPTH:
            dirnames.clear()
            continue
        matched = [d for d in dirnames if d in names]
        for d in matched:
            dirnames.remove(d)
            rel = (Path(dirpath) / d).relative_to(repo_root)
            if _should_link_dir(rel):
                rels.append(rel)
    return rels


def _ignored_dir_rel_paths(repo_root: Path) -> list[Path]:
    result = _run_git(
        repo_root,
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory",
        check=False,
    )
    if result.returncode != 0:
        return []

    rels: list[Path] = []
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        rel = Path(raw.rstrip("/"))
        if (
            (repo_root / rel).is_dir()
            and len(rel.parts) <= _DEPENDENCY_SCAN_MAX_DEPTH
            and _should_link_dir(rel)
            and _should_auto_link_ignored_dir(rel)
        ):
            rels.append(rel)
    return rels


def _should_auto_link_ignored_dir(rel: Path) -> bool:
    return rel.name in set(config.DEPENDENCY_DIRS)


def _should_link_dir(rel: Path) -> bool:
    return not any(part in _AUTO_LINK_DENY_DIR_NAMES for part in rel.parts)


def _commit_tree(repo_root: Path, tree: str, message: str, parents: Optional[list[str]] = None) -> str:
    args = ["commit-tree", tree]
    for parent in parents or []:
        if parent:
            args.extend(["-p", parent])
    args.extend(["-m", message])
    return _run_git(repo_root, *args, env=_internal_commit_env()).stdout.strip()


def _restore_tree(repo_root: Path, from_tree: str, to_tree: str) -> Optional[str]:
    """Best-effort rollback from from_tree back to to_tree. Returns an error if rollback fails."""
    if from_tree == to_tree:
        return None
    rollback_patch = _run_git(repo_root, "diff", "--binary", from_tree, to_tree).stdout
    if not rollback_patch:
        return None
    rollback = _run_git(repo_root, "apply", check=False, input=rollback_patch)
    if rollback.returncode != 0:
        return f"rollback failed (exit {rollback.returncode}, stderr: {rollback.stderr.strip()!r})"
    return None


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


def base_ref_name(topic: str, agent: str) -> str:
    return f"refs/squid/worktrees/{_session_key(topic, agent)}/base"


def worktree_path(repo_root: Path, topic: str, agent: str) -> Path:
    repo_hash = hashlib.md5(str(repo_root).encode()).hexdigest()[:8]
    return _WORKTREES_HOME / repo_hash / branch_name(topic, agent)


def ensure_worktree(repo_root: Path, topic: str, agent: str) -> Path:
    """
    Get or create the isolated turn directory for (topic, agent). Returns its
    path. This is a plain directory with no .git of its own — nothing in it
    for an agent to `git branch`/`push`/`gh pr create` against, regardless of
    what name it tries (see ADR-0025, "Revised design").
    """
    wt = worktree_path(repo_root, topic, agent)
    if wt.exists():
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)
    head = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if config.WORKTREE_TRACK_DIRTY_CHANGES:
        seed_tree = _snapshot_tree(repo_root)
        base_commit = _commit_tree(repo_root, seed_tree, f"squid: hidden base for {topic}/{agent}", [head])
    else:
        base_commit = head
    _run_git(repo_root, "update-ref", base_ref_name(topic, agent), base_commit)
    _materialize_tree(repo_root, base_commit, wt)
    _link_dependency_dirs(repo_root, wt)
    log.info("worktree created: %s (branchless, base=%s)", wt, base_commit[:12])
    return wt


def base_commit_for(repo_root: Path, topic: str, agent: str) -> Optional[str]:
    out = _run_git(repo_root, "rev-parse", "--verify", base_ref_name(topic, agent), check=False).stdout.strip()
    return out or None


def integration_worktree_path(repo_root: Path, topic: str, agent: str) -> Path:
    return worktree_path(repo_root, topic, agent).with_name(f"{branch_name(topic, agent)}-integration")


def remove_worktree(repo_root: Path, topic: str, agent: str) -> None:
    """
    Remove the turn directory and its hidden base ref. The turn directory is
    a plain directory (no .git), so this is a filesystem remove, not
    `git worktree remove`. The integration worktree used for conflict
    resolution is still a real, disposable, Squid-internal git worktree.
    """
    wt = worktree_path(repo_root, topic, agent)
    integration_wt = integration_worktree_path(repo_root, topic, agent)
    _run_git(repo_root, "worktree", "remove", "--force", str(integration_wt), check=False)
    shutil.rmtree(wt, ignore_errors=True)
    _run_git(repo_root, "update-ref", "-d", base_ref_name(topic, agent), check=False)
    log.info("worktree removed: %s", wt)


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
    turn_tree: Optional[str] = None,
) -> list[str]:
    """
    Called at end of each turn: merge the turn's file tree with the current
    code root in an integration worktree, then promote the clean result back as
    ordinary local changes. Returns conflict file paths (empty = success).
    """
    del request_text, response_text
    wt = worktree_path(repo_root, topic, agent)
    if not wt.exists():
        return []

    with _lock_for(repo_root):
        base_commit = _base_commit_from_registry(repo_root, topic, agent) or base_commit_for(repo_root, topic, agent)
        if not base_commit:
            raise RuntimeError(f"missing base commit for worktree {wt}")
        turn_tree = turn_tree or _snapshot_dir(repo_root, wt)
        base_tree = _run_git(repo_root, "show", "--format=%T", "--no-patch", base_commit).stdout.strip()
        if turn_tree == base_tree:
            return []

        current_tree = _snapshot_tree(repo_root)
        current_commit = _commit_tree(
            repo_root, current_tree, f"squid: hidden current for turn {msg_id}", [base_commit]
        )
        turn_commit = _commit_tree(
            repo_root, turn_tree, f"squid: hidden turn for turn {msg_id}", [base_commit]
        )
        integration_wt = integration_worktree_path(repo_root, topic, agent)
        _run_git(repo_root, "worktree", "remove", "--force", str(integration_wt), check=False)
        result = _run_git(
            repo_root, "worktree", "add", "--detach", str(integration_wt), current_commit,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to create integration worktree: {result.stderr.strip()}")

        keep_integration_wt = False
        try:
            result = _run_git(integration_wt, "merge", "--no-commit", "--no-ff", turn_commit, check=False)
            if result.returncode != 0:
                conflicts = [
                    f for f in (
                        _run_git(integration_wt, "diff", "--name-only", "--diff-filter=U", check=False)
                        .stdout.strip()
                        .splitlines()
                    )
                    if f
                ]
                if conflicts:
                    keep_integration_wt = True
                    log.warning("integration merge conflicts in %s: %s file(s)", repo_root, len(conflicts))
                    return conflicts
                raise RuntimeError(
                    f"git merge {turn_commit} failed in integration worktree without "
                    f"conflict markers (exit {result.returncode}, stderr: {result.stderr.strip()!r})"
                )

            merged_tree = _run_git(integration_wt, "write-tree").stdout.strip()
            if _snapshot_tree(repo_root) != current_tree:
                raise RuntimeError("code root changed during worktree promotion; retry on next cleanup")
            patch = _run_git(repo_root, "diff", "--binary", current_tree, merged_tree).stdout
            if patch:
                check = _run_git(repo_root, "apply", "--check", check=False, input=patch)
                if check.returncode != 0:
                    raise RuntimeError(
                        "promotion patch did not apply cleanly "
                        f"(exit {check.returncode}, stderr: {check.stderr.strip()!r})"
                    )
                applied = _run_git(repo_root, "apply", check=False, input=patch)
                if applied.returncode != 0:
                    after_failed_apply_tree = _snapshot_tree(repo_root)
                    rollback_error = _restore_tree(repo_root, after_failed_apply_tree, current_tree)
                    raise RuntimeError(
                        "promotion patch apply failed after clean check "
                        f"(exit {applied.returncode}, stderr: {applied.stderr.strip()!r}"
                        + (f", {rollback_error}" if rollback_error else ", rolled back partial changes")
                        + ")"
                    )
        finally:
            if not keep_integration_wt:
                _run_git(repo_root, "worktree", "remove", "--force", str(integration_wt), check=False)
    return []


def _base_commit_from_registry(repo_root: Path, topic: str, agent: str) -> Optional[str]:
    try:
        from .stats_db import get_worktrees

        for rec in get_worktrees(topic, agent):
            if Path(rec["repo_root"]).resolve() == repo_root.resolve():
                return rec.get("base_commit")
    except Exception:
        log.debug("base commit registry lookup failed topic=%s agent=%s", topic, agent, exc_info=True)
    return None


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
    from .stats_db import get_all_worktrees_for_topic, delete_worktree, mark_worktree_status

    active = get_active_msg_ids()
    records = await asyncio.to_thread(get_all_worktrees_for_topic, topic)
    conflicts: dict[str, list[str]] = {}

    for rec in records:
        repo_root = Path(rec["repo_root"])
        wt_key = rec["agent"]
        if rec.get("status") in {"conflict", "promotion_failed"}:
            log.debug(
                "worktree skip — requires manual attention: topic=%s key=%s status=%s",
                topic, wt_key, rec.get("status"),
            )
            continue
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
                await asyncio.to_thread(mark_worktree_status, topic, wt_key, rec["repo_root"], "conflict")
                conflicts[rec["repo_root"]] = conflict_files
            else:
                await asyncio.to_thread(remove_worktree, repo_root, topic, wt_key)
                await asyncio.to_thread(delete_worktree, topic, wt_key, rec["repo_root"])
        except Exception:
            await asyncio.to_thread(mark_worktree_status, topic, wt_key, rec["repo_root"], "promotion_failed")
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
