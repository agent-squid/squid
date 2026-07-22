import subprocess
from pathlib import Path

import pytest

from agent import stats_db
from agent.publish import PublishBlocked, publish_code_roots
from agent.worktree import branch_name


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("base\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "base")
    branch = git(repo, "branch", "--show-current").stdout.strip()
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", branch)
    return repo, remote, branch


def test_publish_code_roots_commits_and_pushes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo, remote, branch = init_repo_with_remote(tmp_path)

    (repo / "file.txt").write_text("changed\n")

    published = publish_code_roots("squid", [str(repo)], message="publish test")

    assert len(published) == 1
    assert published[0].repo_root == str(repo.resolve())
    assert published[0].branch == branch
    assert published[0].files == ["file.txt"]
    assert published[0].pushed is True
    assert git(repo, "status", "--porcelain").stdout == ""
    assert git(remote, "log", "--format=%s", "-1", branch).stdout.strip() == "publish test"


def test_publish_code_roots_moves_and_pushes_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo, remote, branch = init_repo_with_remote(tmp_path)
    base_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "tag", "v0.1", base_commit)
    git(repo, "push", "origin", "v0.1")

    (repo / "file.txt").write_text("changed\n")

    published = publish_code_roots("squid", [str(repo)], message="publish test", tag="v0.1")

    assert len(published) == 1
    assert published[0].tag == "v0.1"
    assert published[0].tag_pushed is True
    assert published[0].commit != base_commit
    assert git(remote, "rev-parse", "refs/tags/v0.1").stdout.strip() == published[0].commit
    assert git(remote, "log", "--format=%s", "-1", branch).stdout.strip() == "publish test"


def test_publish_refuses_pending_worktree_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo, _remote, _branch = init_repo_with_remote(tmp_path)
    stats_db.save_worktree("squid", "123", str(repo), str(tmp_path / "wt"), branch_name("squid", "123"))

    (repo / "file.txt").write_text("changed\n")

    with pytest.raises(PublishBlocked, match="worktree sync is not complete"):
        publish_code_roots("squid", [str(repo)], message="publish test")
