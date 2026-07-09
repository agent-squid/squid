"""
Tests for agent/worktree.py — per-turn Git worktree isolation.

Each test gets a fresh temp Git repo so there's no shared state.
"""
import subprocess
from pathlib import Path

import pytest

from agent.worktree import (
    branch_name,
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
