"""
git_changes.py - Git-backed direct watch tracking for topic code roots.
"""
from __future__ import annotations

import codecs
import io
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

log = logging.getLogger(__name__)

MAX_INLINE_DIFF_BYTES = 64 * 1024


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


def _read_nul_diff(repo_root: Path, option: str, base: str, head: str) -> str:
    # Text-mode subprocess I/O normalizes CR in literal Git filenames.
    result = subprocess.run(
        ["git", "diff", option, "-z", base, head],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return result.stdout.decode("utf-8")


def _parse_name_status(output: str) -> list[dict]:
    files: list[dict] = []
    entries = iter(output.rstrip("\0").split("\0")) if output else iter(())
    for status in entries:
        path = next(entries)
        file = {"status": status, "path": path}
        if status.startswith(("R", "C")):
            file["old_path"] = path
            file["path"] = next(entries)
        files.append(file)
    return files


def _apply_numstat(files: list[dict], output: str) -> tuple[int, int]:
    stats = {}
    entries = iter(output.rstrip("\0").split("\0")) if output else iter(())
    for entry in entries:
        add, delete, path = entry.split("\t", 2)
        old_path = None
        if not path:
            old_path, path = next(entries), next(entries)
        binary = add == "-"
        stats[(old_path, path)] = {
            "binary": binary,
            "additions": 0 if binary else int(add),
            "deletions": 0 if binary else int(delete),
        }
    for file in files:
        file.update(stats.get((file.get("old_path"), file["path"]), {}))
    return (
        sum(file.get("additions", 0) for file in files),
        sum(file.get("deletions", 0) for file in files),
    )


def _patch_args(base: str, head: str, *paths: str) -> list[str]:
    return [
        "git", "--literal-pathspecs", "diff", "--binary", "--no-color", "--no-textconv", "--no-ext-diff",
        "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", base, head, "--", *paths,
    ]


def _read_inline_diff(repo_root: Path, base: str, head: str) -> Optional[str]:
    args = _patch_args(base, head)
    with subprocess.Popen(args, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        patch = proc.stdout.read(MAX_INLINE_DIFF_BYTES + 1)
        if len(patch) > MAX_INLINE_DIFF_BYTES:
            proc.kill()
            return None
        if proc.wait():
            raise subprocess.CalledProcessError(proc.returncode, args)
        try:
            return patch.decode("utf-8")
        except UnicodeDecodeError:
            # The display decoder must never become the source of a revert.
            return None


def _diff_anchor_ref(msg_id: int) -> str:
    return f"refs/squid/diffs/{msg_id}"


def _anchor_diff_trees(repo_root: Path, msg_id: Optional[int], base_tree: str, head_tree: str) -> bool:
    """Keep the historical trees reachable for on-demand patches."""
    if msg_id is None:
        return False
    try:
        from .worktree import _commit_tree

        base_commit = _commit_tree(repo_root, base_tree, f"squid: diff base for turn {msg_id}")
        head_commit = _commit_tree(repo_root, head_tree, f"squid: diff head for turn {msg_id}", [base_commit])
        return _run_git(repo_root, "update-ref", _diff_anchor_ref(msg_id), head_commit, check=False).returncode == 0
    except Exception:
        log.debug("diff anchor ref failed for msg_id=%s", msg_id, exc_info=True)
        return False


def diff_bytes_for_path(repo_root: Path, base: str, head: str, path: str, old_path: Optional[str]) -> bytes:
    paths = [old_path, path] if old_path else [path]
    result = subprocess.run(
        _patch_args(base, head, *paths), cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else b""


def diff_for_path(
    repo_root: Path, base: str, head: str, path: str, old_path: Optional[str] = None,
    *, offset: int = 0, limit: Optional[int] = None,
    observe_prefix: Optional[Callable[[str], None]] = None,
) -> str:
    """Read a historical file patch, optionally stopping after a page."""
    paths = [old_path, path] if old_path else [path]
    args = _patch_args(base, head, *paths)
    with subprocess.Popen(args, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        with io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="") as output:
            while offset:
                skipped = output.read(min(offset, MAX_INLINE_DIFF_BYTES))
                if not skipped:
                    return ""
                if observe_prefix is not None:
                    observe_prefix(skipped)
                offset -= len(skipped)
            patch = output.read() if limit is None else output.read(limit + 1)
            if limit is not None and len(patch) > limit:
                proc.kill()
                return patch
            return patch if proc.wait() == 0 else ""


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
        name_status = _read_nul_diff(self.repo_root, "--name-status", self.base_tree, head_tree)
        files = _parse_name_status(name_status)
        if not files:
            return None

        numstat = _read_nul_diff(self.repo_root, "--numstat", self.base_tree, head_tree)
        additions, deletions = _apply_numstat(files, numstat)
        stat = _run_git(self.repo_root, "diff", "--no-color", "--stat", self.base_tree, head_tree).stdout
        diff = _read_inline_diff(self.repo_root, self.base_tree, head_tree)
        deferred = diff is None
        if deferred and not _anchor_diff_trees(self.repo_root, self.msg_id, self.base_tree, head_tree):
            # Preserve the prior stored-patch behavior if we cannot pin the trees.
            args = _patch_args(self.base_tree, head_tree)
            full_patch = subprocess.run(args, cwd=self.repo_root, stdout=subprocess.PIPE, check=True).stdout
            try:
                diff, omitted_paths = _truncate_diff_by_file(full_patch.decode("utf-8"), MAX_INLINE_DIFF_BYTES)
                deferred = False
            except UnicodeDecodeError:
                # Without the anchor the trees can be pruned, but using a
                # lossy stored patch would make a successful revert unsafe.
                omitted_paths = []
        else:
            omitted_paths = []

        return {
            "name": "GitDiff",
            "file_count": len(files),
            "additions": additions,
            "deletions": deletions,
            "files": files,
            "stat": stat,
            "diff": diff or "",
            **({"diff_deferred": True} if deferred else {"diff_lossless": True}),
            "base": self.base_tree,
            "head": head_tree,
            "cwd": str(self.event_cwd),
            "source": str(self.source_cwd),
            "repo": str(self.event_repo_root),
            "mode": "direct-watch",
            "persistent": self.persistent,
            "truncated": bool(omitted_paths),
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


def unquote_git_path(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        decoded, _ = codecs.escape_decode(path[1:-1].encode("utf-8"))
        return decoded.decode("utf-8", errors="replace")
    return path


def _iter_diff_chunks(full_diff: str):
    """Yield a path and a complete, independently appliable patch per file."""
    current_path: Optional[str] = None
    current_lines: list[str] = []
    in_hunk = False

    def _chunk() -> str:
        text = '\n'.join(current_lines)
        return text if text.endswith('\n') else text + '\n'

    for line in full_diff.split('\n'):
        if line.startswith('diff --git '):
            if current_lines:
                yield current_path, _chunk()
            match = re.match(r'^diff --git .+ ("b/.*"|b/.*)$', line)
            current_path = unquote_git_path(match.group(1))[2:] if match else None
            current_lines = [line]
            in_hunk = False
        elif current_lines:
            current_lines.append(line)
            if line.startswith('@@'):
                in_hunk = True
            if not in_hunk and line.startswith(('rename to ', 'copy to ')):
                current_path = unquote_git_path(line.split(' to ', 1)[1])
            if not in_hunk and line.startswith(('--- ', '+++ ')):
                path = unquote_git_path(line[4:].removesuffix('\t'))
                if path.startswith(('a/', 'b/')):
                    current_path = path[2:]

    if current_lines:
        yield current_path, _chunk()


def extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the unified diff chunk for a single file from a full diff."""
    for path, chunk in _iter_diff_chunks(full_diff):
        if path == file_path:
            return chunk
    return ''


def summarize_stored_diff(tool: dict) -> bool:
    """Keep large legacy patches in storage and send a small history summary."""
    patch = tool.get("diff") or ""
    files = tool.get("files") or []
    if not patch:
        return False
    chunks = dict(_iter_diff_chunks(patch))
    for file in files:
        chunk = chunks.get(unquote_git_path(file.get("path", "")), "")
        if not chunk:
            continue
        binary = "\nGIT binary patch\n" in chunk or "\nBinary files " in chunk
        hunk = chunk.partition("\n@@")[2]
        file.setdefault("binary", binary)
        file.setdefault("additions", sum(line.startswith("+") for line in hunk.splitlines()))
        file.setdefault("deletions", sum(line.startswith("-") for line in hunk.splitlines()))
    tool["diff"] = ""
    tool["diff_deferred"] = True
    return True


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


def apply_reverse_patch(repo_root: Path, patch: Union[str, bytes]) -> tuple[bool, str]:
    """Apply the reverse of a lossless patch. Returns (success, error_message)."""
    fd, patch_path = tempfile.mkstemp(suffix='.patch')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(patch.encode('utf-8') if isinstance(patch, str) else patch)
        args = ["git", "apply", "--reverse", "--whitespace=nowarn"]
        for options in (["--check"], []):
            result = subprocess.run(
                [*args, *options, patch_path], cwd=repo_root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                return False, result.stderr.decode("utf-8", errors="replace").strip()
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
