import subprocess

import agent.git_changes as git_changes
from agent.git_changes import (
    apply_reverse_patch,
    diff_for_path,
    extract_file_diff,
    prepare_tracker,
    prepare_trackers,
)


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
    wt.mkdir()
    (wt / "app.txt").write_text("base\n")

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
    assert tracker.repo_root == repo
    assert tracker.work_tree == wt
    assert tracker.event_repo_root == repo

    (wt / "app.txt").write_text("changed in worktree\n")
    event = tracker.build_event()

    assert event is not None
    assert event["repo"] == str(repo)
    assert event["cwd"] == str(repo)
    assert event["worktree_repo"] == str(wt)
    assert event["worktree_cwd"] == str(wt)
    assert event["worktree_status"] == "pending"
    assert "-base" in event["diff"]
    assert "+changed in worktree" in event["diff"]

    (repo / "app.txt").write_text("changed in worktree\n")
    ok, err = apply_reverse_patch(repo, event["diff"])

    assert ok, err
    assert (repo / "app.txt").read_text() == "base\n"


def test_extract_file_diff_mid_diff_chunk_reverse_applies(tmp_path):
    # A file whose chunk is followed by another `diff --git` used to be
    # extracted without a trailing newline, which git apply rejects as a
    # corrupt patch.
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    git(repo, "add", "a.txt", "b.txt")
    git(repo, "commit", "-m", "two files")

    (repo / "a.txt").write_text("one changed\n")
    (repo / "b.txt").write_text("two changed\n")
    full_diff = git(repo, "diff").stdout

    for fname, restored in [("a.txt", "one\n"), ("b.txt", "two\n")]:
        chunk = extract_file_diff(full_diff, fname)
        assert chunk.endswith("\n")
        ok, err = apply_reverse_patch(repo, chunk)
        assert ok, err
        assert (repo / fname).read_text() == restored


def test_truncate_diff_by_file_never_cuts_mid_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    (repo / "c.txt").write_text("three\n")
    git(repo, "add", "a.txt", "b.txt", "c.txt")
    git(repo, "commit", "-m", "three files")

    (repo / "a.txt").write_text("one changed\n")
    (repo / "b.txt").write_text("two changed\n")
    (repo / "c.txt").write_text("three changed\n")
    full_diff = git(repo, "diff").stdout

    # Budget only large enough for the first file's chunk.
    a_chunk = extract_file_diff(full_diff, "a.txt")
    kept, omitted = git_changes._truncate_diff_by_file(full_diff, len(a_chunk))

    assert kept == a_chunk
    assert omitted == ["b.txt", "c.txt"]
    # What's kept must still be a complete, appliable patch for a.txt alone.
    ok, err = apply_reverse_patch(repo, kept)
    assert ok, err
    assert (repo / "a.txt").read_text() == "one\n"
    # Untouched by the (correctly omitted) chunks for b.txt/c.txt.
    assert (repo / "b.txt").read_text() == "two changed\n"


def test_truncate_diff_by_file_under_budget_is_unchanged():
    full_diff = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n"
    kept, omitted = git_changes._truncate_diff_by_file(full_diff, 10_000)
    assert kept == full_diff
    assert omitted == []


def test_git_diff_keeps_carriage_return_and_line_feed_filenames_distinct(tmp_path):
    init_repo(tmp_path)
    cr_name = "a\rfile.txt"
    lf_name = "a\nfile.txt"
    for name in (cr_name, lf_name):
        (tmp_path / name).write_text("before\n")
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "-m", "Add similarly named files")
    tracker = prepare_tracker(str(tmp_path), topic="t", agent="codex", adhoc=False, msg_id=777)
    assert tracker is not None
    (tmp_path / cr_name).write_text("edited CR\n")
    (tmp_path / lf_name).write_text("edited LF\nanother line\n")

    event = tracker.build_event()
    assert {file["path"]: file["additions"] for file in event["files"]} == {cr_name: 1, lf_name: 2}
    cr_patch = extract_file_diff(event["diff"], cr_name)
    assert "+edited CR" in cr_patch
    assert "+edited LF" not in cr_patch
    ok, error = apply_reverse_patch(tmp_path, cr_patch)
    assert ok, error
    assert (tmp_path / cr_name).read_text() == "before\n"
    assert (tmp_path / lf_name).read_text() == "edited LF\nanother line\n"


def test_large_diff_keeps_metadata_and_anchors_historical_patches(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "small.txt").write_text("s\n")
    (tmp_path / "huge.txt").write_text("h\n")
    (tmp_path / "original name.txt").write_text("renamed content\n")
    git(tmp_path, "add", "small.txt", "huge.txt", "original name.txt")
    git(tmp_path, "commit", "-m", "two files")

    monkeypatch.setattr(git_changes, "MAX_INLINE_DIFF_BYTES", 1)

    tracker = prepare_tracker(
        str(tmp_path), topic="t", agent="codex", adhoc=False, msg_id=777,
    )
    assert tracker is not None
    (tmp_path / "small.txt").write_text("s changed\n")
    (tmp_path / "huge.txt").write_text("h changed\n")
    (tmp_path / "original name.txt").rename(tmp_path / "新 name.txt")

    event = tracker.build_event()
    assert event["diff_deferred"] is True
    assert event["files"] == [
        {"path": "huge.txt", "status": "M", "additions": 1, "deletions": 1, "binary": False},
        {"path": "small.txt", "status": "M", "additions": 1, "deletions": 1, "binary": False},
        {
            "path": "新 name.txt", "old_path": "original name.txt", "status": "R100",
            "additions": 0, "deletions": 0, "binary": False,
        },
    ]
    assert event["additions"] == event["deletions"] == 2
    (tmp_path / "huge.txt").write_text("a later change\n")
    git(tmp_path, "gc", "--prune=now")
    assert event["diff"] == ""
    assert event["base"] and event["head"]

    # Simulate looking it up long after the turn, in a fresh call, using only
    # what would have been persisted in the message row.
    recovered = diff_for_path(tmp_path, event["base"], event["head"], "huge.txt")
    assert "+h changed" in recovered
    assert "small.txt" not in recovered
    assert "a later change" not in recovered
    rename = diff_for_path(tmp_path, event["base"], event["head"], "新 name.txt", "original name.txt")
    assert "rename from original name.txt" in rename
    assert extract_file_diff(rename, "新 name.txt") == rename

