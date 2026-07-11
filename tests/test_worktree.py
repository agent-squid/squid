"""
Tests for agent/worktree.py — per-turn Git worktree isolation.

Each test gets a fresh temp Git repo so there's no shared state.
"""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent import stats_db
from agent.worktree import (
    _link_dependency_dirs,
    branch_name,
    cleanup_worktrees,
    commit_worktree,
    ensure_worktree,
    merge_worktree,
    remove_worktree,
    repo_root_for,
    sync_after_turn,
    worktree_path,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "file.txt").write_text("base\n")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "base")
    return path


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def test_branch_name_is_stable():
    assert branch_name("mytopic", "claude") == branch_name("mytopic", "claude")


def test_branch_name_has_sqd_prefix():
    assert branch_name("t", "a").startswith("sqd-")


def test_worktree_path_outside_repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = worktree_path(repo, "t", "42")
    assert str(repo) not in str(wt), "worktree must live outside the repo"


# ---------------------------------------------------------------------------
# repo_root_for
# ---------------------------------------------------------------------------

def test_repo_root_for_returns_root(tmp_path):
    repo = init_repo(tmp_path / "repo")
    assert repo_root_for(str(repo)) == repo


def test_repo_root_for_returns_none_for_non_git(tmp_path):
    assert repo_root_for(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# ensure_worktree
# ---------------------------------------------------------------------------

def test_ensure_worktree_creates_directory(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "101")
    assert wt.exists()
    assert (wt / "file.txt").exists()


def test_ensure_worktree_creates_branch(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "102")
    br = branch_name("t", "102")
    result = subprocess.run(
        ["git", "branch", "--list", br], cwd=str(repo),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert br in result.stdout


def test_ensure_worktree_reuses_existing(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt1 = ensure_worktree(repo, "t", "103")
    wt2 = ensure_worktree(repo, "t", "103")
    assert wt1 == wt2


def test_ensure_worktree_per_msg_id_are_independent(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt_a = ensure_worktree(repo, "t", "201")
    wt_b = ensure_worktree(repo, "t", "202")
    assert wt_a != wt_b
    assert wt_a.exists()
    assert wt_b.exists()


def test_ensure_worktree_symlinks_dependency_dirs(tmp_path):
    repo = init_repo(tmp_path / "repo")
    node_modules = repo / "node_modules" / "some-pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = {};\n")
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")

    wt = ensure_worktree(repo, "t", "210")

    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules" / "some-pkg" / "index.js").exists()
    assert (wt / ".venv").is_symlink()
    assert (wt / ".venv" / "bin" / "python").exists()


def test_ensure_worktree_playwright_cli_runs_through_nested_symlink(tmp_path):
    """
    Mirrors tests/e2e/node_modules' real layout: npm links node_modules/.bin/<cli>
    as a relative symlink into node_modules/<pkg>/. Verifies that chain still
    resolves, stays executable, and actually runs when reached through the
    repo->worktree node_modules symlink (not just that the path exists).
    """
    repo = init_repo(tmp_path / "repo")
    e2e = repo / "tests" / "e2e"
    pkg_dir = e2e / "node_modules" / "playwright"
    pkg_dir.mkdir(parents=True)
    cli = pkg_dir / "cli.js"
    cli.write_text("#!/bin/sh\necho Version 1.99.0\n")
    cli.chmod(0o755)

    bin_dir = e2e / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "playwright").symlink_to(Path("..") / "playwright" / "cli.js")

    wt = ensure_worktree(repo, "t", "801")

    linked_cli = wt / "tests" / "e2e" / "node_modules" / ".bin" / "playwright"
    assert os.access(linked_cli, os.X_OK)
    result = subprocess.run(
        [str(linked_cli)], text=True, stdout=subprocess.PIPE, check=True,
    )
    assert result.stdout.strip() == "Version 1.99.0"


def test_link_dependency_dirs_refuses_when_wt_equals_repo_root(tmp_path):
    repo = init_repo(tmp_path / "repo")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.js").write_text("x\n")

    _link_dependency_dirs(repo, repo)  # wt == repo_root: must not touch anything

    assert not (repo / "node_modules").is_symlink()
    assert (repo / "node_modules" / "pkg.js").read_text() == "x\n"


def test_ensure_worktree_does_not_recurse_into_matched_dependency_dirs(tmp_path):
    repo = init_repo(tmp_path / "repo")
    nested = repo / "node_modules" / "some-pkg" / "node_modules" / "nested-dep"
    nested.mkdir(parents=True)

    wt = ensure_worktree(repo, "t", "211")

    # the outer node_modules is a symlink; the nested one under it is reached
    # through that symlink, not independently symlinked by the worktree scan
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules" / "some-pkg" / "node_modules" / "nested-dep").exists()


# ---------------------------------------------------------------------------
# commit_worktree
# ---------------------------------------------------------------------------

def test_commit_worktree_commits_changes(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "301")
    (wt / "new.txt").write_text("hello\n")
    committed = commit_worktree(wt, "squid: turn 301")
    assert committed
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(wt),
        text=True, stdout=subprocess.PIPE,
    ).stdout
    assert "squid: turn 301" in log


def test_commit_worktree_returns_false_when_nothing_to_commit(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "302")
    assert not commit_worktree(wt, "squid: turn 302")


# ---------------------------------------------------------------------------
# sync_after_turn
# ---------------------------------------------------------------------------

def test_sync_after_turn_commits_and_merges(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "401")
    wt = worktree_path(repo, "t", "401")
    (wt / "new.txt").write_text("turn output\n")

    conflicts = sync_after_turn(repo, "t", "401", msg_id=401)

    assert conflicts == []
    # change should now be on main
    assert (repo / "new.txt").exists()


def test_sync_after_turn_is_noop_when_no_worktree(tmp_path):
    repo = init_repo(tmp_path / "repo")
    conflicts = sync_after_turn(repo, "t", "999", msg_id=999)
    assert conflicts == []


def test_sync_after_turn_commit_includes_request_and_response(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "410")
    wt = worktree_path(repo, "t", "410")
    (wt / "new.txt").write_text("turn output\n")

    conflicts = sync_after_turn(
        repo, "t", "410", msg_id=410,
        request_text="please add new.txt",
        response_text="Added new.txt with the requested content.",
    )

    assert conflicts == []
    # HEAD is the --no-ff merge commit; the turn's own commit (with our
    # generated message) is its second parent.
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD^2"], cwd=str(repo),
        text=True, stdout=subprocess.PIPE,
    ).stdout
    assert "please add new.txt" in log
    assert "Added new.txt with the requested content." in log


def test_merge_worktree_raises_on_failure_without_conflict_markers(tmp_path):
    """A merge that fails to even run (e.g. lock contention) must not be
    mistaken for a clean merge — that would silently drop the turn's work."""
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "420")
    wt = worktree_path(repo, "t", "420")
    (wt / "new.txt").write_text("turn output\n")
    commit_worktree(wt, "squid: turn 420")

    lock_file = repo / ".git" / "index.lock"
    lock_file.write_text("")
    try:
        with pytest.raises(RuntimeError):
            merge_worktree(repo, "t", "420")
    finally:
        lock_file.unlink()


def test_sync_after_turn_returns_conflicts_on_same_line_edit(tmp_path):
    repo = init_repo(tmp_path / "repo")

    # session A edits line 1
    ensure_worktree(repo, "topic", "501")
    wt_a = worktree_path(repo, "topic", "501")
    (wt_a / "file.txt").write_text("edit-A\n")
    sync_after_turn(repo, "topic", "501", msg_id=501)

    # session B also edits the same line, based on the original HEAD (before A merged)
    # Simulate by checking out a fresh worktree from original HEAD isn't possible here;
    # instead we manufacture a conflicting state by creating a new branch that
    # diverges from the pre-A HEAD.
    br_b = branch_name("topic", "502")
    git(repo, "checkout", "-b", br_b, "HEAD~1")  # branch from before A's merge
    git(repo, "checkout", "main")  # back to main

    ensure_worktree(repo, "topic", "502")
    wt_b = worktree_path(repo, "topic", "502")
    (wt_b / "file.txt").write_text("edit-B\n")
    commit_worktree(wt_b, "squid: turn 502")

    conflicts = merge_worktree(repo, "topic", "502")
    assert "file.txt" in conflicts


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------

def test_remove_worktree_cleans_up(tmp_path):
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "601")
    wt = worktree_path(repo, "t", "601")
    assert wt.exists()

    remove_worktree(repo, "t", "601")

    assert not wt.exists()
    br = branch_name("t", "601")
    result = subprocess.run(
        ["git", "branch", "--list", br], cwd=str(repo),
        text=True, stdout=subprocess.PIPE,
    )
    assert br not in result.stdout


# ---------------------------------------------------------------------------
# cleanup_worktrees (deferred sweep)
# ---------------------------------------------------------------------------

def test_cleanup_worktrees_defers_removal_within_grace_period(tmp_path, monkeypatch):
    """A worktree synced moments ago is left alone — a background process the
    turn spawned may still be using it as cwd."""
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "sweeptopic", "801")
    wt = worktree_path(repo, "sweeptopic", "801")
    stats_db.save_worktree("sweeptopic", "801", str(repo), str(wt), branch_name("sweeptopic", "801"))

    conflicts = asyncio.run(cleanup_worktrees("sweeptopic"))

    assert conflicts == {}
    assert wt.exists()
    assert stats_db.get_worktrees("sweeptopic", "801")


def test_cleanup_worktrees_removes_once_grace_period_elapses(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    monkeypatch.setattr("agent.worktree._CLEANUP_GRACE_SECONDS", 0)
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "sweeptopic", "802")
    wt = worktree_path(repo, "sweeptopic", "802")
    stats_db.save_worktree("sweeptopic", "802", str(repo), str(wt), branch_name("sweeptopic", "802"))

    conflicts = asyncio.run(cleanup_worktrees("sweeptopic"))

    assert conflicts == {}
    assert not wt.exists()
    assert stats_db.get_worktrees("sweeptopic", "802") == []


def test_cleanup_worktrees_skips_worktree_with_active_msg_id(tmp_path, monkeypatch):
    """Even past the grace period, a worktree whose turn is still running must
    never be removed out from under it."""
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: {803})
    monkeypatch.setattr("agent.worktree._CLEANUP_GRACE_SECONDS", 0)
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "sweeptopic", "803")
    wt = worktree_path(repo, "sweeptopic", "803")
    stats_db.save_worktree("sweeptopic", "803", str(repo), str(wt), branch_name("sweeptopic", "803"))

    conflicts = asyncio.run(cleanup_worktrees("sweeptopic"))

    assert conflicts == {}
    assert wt.exists()
    assert stats_db.get_worktrees("sweeptopic", "803")


# ---------------------------------------------------------------------------
# full per-turn lifecycle
# ---------------------------------------------------------------------------

def test_full_per_turn_lifecycle(tmp_path):
    """Each turn uses a unique msg_id key, gets isolated, merged, then removed."""
    repo = init_repo(tmp_path / "repo")

    for msg_id in (701, 702, 703):
        key = str(msg_id)
        ensure_worktree(repo, "mytopic", key)
        wt = worktree_path(repo, "mytopic", key)
        (wt / f"turn_{msg_id}.txt").write_text(f"work from turn {msg_id}\n")
        conflicts = sync_after_turn(repo, "mytopic", key, msg_id=msg_id)
        assert conflicts == []
        remove_worktree(repo, "mytopic", key)
        assert not wt.exists()

    # All three turns' files should now be on main
    for msg_id in (701, 702, 703):
        assert (repo / f"turn_{msg_id}.txt").exists()
