"""
git_changes.py - Git-backed direct watch tracking for topic code roots.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 500_000


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def _repo_root(cwd: Path) -> Optional[Path]:
    try:
        out = _run_git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out).resolve() if out else None


def _snapshot_tree(repo_root: Path, work_tree: Optional[Path] = None) -> str:
    """
    Build a tree object for work_tree's current file content, using
    repo_root's object database. Defaults to snapshotting repo_root itself
    (its own dirty state relative to HEAD).

    When work_tree is a separate, branchless per-turn directory (no .git of
    its own — see ADR-0025), it is captured via GIT_WORK_TREE instead, with
    no `read-tree HEAD` seeding: the turn directory may have started from an
    older base than repo_root's current HEAD, and seeding from HEAD would
    make repo_root drift since the turn started look like part of the turn's
    own diff.
    """
    fd, index_path = tempfile.mkstemp(prefix="agent-squid-index-")
    os.close(fd)
    os.unlink(index_path)
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        if work_tree is not None and work_tree != repo_root:
            env["GIT_WORK_TREE"] = str(work_tree)
        else:
            _run_git(repo_root, "read-tree", "HEAD", env=env)
        _run_git(repo_root, "add", "-A", "--", ".", env=env)
        if work_tree is not None and work_tree != repo_root:
            from .worktree import _external_dir_symlink_rel_paths, _linked_dir_rel_paths

            for rel in set(_linked_dir_rel_paths(repo_root)) | set(_external_dir_symlink_rel_paths(work_tree)):
                _run_git(repo_root, "rm", "-r", "--cached", "--ignore-unmatch", "--", str(rel), env=env, check=False)
        return _run_git(repo_root, "write-tree", env=env).stdout.strip()
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def _parse_name_status(output: str) -> list[dict]:
    files: list[dict] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            item = {"status": parts[0], "path": parts[-1]}
            if len(parts) > 2:
                item["old_path"] = parts[1]
            files.append(item)
    return files


def _parse_numstat(output: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            additions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return additions, deletions


def _diff_anchor_ref(msg_id: int) -> str:
    return f"refs/squid/diffs/{msg_id}"


def _anchor_diff_trees(repo_root: Path, msg_id: Optional[int], base_tree: str, head_tree: str) -> None:
    """Keep base_tree/head_tree reachable indefinitely so a later on-demand
    per-file diff (e.g. a file this turn's diff omitted for size) can still be
    recomputed from them, even after `git gc` would otherwise prune a bare,
    ref-less tree. Best-effort: this must never fail the turn itself — chat
    history/diffs are already retained indefinitely (ADR-0029), so once
    created this ref is never deleted either.
    """
    if msg_id is None:
        return
    try:
        from .worktree import _commit_tree, _internal_commit_env  # local: avoid any import-time cycle

        base_commit = _commit_tree(repo_root, base_tree, f"squid: diff base for turn {msg_id}")
        head_commit = _commit_tree(repo_root, head_tree, f"squid: diff head for turn {msg_id}", [base_commit])
        _run_git(repo_root, "update-ref", _diff_anchor_ref(msg_id), head_commit, check=False)
    except Exception:
        log.debug("diff anchor ref failed for msg_id=%s", msg_id, exc_info=True)


def diff_for_path(repo_root: Path, base: str, head: str, path: str) -> str:
    """Recompute a single file's diff live from stored (base, head) tree hashes.
    Used for files a turn's stored diff omitted for size — those still have
    their trees anchored via _anchor_diff_trees, so this works long after the
    turn ended. Returns '' if either tree is gone (e.g. pre-anchor history, or
    the anchor ref was somehow removed) rather than raising.
    """
    result = _run_git(repo_root, "diff", "--binary", "--unified=3", base, head, "--", path, check=False)
    return result.stdout if result.returncode == 0 else ''


@dataclass
class GitChangeTracker:
    source_cwd: Path
    source_root: Path
    run_cwd: Path
    repo_root: Path
    work_tree: Path
    event_cwd: Path
    event_repo_root: Path
    base_tree: str
    persistent: bool
    msg_id: Optional[int] = None
    head_tree: Optional[str] = None

    @classmethod
    def prepare(
        cls,
        cwd: str,
        *,
        topic: str,
        agent: Optional[str],
        adhoc: bool,
        msg_id: Optional[int],
        source_cwd: Optional[str] = None,
        source_root: Optional[str] = None,
    ) -> Optional["GitChangeTracker"]:
        del topic, agent, adhoc
        run_cwd = Path(cwd).expanduser().resolve()

        event_cwd = Path(source_cwd).expanduser().resolve() if source_cwd else run_cwd
        event_root = Path(source_root).expanduser().resolve() if source_root else _repo_root(event_cwd)

        # run_cwd is a branchless per-turn directory (no .git of its own — see
        # ADR-0025) whenever source_root points elsewhere: git commands must
        # run against the real repo, snapshotting run_cwd's content via
        # GIT_WORK_TREE rather than requiring run_cwd itself to be a git repo.
        git_root = _repo_root(run_cwd) or event_root
        if not git_root:
            return None
        event_root = event_root or git_root

        base_tree = _snapshot_tree(git_root, run_cwd)
        return cls(
            source_cwd=event_cwd,
            source_root=event_root,
            run_cwd=run_cwd,
            repo_root=git_root,
            work_tree=run_cwd,
            event_cwd=event_cwd,
            event_repo_root=event_root,
            base_tree=base_tree,
            persistent=True,
            msg_id=msg_id,
        )

    def build_event(self) -> Optional[dict]:
        head_tree = _snapshot_tree(self.repo_root, self.work_tree)
        self.head_tree = head_tree
        name_status = _run_git(self.repo_root, "diff", "--name-status", self.base_tree, head_tree).stdout
        files = _parse_name_status(name_status)
        if not files:
            return None

        numstat = _run_git(self.repo_root, "diff", "--numstat", self.base_tree, head_tree).stdout
        additions, deletions = _parse_numstat(numstat)
        stat = _run_git(self.repo_root, "diff", "--stat", self.base_tree, head_tree).stdout
        full_diff = _run_git(self.repo_root, "diff", "--binary", "--unified=3", self.base_tree, head_tree).stdout
        diff, omitted_paths = _truncate_diff_by_file(full_diff, _MAX_DIFF_CHARS)
        truncated = bool(omitted_paths)
        if omitted_paths:
            _anchor_diff_trees(self.repo_root, self.msg_id, self.base_tree, head_tree)

        return {
            "name": "GitDiff",
            "file_count": len(files),
            "additions": additions,
            "deletions": deletions,
            "files": files,
            "stat": stat,
            "diff": diff,
            "base": self.base_tree,
            "head": head_tree,
            "cwd": str(self.event_cwd),
            "source": str(self.source_cwd),
            "repo": str(self.event_repo_root),
            "mode": "direct-watch",
            "persistent": self.persistent,
            "truncated": truncated,
            "omitted_paths": omitted_paths,
            **({
                "worktree_cwd": str(self.run_cwd),
                "worktree_repo": str(self.work_tree),
                "worktree_status": "pending",
            } if self.work_tree != self.event_repo_root else {}),
        }

    def build_no_change_event(self) -> dict:
        return {
            "name": "GitDiff",
            "file_count": 0,
            "additions": 0,
            "deletions": 0,
            "files": [],
            "stat": "",
            "diff": "",
            "base": self.base_tree,
            "head": self.head_tree or self.base_tree,
            "cwd": str(self.event_cwd),
            "source": str(self.source_cwd),
            "repo": str(self.event_repo_root),
            "mode": "direct-watch",
            "persistent": self.persistent,
            "truncated": False,
            "no_changes": True,
            **({
                "worktree_cwd": str(self.run_cwd),
                "worktree_repo": str(self.work_tree),
                "worktree_status": "pending",
            } if self.work_tree != self.event_repo_root else {}),
        }

    def cleanup(self) -> None:
        return None


def _iter_diff_chunks(full_diff: str):
    """Yield (path, chunk_text) for each file section of a unified diff, in order.

    chunk_text always ends with a trailing newline — git apply rejects a patch
    whose last line has none ("corrupt patch"), which a naive line-boundary
    split can otherwise produce at the last chunk or a truncation cut point.
    """
    current_path: Optional[str] = None
    current_lines: list[str] = []

    def _chunk() -> str:
        text = '\n'.join(current_lines)
        return text if text.endswith('\n') else text + '\n'

    for line in full_diff.split('\n'):
        if line.startswith('diff --git '):
            if current_lines:
                yield current_path, _chunk()
            m = re.match(r'^diff --git a/.+ b/(.+)$', line)
            current_path = m.group(1) if m else None
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)

    if current_lines:
        yield current_path, _chunk()


def extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the unified diff chunk for a single file from a full diff."""
    for path, chunk in _iter_diff_chunks(full_diff):
        if path == file_path:
            return chunk
    return ''


def _truncate_diff_by_file(full_diff: str, max_chars: int) -> tuple[str, list[str]]:
    """Keep whole file chunks up to max_chars total — never cut mid-file/mid-hunk.

    Returns (kept_diff_text, omitted_paths). Once a chunk doesn't fit, it and
    every chunk after it (in original order) are omitted, so kept_diff_text is
    always a clean prefix of full_diff and every included file's diff is
    complete and independently appliable (e.g. for single-file revert).
    """
    if len(full_diff) <= max_chars:
        return full_diff, []
    kept: list[str] = []
    omitted: list[str] = []
    budget = max_chars
    cutoff = False
    for path, chunk in _iter_diff_chunks(full_diff):
        if not cutoff and len(chunk) <= budget:
            kept.append(chunk)
            budget -= len(chunk)
        else:
            cutoff = True
            if path:
                omitted.append(path)
    return ''.join(kept), omitted


def apply_reverse_patch(repo_root: Path, patch_text: str) -> tuple[bool, str]:
    """Apply the reverse of patch_text in repo_root. Returns (success, error_message)."""
    fd, patch_path = tempfile.mkstemp(suffix='.patch')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(patch_text)
        check = _run_git(repo_root, 'apply', '--reverse', '--check', patch_path, check=False)
        if check.returncode != 0:
            return False, check.stderr.strip()
        result = _run_git(repo_root, 'apply', '--reverse', patch_path, check=False)
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, ''
    finally:
        try:
            os.unlink(patch_path)
        except FileNotFoundError:
            pass


def prepare_tracker(*args, **kwargs) -> Optional[GitChangeTracker]:
    try:
        return GitChangeTracker.prepare(*args, **kwargs)
    except Exception as exc:
        log.warning("git change tracking disabled: %s", exc)
        return None


def _source_for_worktree_path(path: Path, worktree_sources: dict[Path, Path]) -> tuple[Optional[Path], Optional[Path]]:
    for worktree_root, source_root in worktree_sources.items():
        try:
            rel = path.relative_to(worktree_root)
        except ValueError:
            continue
        return source_root / rel, source_root
    return None, None


def prepare_trackers(
    roots: list[str],
    *,
    worktree_sources: Optional[dict[str, str]] = None,
    **kwargs,
) -> list[GitChangeTracker]:
    trackers: list[GitChangeTracker] = []
    seen: set[Path] = set()
    resolved_worktree_sources = {
        Path(wt).expanduser().resolve(): Path(src).expanduser().resolve()
        for wt, src in (worktree_sources or {}).items()
    }
    for root in roots:
        try:
            source = Path(root).expanduser().resolve()
        except OSError as exc:
            log.warning("git change tracking skipped invalid root %r: %s", root, exc)
            continue
        repo_root = _repo_root(source)
        dedupe_key = repo_root or source
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        source_cwd, source_root = _source_for_worktree_path(source, resolved_worktree_sources)
        tracker = prepare_tracker(
            str(source),
            source_cwd=str(source_cwd) if source_cwd else None,
            source_root=str(source_root) if source_root else None,
            **kwargs,
        )
        if tracker:
            trackers.append(tracker)
    return trackers
