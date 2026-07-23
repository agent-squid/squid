"""
Tests for agent/worktree.py — per-turn Git worktree isolation.

Each test gets a fresh temp Git repo so there's no shared state.
"""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent import worktree as worktree_mod
from agent import stats_db
from agent.worktree import (
    _link_dependency_dirs,
    branch_name,
    cleanup_worktrees,
    ensure_worktree,
    promote_resolved_integration_worktree,
    remove_worktree,
    repo_root_for,
    settle_worktrees_before_turn,
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


def test_ensure_worktree_has_no_git(tmp_path):
    """Turn directory must not be a git repo — nothing for an agent to
    `git branch`/`push`/`gh pr create` against, regardless of what name it
    tries (ADR-0025, "Revised design")."""
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "102")
    assert not (wt / ".git").exists()
    br = branch_name("t", "102")
    result = subprocess.run(
        ["git", "branch", "--list", br], cwd=str(repo),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert br not in result.stdout


def test_ensure_worktree_prunes_temporary_git_worktree_registration(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "102b")

    result = git(repo, "worktree", "list", "--porcelain")

    assert str(wt) not in result.stdout
    assert not (wt / ".git").exists()


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


def test_ensure_worktree_defaults_to_clean_head_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_mod.config, "WORKTREE_TRACK_DIRTY_CHANGES", False)
    repo = init_repo(tmp_path / "repo")
    (repo / "file.txt").write_text("dirty source\n")
    (repo / "seed.txt").write_text("untracked seed\n")

    wt = ensure_worktree(repo, "t", "204")

    assert (wt / "file.txt").read_text() == "base\n"
    assert not (wt / "seed.txt").exists()


def test_ensure_worktree_tracks_dirty_code_root_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_mod.config, "WORKTREE_TRACK_DIRTY_CHANGES", True)
    repo = init_repo(tmp_path / "repo")
    (repo / "file.txt").write_text("dirty source\n")
    (repo / "seed.txt").write_text("untracked seed\n")

    wt = ensure_worktree(repo, "t", "205")

    assert (wt / "file.txt").read_text() == "dirty source\n"
    assert (wt / "seed.txt").read_text() == "untracked seed\n"


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


def test_ensure_worktree_auto_symlinks_only_allowlisted_ignored_dirs_not_files(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_mod.config, "DEPENDENCY_DIRS", ["tool-cache"])
    repo = init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("tool-cache/\ndist/\n.env\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore local tool state")
    tool_cache = repo / "tool-cache" / "bin"
    tool_cache.mkdir(parents=True)
    (tool_cache / "tool").write_text("#!/bin/sh\n")
    dist = repo / "dist"
    dist.mkdir()
    (dist / "app.js").write_text("built output\n")
    (repo / ".env").write_text("SECRET=1\n")

    wt = ensure_worktree(repo, "t", "212")

    assert (wt / "tool-cache").is_symlink()
    assert (wt / "tool-cache" / "bin" / "tool").exists()
    assert not (wt / "dist").exists()
    assert not (wt / ".env").exists()


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


def test_sync_after_turn_reuses_supplied_turn_tree_for_noop(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    ensure_worktree(repo, "t", "408")
    base_commit = worktree_mod.base_commit_for(repo, "t", "408")
    base_tree = git(repo, "show", "--format=%T", "--no-patch", base_commit).stdout.strip()

    def fail_snapshot_dir(*args, **kwargs):
        raise AssertionError("turn directory was snapshotted again")

    monkeypatch.setattr(worktree_mod, "_snapshot_dir", fail_snapshot_dir)

    conflicts = sync_after_turn(repo, "t", "408", msg_id=408, turn_tree=base_tree)

    assert conflicts == []


def test_sync_after_turn_promotes_without_source_commit(tmp_path):
    repo = init_repo(tmp_path / "repo")
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    ensure_worktree(repo, "t", "410")
    wt = worktree_path(repo, "t", "410")
    (wt / "new.txt").write_text("turn output\n")

    conflicts = sync_after_turn(
        repo, "t", "410", msg_id=410,
        request_text="please add new.txt",
        response_text="Added new.txt with the requested content.",
    )

    assert conflicts == []
    assert (repo / "new.txt").read_text() == "turn output\n"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_sync_after_turn_seeds_from_dirty_code_root_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_mod.config, "WORKTREE_TRACK_DIRTY_CHANGES", True)
    repo = init_repo(tmp_path / "repo")
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "file.txt").write_text("dirty source\n")
    (repo / "seed.txt").write_text("untracked seed\n")

    wt = ensure_worktree(repo, "t", "411")

    assert (wt / "file.txt").read_text() == "dirty source\n"
    assert (wt / "seed.txt").read_text() == "untracked seed\n"

    (wt / "turn.txt").write_text("turn output\n")
    conflicts = sync_after_turn(repo, "t", "411", msg_id=411)

    assert conflicts == []
    assert (repo / "file.txt").read_text() == "dirty source\n"
    assert (repo / "seed.txt").read_text() == "untracked seed\n"
    assert (repo / "turn.txt").read_text() == "turn output\n"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_sync_after_turn_preserves_dirty_source_when_tracking_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_mod.config, "WORKTREE_TRACK_DIRTY_CHANGES", False)
    repo = init_repo(tmp_path / "repo")
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "file.txt").write_text("dirty source\n")
    (repo / "seed.txt").write_text("untracked seed\n")

    wt = ensure_worktree(repo, "t", "412")

    assert (wt / "file.txt").read_text() == "base\n"
    assert not (wt / "seed.txt").exists()

    (wt / "turn.txt").write_text("turn output\n")
    conflicts = sync_after_turn(repo, "t", "412", msg_id=412)

    assert conflicts == []
    assert (repo / "file.txt").read_text() == "dirty source\n"
    assert (repo / "seed.txt").read_text() == "untracked seed\n"
    assert (repo / "turn.txt").read_text() == "turn output\n"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_sync_after_turn_rolls_back_partial_git_apply_failure(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "413")
    (wt / "file.txt").write_text("turn output\n")

    original_run_git = worktree_mod._run_git
    failed_once = False

    def fake_run_git(cwd: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
        nonlocal failed_once
        if cwd == repo and args == ("apply",) and not failed_once:
            failed_once = True
            (repo / "file.txt").write_text("partially applied\n")
            return subprocess.CompletedProcess(["git", "apply"], 128, "", "fatal: simulated apply failure")
        return original_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(worktree_mod, "_run_git", fake_run_git)

    with pytest.raises(RuntimeError, match="rolled back partial changes"):
        sync_after_turn(repo, "t", "413", msg_id=413)

    assert failed_once
    assert (repo / "file.txt").read_text() == "base\n"


def test_sync_after_turn_excludes_stale_external_dependency_symlinks(tmp_path):
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "t", "414")
    dep_cache = repo / "agent" / "__pycache__"
    dep_cache.mkdir(parents=True)
    (dep_cache / "cache.pyc").write_text("cache\n")

    stale_link = wt / "agent" / "__pycache__"
    stale_link.parent.mkdir(parents=True, exist_ok=True)
    stale_link.symlink_to(dep_cache, target_is_directory=True)
    (wt / "new.txt").write_text("turn output\n")

    conflicts = sync_after_turn(repo, "t", "414", msg_id=414)

    assert conflicts == []
    assert (repo / "new.txt").read_text() == "turn output\n"
    assert git(repo, "ls-files", "--", "agent/__pycache__").stdout == ""


def test_sync_after_turn_returns_conflicts_on_same_line_edit(tmp_path):
    repo = init_repo(tmp_path / "repo")

    # Two turns start from the same seed, then both edit line 1.
    ensure_worktree(repo, "topic", "501")
    wt_a = worktree_path(repo, "topic", "501")
    ensure_worktree(repo, "topic", "502")
    wt_b = worktree_path(repo, "topic", "502")

    (wt_a / "file.txt").write_text("edit-A\n")
    sync_after_turn(repo, "topic", "501", msg_id=501)
    (wt_b / "file.txt").write_text("edit-B\n")

    conflicts = sync_after_turn(repo, "topic", "502", msg_id=502)
    assert "file.txt" in conflicts


def test_promote_resolved_integration_worktree_applies_manual_resolution(tmp_path):
    repo = init_repo(tmp_path / "repo")

    ensure_worktree(repo, "topic", "511")
    wt_a = worktree_path(repo, "topic", "511")
    ensure_worktree(repo, "topic", "512")
    wt_b = worktree_path(repo, "topic", "512")

    (wt_a / "file.txt").write_text("edit-A\n")
    sync_after_turn(repo, "topic", "511", msg_id=511)
    (wt_b / "file.txt").write_text("edit-B\n")

    conflicts = sync_after_turn(repo, "topic", "512", msg_id=512)
    assert conflicts == ["file.txt"]

    integration = worktree_mod.integration_worktree_path(repo, "topic", "512")
    (integration / "file.txt").write_text("resolved\n")

    promote_resolved_integration_worktree(repo, "topic", "512")

    assert (repo / "file.txt").read_text() == "resolved\n"
    assert not integration.exists()
    assert not wt_b.exists()


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


def test_cleanup_worktrees_marks_conflicts_and_skips_later(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    monkeypatch.setattr("agent.worktree._CLEANUP_GRACE_SECONDS", 0)
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "sweeptopic", "804")
    stats_db.save_worktree("sweeptopic", "804", str(repo), str(wt), branch_name("sweeptopic", "804"))
    (repo / "file.txt").write_text("source edit\n")
    (wt / "file.txt").write_text("turn edit\n")

    conflicts = asyncio.run(cleanup_worktrees("sweeptopic"))

    assert conflicts == {str(repo): ["file.txt"]}
    rec = stats_db.get_worktrees("sweeptopic", "804")[0]
    assert rec["status"] == "conflict"

    def fail_sync(*args, **kwargs):
        raise AssertionError("conflicted worktree should be skipped")

    monkeypatch.setattr(worktree_mod, "sync_after_turn", fail_sync)
    assert asyncio.run(cleanup_worktrees("sweeptopic")) == {}


def test_cleanup_worktrees_marks_promotion_failures_and_skips_later(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    monkeypatch.setattr("agent.worktree._CLEANUP_GRACE_SECONDS", 0)
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "sweeptopic", "805")
    stats_db.save_worktree("sweeptopic", "805", str(repo), str(wt), branch_name("sweeptopic", "805"))

    calls = 0

    def fail_sync(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated promotion failure")

    monkeypatch.setattr(worktree_mod, "sync_after_turn", fail_sync)

    assert asyncio.run(cleanup_worktrees("sweeptopic")) == {}
    rec = stats_db.get_worktrees("sweeptopic", "805")[0]
    assert rec["status"] == "promotion_failed"

    assert asyncio.run(cleanup_worktrees("sweeptopic")) == {}
    assert calls == 1


# ---------------------------------------------------------------------------
# admission settlement
# ---------------------------------------------------------------------------

def test_settle_worktrees_before_turn_promotes_pending_without_grace(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    monkeypatch.setattr("agent.worktree._CLEANUP_GRACE_SECONDS", 3600)
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "admit", "901")
    stats_db.save_worktree("admit", "901", str(repo), str(wt), branch_name("admit", "901"))
    (wt / "turn.txt").write_text("ready\n")

    blockers = asyncio.run(settle_worktrees_before_turn("admit", [repo]))

    assert blockers == []
    assert (repo / "turn.txt").read_text() == "ready\n"
    assert not wt.exists()
    assert stats_db.get_worktrees("admit", "901") == []


def test_settle_worktrees_before_turn_skips_active_prior_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: {902})
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "admit", "902")
    stats_db.save_worktree("admit", "902", str(repo), str(wt), branch_name("admit", "902"))
    (wt / "turn.txt").write_text("still running\n")

    blockers = asyncio.run(settle_worktrees_before_turn("admit", [repo]))

    assert blockers == []
    assert not (repo / "turn.txt").exists()
    assert wt.exists()
    assert stats_db.get_worktrees("admit", "902")


def test_settle_worktrees_before_turn_surfaces_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runners.get_active_msg_ids", lambda: set())
    stats_db.init_db()
    repo = init_repo(tmp_path / "repo")
    wt = ensure_worktree(repo, "admit", "903")
    stats_db.save_worktree("admit", "903", str(repo), str(wt), branch_name("admit", "903"))
    (repo / "file.txt").write_text("source edit\n")
    (wt / "file.txt").write_text("turn edit\n")

    blockers = asyncio.run(settle_worktrees_before_turn("admit", [repo]))

    assert blockers
    assert blockers[0]["status"] == "conflict"
    assert blockers[0]["conflicts"] == ["file.txt"]
    assert stats_db.get_worktrees("admit", "903")[0]["status"] == "conflict"


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
