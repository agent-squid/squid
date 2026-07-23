import asyncio
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory import code_roots_prompt_block
from agent.topic_queue import QueueItem, TopicDispatcher, TopicWorker, remap_worktree_paths


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


def test_dispatch_preserves_lookback_on_queue_item():
    captured = {}

    async def fake_enqueue(self, item):
        captured["item"] = item
        return 7

    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch.object(TopicWorker, "enqueue", fake_enqueue):
            _out_q, seq, _worker = await dispatcher.dispatch(
                topic="work",
                prompt="hello",
                context_history=[],
                backend="codex",
                model=None,
                adhoc=True,
                lookback=3,
                msg_id=123,
            )
        return seq

    assert asyncio.run(run()) == 7
    assert captured["item"].lookback == 3


def test_queue_preview_uses_display_prompt_not_augmented_prompt():
    async def run():
        worker = TopicWorker("work")
        augmented = "Persistent user-editable topic memory:\n<topic_memory>secret</topic_memory>\n\nfix app"
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt=augmented,
            display_prompt="fix app",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=124,
        )
        worker.q.put_nowait(item)
        return worker.queue_items()

    assert asyncio.run(run())[0]["prompt_preview"] == "fix app"


def _make_item(seq, msg_id):
    return QueueItem(
        seq=seq, topic="work", agent="codex", prompt="p", display_prompt=None,
        context_history=[], backend="codex", model=None, msg_id=msg_id,
    )


def test_position_of_reflects_live_queue_after_earlier_item_cancelled():
    # Regression: position_of used to subtract seq numbers directly, which
    # overcounts once an earlier-queued item is drained — its seq is never
    # reassigned to the items behind it, so the queue "shrinks" without the
    # remaining items' seq values shifting down to match.
    async def run():
        worker = TopicWorker("work")
        worker._processing_seq = 0  # something is currently running with seq 0
        first = _make_item(seq=1, msg_id=101)
        second = _make_item(seq=2, msg_id=102)
        worker.q.put_nowait(first)
        worker.q.put_nowait(second)

        before = (worker.position_of(1), worker.position_of(2))
        worker.drain(pos=1)  # cancel the first queued item (position 1)
        after = worker.position_of(2)
        return before, after

    (pos_first, pos_second), pos_second_after_cancel = asyncio.run(run())
    assert (pos_first, pos_second) == (1, 2)
    assert pos_second_after_cancel == 1


def test_worker_persists_stats_with_item_lookback():
    async def fake_runner(*args, **kwargs):
        yield {"_stats": {"session_id": "thread-1", "input_tokens": 10, "output_tokens": 5}}

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            adhoc=True,
            lookback=4,
            msg_id=123,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats") as save_stats:
            await worker._process(item)
        return save_stats.call_args

    call_args = asyncio.run(run())
    assert call_args.kwargs["lookback"] == 4
    assert call_args.args[1]["lookback"] == 4


def test_worker_keeps_status_out_of_persisted_response():
    async def fake_runner(*args, **kwargs):
        yield {"_status": "Checking the code..."}
        yield "Final response only."

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=124,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message") as update_message, \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return update_message.call_args, chunks

    update_call, chunks = asyncio.run(run())
    assert update_call.args[1] == "Final response only."
    assert update_call.args[3] == "done"
    assert chunks[:2] == [
        {"_status": "Checking the code..."},
        "Final response only.",
    ]


def test_remap_worktree_paths_uses_source_repo_for_response_links():
    text = (
        "See [app.py](/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61/app.py:12) "
        "and `/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61/tests/test_app.py`."
    )

    remapped = remap_worktree_paths(text, {
        "/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61": "/Users/alice/Work/squid",
    })

    assert "/Users/alice/.squid/worktrees" not in remapped
    assert "[app.py](/Users/alice/Work/squid/app.py:12)" in remapped
    assert "`/Users/alice/Work/squid/tests/test_app.py`" in remapped


def test_worker_remaps_worktree_paths_before_stream_and_persist():
    worktree = "/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61"
    source = "/Users/alice/Work/squid"

    async def fake_runner(*args, **kwargs):
        yield f"See [app.py]({worktree}/app.py:12)"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=2066,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[{
                 "worktree_path": worktree,
                 "repo_root": source,
             }]), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message") as update_message, \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return update_message.call_args, chunks

    update_call, chunks = asyncio.run(run())
    expected = "See [app.py](/Users/alice/Work/squid/app.py:12)"
    assert chunks[:1] == [expected]
    assert update_call.args[1] == expected


def test_worker_remaps_worktree_paths_split_across_chunks():
    worktree = "/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2067-921e61"
    source = "/Users/alice/Work/squid"

    async def fake_runner(*args, **kwargs):
        yield "See [app.py](/Users/alice/.squid/worktrees/abcd1234/sqd"
        yield "-squid-2067-921e61/app.py:12)"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=2067,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[{
                 "worktree_path": worktree,
                 "repo_root": source,
             }]), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message") as update_message, \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return update_message.call_args, chunks

    update_call, chunks = asyncio.run(run())
    content = "".join(chunk for chunk in chunks if isinstance(chunk, str))
    expected = "See [app.py](/Users/alice/Work/squid/app.py:12)"
    assert content == expected
    assert update_call.args[1] == expected


def test_worker_does_not_persist_status_as_response_when_no_final_text():
    async def fake_runner(*args, **kwargs):
        yield {"_status": "Checking the code..."}

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=125,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message") as update_message, \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        return update_message.call_args

    update_call = asyncio.run(run())
    assert update_call.args[1] == ""
    assert update_call.args[3] == "error"


def test_worker_emits_git_diff_tool_event(tmp_path):
    init_repo(tmp_path)

    async def fake_runner(*args, **kwargs):
        cwd = kwargs["cwd"]
        from pathlib import Path
        Path(cwd, "app.txt").write_text("changed by agent\n")
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            cwd=str(tmp_path),
            code_roots=[str(tmp_path)],
            adhoc=True,
            msg_id=456,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return chunks

    chunks = asyncio.run(run())
    tools = [chunk["_tool"] for chunk in chunks if isinstance(chunk, dict) and "_tool" in chunk]
    assert [tool["name"] for tool in tools] == ["GitDiff"]
    assert tools[0]["files"] == [{"status": "M", "path": "app.txt"}]
    assert "+changed by agent" in tools[0]["diff"]


def test_worker_marks_worktree_conflict_and_emits_sync_tool(caplog):
    class FakeTracker:
        def build_event(self):
            return {
                "name": "GitDiff",
                "repo": "/tmp/repo",
                "worktree_repo": "/tmp/wt",
                "worktree_status": "pending",
                "files": [{"status": "M", "path": "app.txt"}],
                "diff": "diff --git a/app.txt b/app.txt\n",
            }

        def build_no_change_event(self):
            return {"name": "GitDiff", "files": [], "no_changes": True}

        def cleanup(self):
            return None

    async def fake_runner(*args, **kwargs):
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            cwd="/tmp/wt",
            code_roots=["/tmp/wt"],
            adhoc=True,
            msg_id=458,
            worktree_setup_elapsed_ms=12.3,
            worktree_isolated=True,
        )
        rec = {
            "repo_root": "/tmp/repo",
            "worktree_path": "/tmp/wt",
            "integration_worktree_path": "/tmp/wt-integration",
        }
        with caplog.at_level(logging.INFO), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[rec]), \
             patch("agent.git_changes.prepare_trackers", return_value=[FakeTracker()]), \
             patch("agent.worktree.sync_after_turn", return_value=["app.txt"]), \
             patch("agent.stats_db.mark_worktree_status") as mark_status, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return chunks, mark_status.call_args

    chunks, mark_status_call = asyncio.run(run())
    tools = [chunk["_tool"] for chunk in chunks if isinstance(chunk, dict) and "_tool" in chunk]
    assert [tool["name"] for tool in tools] == ["GitDiff", "WorktreeSync"]
    assert tools[1]["status"] == "conflict"
    assert tools[1]["conflicts"] == ["app.txt"]
    assert mark_status_call.args == ("work", "458", "/tmp/repo", "conflict")
    assert "worktree turn timing topic=work agent=codex msg_id=458 isolated=True repos=1 setup_ms=12.3" in caplog.text
    assert "statuses=conflict" in caplog.text


def test_worker_runs_deferred_publish_after_worktree_sync():
    class FakeTracker:
        def build_event(self):
            return {
                "name": "GitDiff",
                "repo": "/tmp/repo",
                "worktree_repo": "/tmp/wt",
                "worktree_status": "pending",
                "files": [{"status": "M", "path": "app.txt"}],
                "diff": "diff --git a/app.txt b/app.txt\n",
            }

        def build_no_change_event(self):
            return {"name": "GitDiff", "files": [], "no_changes": True}

        def cleanup(self):
            return None

    async def fake_runner(*args, **kwargs):
        yield "publish queued"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            cwd="/tmp/wt",
            code_roots=["/tmp/wt"],
            adhoc=True,
            msg_id=459,
            worktree_setup_elapsed_ms=12.3,
            worktree_isolated=True,
        )
        rec = {
            "repo_root": "/tmp/repo",
            "worktree_path": "/tmp/wt",
            "integration_worktree_path": "/tmp/wt-integration",
        }
        published_repo = SimpleNamespace(
            repo_root="/tmp/repo",
            branch="main",
            commit="abc123",
            files=["app.txt"],
            pushed=True,
            tag="v0.1",
            tag_pushed=True,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[rec]), \
             patch("agent.git_changes.prepare_trackers", return_value=[FakeTracker()]), \
             patch("agent.worktree.sync_after_turn", return_value=[]), \
             patch("agent.stats_db.mark_worktree_synced"), \
             patch("agent.publish.pop_deferred_publish", return_value=SimpleNamespace(message="ship it", tag="v0.1")), \
             patch("agent.memory.topic_memory_squid_config", return_value={"code_roots": ["/repo"]}), \
             patch("agent.publish.publish_code_roots", return_value=[published_repo]) as publish, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return chunks, publish.call_args

    chunks, publish_call = asyncio.run(run())
    tools = [chunk["_tool"] for chunk in chunks if isinstance(chunk, dict) and "_tool" in chunk]
    assert [tool["name"] for tool in tools] == ["GitDiff", "WorktreeSync", "SquidPublish"]
    assert tools[2]["status"] == "published"
    assert tools[2]["published"][0]["commit"] == "abc123"
    assert publish_call.args == ("work", ["/repo"])
    assert publish_call.kwargs == {"message": "ship it", "tag": "v0.1"}


def test_worker_emits_no_change_git_diff_when_tracked_tree_is_clean(tmp_path):
    init_repo(tmp_path)

    async def fake_runner(*args, **kwargs):
        yield {"_tool": {"name": "Edit", "file": str(tmp_path / "app.txt"), "old": "base", "new": "temp"}}
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            cwd=str(tmp_path),
            code_roots=[str(tmp_path)],
            adhoc=True,
            msg_id=457,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return chunks

    chunks = asyncio.run(run())
    tools = [chunk["_tool"] for chunk in chunks if isinstance(chunk, dict) and "_tool" in chunk]
    assert [tool["name"] for tool in tools] == ["Edit", "GitDiff"]
    assert tools[1]["no_changes"] is True
    assert tools[1]["file_count"] == 0
    assert tools[1]["files"] == []


def test_worker_skips_git_tracking_when_code_roots_empty():
    async def fake_runner(*args, **kwargs):
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            code_roots=[],
            msg_id=789,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.git_changes.prepare_trackers", return_value=[]) as prepare_trackers, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        return prepare_trackers.call_args

    call_args = asyncio.run(run())
    assert call_args.args[0] == []


def test_worker_skips_git_tracking_when_code_roots_missing():
    async def fake_runner(*args, **kwargs):
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            code_roots=None,
            msg_id=789,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.git_changes.prepare_trackers", return_value=[]) as prepare_trackers, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        return prepare_trackers.call_args

    call_args = asyncio.run(run())
    assert call_args.args[0] == []


def test_worker_keeps_agent_cwd_when_code_roots_are_tracked(tmp_path):
    init_repo(tmp_path)

    captured = {}

    async def fake_runner(*args, **kwargs):
        captured["cwd"] = kwargs["cwd"]
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            cwd="/should/not/win",
            code_roots=[str(tmp_path)],
            adhoc=True,
            msg_id=790,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

    asyncio.run(run())
    assert captured["cwd"] == "/should/not/win"


def test_worker_keeps_real_code_roots_in_prompt(tmp_path):
    init_repo(tmp_path)
    captured = {}

    async def fake_runner(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["cwd"] = kwargs["cwd"]
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt=code_roots_prompt_block([str(tmp_path)]) + "\n\nedit app.txt",
            context_history=[],
            backend="codex",
            model=None,
            cwd="/should/not/win",
            code_roots=[str(tmp_path)],
            adhoc=True,
            msg_id=791,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

    asyncio.run(run())
    assert captured["cwd"] == "/should/not/win"
    assert str(tmp_path) in captured["prompt"]


def test_worker_passes_display_prompt_as_runner_preview(tmp_path):
    init_repo(tmp_path)
    captured = {}

    async def fake_runner(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["prompt_preview"] = kwargs["prompt_preview"]
        yield "done"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt=code_roots_prompt_block([str(tmp_path)]) + "\n\nedit app.txt",
            display_prompt="edit app.txt",
            context_history=[],
            backend="codex",
            model=None,
            cwd="/tmp/project",
            code_roots=[str(tmp_path)],
            adhoc=True,
            msg_id=793,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

    asyncio.run(run())
    assert str(tmp_path) in captured["prompt"]
    assert captured["prompt_preview"] == "edit app.txt"


def test_worker_persists_agent_cwd_for_sessions_with_code_roots(tmp_path):
    init_repo(tmp_path)

    async def fake_runner(*args, **kwargs):
        yield {"_stats": {"session_id": "thread-1"}}

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt=code_roots_prompt_block([str(tmp_path)]),
            context_history=[],
            backend="codex",
            model=None,
            cwd="/agent/config/cwd",
            code_roots=[str(tmp_path)],
            adhoc=False,
            msg_id=792,
        )
        with patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session") as set_topic_session, \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        return set_topic_session.call_args

    call_args = asyncio.run(run())
    assert call_args.args[:4] == ("work", "codex", "thread-1", "/agent/config/cwd")
    assert len(call_args.args[4]) == 16  # backend configuration fingerprint


def test_worker_clears_session_on_prompt_too_long_text_response():
    """When Claude responds with 'Prompt is too long' as text (not error), clear the DB session."""
    async def fake_runner(prompt, **kwargs):
        yield "Prompt is too long"

    async def run():
        worker = TopicWorker("mai")
        item = QueueItem(
            seq=0,
            topic="mai",
            agent="clive",
            prompt="design an LLM orchestrator",
            context_history=[],
            backend="claude",
            model=None,
            adhoc=False,
            resume_session_id="old-session-789",
            msg_id=526,
        )
        with patch("agent.runners.run_claude_interactive_cli", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.clear_topic_session") as clear_session, \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return clear_session.call_args, chunks

    clear_call, chunks = asyncio.run(run())
    assert clear_call is not None, "clear_topic_session must be called when text response is 'Prompt is too long'"
    assert clear_call.args == ("mai", "clive")
    assert "Prompt is too long" in chunks


def test_worker_retries_fresh_on_prompt_too_long():
    call_count = 0

    async def fake_runner(prompt, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            from agent.runners import CLIError
            raise CLIError("Prompt is too long")
        yield "success"

    async def run():
        worker = TopicWorker("mai")
        item = QueueItem(
            seq=0,
            topic="mai",
            agent="clive",
            prompt="continue",
            context_history=[],
            backend="claude",
            model=None,
            adhoc=False,
            resume_session_id="old-session-123",
            msg_id=500,
        )
        with patch("agent.runners.run_claude_interactive_cli", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.clear_topic_session") as clear_session, \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

        chunks = []
        while True:
            chunk = await item.out_q.get()
            chunks.append(chunk)
            if chunk is None:
                break
        return clear_session.call_args, chunks

    clear_call, chunks = asyncio.run(run())
    assert call_count == 2
    assert clear_call is not None
    assert clear_call.args == ("mai", "clive")
    status_chunks = [c for c in chunks if isinstance(c, dict) and "_status" in c]
    assert any("context window exceeded" in c["_status"].lower() for c in status_chunks)
    assert "success" in chunks


def test_worker_bug_emits_error_and_sentinel():
    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=0,
            topic="work",
            agent="codex",
            prompt="hello",
            context_history=[],
            backend="codex",
            model=None,
            msg_id=123,
        )

        async def fail(_item):
            raise RuntimeError("boom")

        with patch.object(worker, "_process", fail):
            await worker.enqueue(item)
            await worker.q.put(None)
            await asyncio.wait_for(worker._run(), timeout=1)

        error = await asyncio.wait_for(item.out_q.get(), timeout=1)
        sentinel = await asyncio.wait_for(item.out_q.get(), timeout=1)
        return error, sentinel

    error, sentinel = asyncio.run(run())
    assert error == {"_error": "boom"}
    assert sentinel is None
