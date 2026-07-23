"""
publish.py - Squid-owned commit/push for promoted code-root changes.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .worktree import repo_root_for


class PublishError(RuntimeError):
    status_code = 400


class PublishBlocked(PublishError):
    status_code = 409


@dataclass
class PendingPublish:
    message: Optional[str] = None
    tag: Optional[str] = None


_pending_publish_lock = threading.Lock()
_pending_publish: dict[tuple[str, int], PendingPublish] = {}


def defer_publish(topic: str, msg_id: int, *, message: Optional[str] = None, tag: Optional[str] = None) -> None:
    with _pending_publish_lock:
        _pending_publish[(topic, msg_id)] = PendingPublish(message=message, tag=tag)


def pop_deferred_publish(topic: str, msg_id: int) -> Optional[PendingPublish]:
    with _pending_publish_lock:
        return _pending_publish.pop((topic, msg_id), None)


@dataclass
class PublishedRepo:
    repo_root: str
    branch: str
    commit: str
    files: list[str]
    pushed: bool
    tag: Optional[str] = None
    tag_pushed: bool = False


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _default_message(files: list[str]) -> str:
    if not files:
        return "squid: publish changes"
    if len(files) == 1:
        return f"squid: update {files[0]}"
    return f"squid: update {len(files)} files"


def _clean_message(message: Optional[str], files: list[str]) -> str:
    text = (message or "").strip()
    return text or _default_message(files)


def _clean_tag(repo_root: Path, tag: Optional[str]) -> Optional[str]:
    text = (tag or "").strip()
    if not text:
        return None
    result = _run_git(repo_root, "check-ref-format", f"refs/tags/{text}", check=False)
    if result.returncode != 0:
        raise PublishError(f"invalid tag name: {text}")
    return text


def _repo_pathspec(repo_root: Path, root: str) -> str:
    path = Path(root).expanduser().resolve()
    try:
        rel = path.relative_to(repo_root)
    except ValueError as exc:
        raise PublishError(f"code root is outside repo: {root}") from exc
    return "." if str(rel) == "." else str(rel)


def _changed_files(repo_root: Path, pathspecs: list[str]) -> list[str]:
    result = _run_git(
        repo_root,
        "status", "--porcelain", "--untracked-files=all", "--", *pathspecs,
        check=False,
    )
    if result.returncode != 0:
        raise PublishError(result.stderr.strip() or "git status failed")
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def _ensure_publishable_worktrees(topic: str) -> None:
    from .stats_db import get_all_worktrees_for_topic

    blockers = [
        rec for rec in get_all_worktrees_for_topic(topic)
        if rec.get("status") != "synced"
    ]
    if blockers:
        repos = ", ".join(sorted({rec["repo_root"] for rec in blockers}))
        raise PublishBlocked(f"worktree sync is not complete for: {repos}")


def publish_code_roots(
    topic: str,
    code_roots: list[str],
    *,
    message: Optional[str] = None,
    tag: Optional[str] = None,
    remote: str = "origin",
    push: bool = True,
) -> list[PublishedRepo]:
    """
    Commit and push dirty changes under topic code roots from the real repos.

    This function is intentionally narrow: callers provide topic/code-root
    identity, not arbitrary shell commands. It refuses unresolved worktree syncs
    and pre-existing staged changes so it does not mix Squid publication with a
    user's manual index state.
    """
    roots = [root for root in code_roots if root.strip()]
    if not roots:
        raise PublishError("no code roots configured for this topic")

    _ensure_publishable_worktrees(topic)

    repos: dict[Path, list[str]] = {}
    for root in roots:
        repo_root = repo_root_for(root)
        if not repo_root:
            raise PublishError(f"code root is not inside a git repo: {root}")
        repos.setdefault(repo_root, []).append(_repo_pathspec(repo_root, root))

    published: list[PublishedRepo] = []
    for repo_root, pathspecs in repos.items():
        clean_tag = _clean_tag(repo_root, tag)
        unmerged = _run_git(repo_root, "diff", "--name-only", "--diff-filter=U", check=False)
        if unmerged.stdout.strip():
            raise PublishBlocked(f"repo has unresolved merge conflicts: {repo_root}")

        staged = _run_git(repo_root, "diff", "--cached", "--name-only", check=False)
        if staged.stdout.strip():
            raise PublishBlocked(f"repo has pre-existing staged changes: {repo_root}")

        files = _changed_files(repo_root, pathspecs)
        if not files:
            continue

        branch = _run_git(repo_root, "branch", "--show-current").stdout.strip()
        if not branch:
            raise PublishBlocked(f"repo is detached; cannot push current branch: {repo_root}")

        add = _run_git(repo_root, "add", "-A", "--", *pathspecs, check=False)
        if add.returncode != 0:
            raise PublishError(add.stderr.strip() or "git add failed")

        commit_message = _clean_message(message, files)
        commit = _run_git(repo_root, "commit", "-m", commit_message, check=False)
        if commit.returncode != 0:
            raise PublishError(commit.stderr.strip() or "git commit failed")

        commit_id = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
        pushed = False
        if push:
            pushed_result = _run_git(repo_root, "push", remote, branch, check=False)
            if pushed_result.returncode != 0:
                raise PublishError(pushed_result.stderr.strip() or "git push failed")
            pushed = True

        tag_pushed = False
        if clean_tag:
            tag_result = _run_git(repo_root, "tag", "-f", clean_tag, commit_id, check=False)
            if tag_result.returncode != 0:
                raise PublishError(tag_result.stderr.strip() or "git tag failed")
            if push:
                push_tag = _run_git(
                    repo_root, "push", remote,
                    f"+refs/tags/{clean_tag}:refs/tags/{clean_tag}",
                    check=False,
                )
                if push_tag.returncode != 0:
                    raise PublishError(push_tag.stderr.strip() or "git tag push failed")
                tag_pushed = True

        published.append(PublishedRepo(
            repo_root=str(repo_root),
            branch=branch,
            commit=commit_id,
            files=files,
            pushed=pushed,
            tag=clean_tag,
            tag_pushed=tag_pushed,
        ))

    if not published:
        raise PublishError("no changes to publish")
    return published
