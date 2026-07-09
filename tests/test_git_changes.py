import subprocess

from agent.git_changes import apply_reverse_patch, prepare_tracker, prepare_trackers


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def init_repo(path):
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "app.txt").write_text("base\n")
    git(path, "add", "app.txt")
    git(path, "commit", "-m", "base")


def test_git_tracker_uses_dirty_source_state_as_baseline(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "app.txt").write_text("dirty before run\n")
    (tmp_path / "notes.txt").write_text("untracked before run\n")

    tracker = prepare_tracker(
        str(tmp_path),
        topic="work",
        agent="codex",
        adhoc=True,
        msg_id=9001,
    )

    assert tracker is not None
    assert tracker.run_cwd == tmp_path
    assert tracker.repo_root == tmp_path
    assert (tracker.run_cwd / "app.txt").read_text() == "dirty before run\n"
    assert (tracker.run_cwd / "notes.txt").read_text() == "untracked before run\n"

    (tracker.run_cwd / "app.txt").write_text("changed by agent\n")
    (tracker.run_cwd / "new.txt").write_text("new by agent\n")

    event = tracker.build_event()

    assert event["name"] == "GitDiff"
    assert event["file_count"] == 2
    assert {f["path"] for f in event["files"]} == {"app.txt", "new.txt"}
    assert "-dirty before run" in event["diff"]
    assert "+changed by agent" in event["diff"]
    assert "untracked before run" not in event["diff"]


def test_direct_tracker_snapshots_current_source_state_between_turns(tmp_path):
    init_repo(tmp_path)

    first = prepare_tracker(
        str(tmp_path),
        topic="direct-refresh",
        agent="codex",
        adhoc=False,
        msg_id=1,
    )
    assert first is not None
    (first.run_cwd / "app.txt").write_text("changed in first run\n")
    assert first.build_event() is not None

    (tmp_path / "app.txt").write_text("dirty before second run\n")
    (tmp_path / "notes.txt").write_text("untracked before second run\n")

    second = prepare_tracker(
        str(tmp_path),
        topic="persistent-refresh",
        agent="codex",
        adhoc=False,
        msg_id=2,
    )

    assert second is not None
    assert second.repo_root == first.repo_root
    assert (second.run_cwd / "app.txt").read_text() == "dirty before second run\n"
    assert (second.run_cwd / "notes.txt").read_text() == "untracked before second run\n"

    (second.run_cwd / "app.txt").write_text("changed in second run\n")
    event = second.build_event()

    assert event is not None
    assert "-dirty before second run" in event["diff"]
    assert "+changed in second run" in event["diff"]
    assert "changed in first run" not in event["diff"]


def test_adhoc_tracker_cleanup_keeps_direct_watch_root(tmp_path):
    init_repo(tmp_path)
    tracker = prepare_tracker(
        str(tmp_path),
        topic="adhoc-cleanup",
        agent="codex",
        adhoc=True,
        msg_id=3,
    )
    assert tracker is not None
    repo = tracker.repo_root
    assert repo.exists()

    tracker.cleanup()

    assert repo.exists()


def test_worktree_tracker_emits_durable_repo_for_revert(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-b", "sqd-test", str(wt), "HEAD")

    trackers = prepare_trackers(
        [str(wt)],
        topic="worktree-revert",
        agent="codex",
        adhoc=False,
        msg_id=42,
        worktree_sources={str(wt): str(repo)},
    )

    assert len(trackers) == 1
    tracker = trackers[0]
    assert tracker.repo_root == wt
    assert tracker.event_repo_root == repo

    (wt / "app.txt").write_text("changed in worktree\n")
    event = tracker.build_event()

    assert event is not None
    assert event["repo"] == str(repo)
    assert event["cwd"] == str(repo)
    assert event["worktree_repo"] == str(wt)
    assert event["worktree_cwd"] == str(wt)
    assert "-base" in event["diff"]
    assert "+changed in worktree" in event["diff"]

    git(wt, "add", "app.txt")
    git(wt, "commit", "-m", "worktree change")
    git(repo, "merge", "--no-ff", "sqd-test")
    ok, err = apply_reverse_patch(repo, event["diff"])

    assert ok, err
    assert (repo / "app.txt").read_text() == "base\n"
