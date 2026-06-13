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


def _snapshot_tree(repo_root: Path) -> str:
    fd, index_path = tempfile.mkstemp(prefix="agent-squid-index-")
    os.close(fd)
    os.unlink(index_path)
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        _run_git(repo_root, "read-tree", "HEAD", env=env)
        _run_git(repo_root, "add", "-A", "--", ".", env=env)
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


@dataclass
class GitChangeTracker:
    source_cwd: Path
    source_root: Path
    run_cwd: Path
    repo_root: Path
    base_tree: str
    persistent: bool

    @classmethod
    def prepare(
        cls,
        cwd: str,
        *,
        topic: str,
        agent: Optional[str],
        adhoc: bool,
        msg_id: Optional[int],
    ) -> Optional["GitChangeTracker"]:
        del topic, agent, adhoc, msg_id
        source_cwd = Path(cwd).expanduser().resolve()
        source_root = _repo_root(source_cwd)
        if not source_root:
            return None

        base_tree = _snapshot_tree(source_root)
        return cls(
            source_cwd=source_cwd,
            source_root=source_root,
            run_cwd=source_cwd,
            repo_root=source_root,
            base_tree=base_tree,
            persistent=True,
        )

    def build_event(self) -> Optional[dict]:
        head_tree = _snapshot_tree(self.repo_root)
        name_status = _run_git(self.repo_root, "diff", "--name-status", self.base_tree, head_tree).stdout
        files = _parse_name_status(name_status)
        if not files:
            return None

        numstat = _run_git(self.repo_root, "diff", "--numstat", self.base_tree, head_tree).stdout
        additions, deletions = _parse_numstat(numstat)
        stat = _run_git(self.repo_root, "diff", "--stat", self.base_tree, head_tree).stdout
        diff = _run_git(self.repo_root, "diff", "--binary", "--unified=3", self.base_tree, head_tree).stdout
        truncated = False
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]\n"
            truncated = True

        return {
            "name": "GitDiff",
            "file_count": len(files),
            "additions": additions,
            "deletions": deletions,
            "files": files,
            "stat": stat,
            "diff": diff,
            "base": self.base_tree,
            "cwd": str(self.run_cwd),
            "source": str(self.source_cwd),
            "repo": str(self.repo_root),
            "mode": "direct-watch",
            "persistent": self.persistent,
            "truncated": truncated,
        }

    def cleanup(self) -> None:
        return None


def extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the unified diff chunk for a single file from a full diff."""
    current_path: Optional[str] = None
    current_lines: list[str] = []

    for line in full_diff.split('\n'):
        if line.startswith('diff --git '):
            if current_path == file_path:
                return '\n'.join(current_lines)
            m = re.match(r'^diff --git a/.+ b/(.+)$', line)
            current_path = m.group(1) if m else None
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)

    if current_path == file_path:
        return '\n'.join(current_lines)
    return ''


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


def prepare_trackers(roots: list[str], **kwargs) -> list[GitChangeTracker]:
    trackers: list[GitChangeTracker] = []
    seen: set[Path] = set()
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
        tracker = prepare_tracker(str(source), **kwargs)
        if tracker:
            trackers.append(tracker)
    return trackers
