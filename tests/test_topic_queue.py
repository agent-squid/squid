import asyncio
import logging
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent import stats_db
from agent import worktree as worktree_mod
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


def test_flow_completion_hook_contains_background_errors(caplog):
    async def fail_completion(*_args, **_kwargs):
        raise RuntimeError("database busy")

    async def run():
        worker = TopicWorker("work")
        with patch("agent.flow.complete_durable_step", fail_completion):
            worker._trigger_chain_continuation(42, error="failed")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert "flow completion hook failed msg_id=42" in caplog.text


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


def test_queue_listener_receives_authoritative_state_on_enqueue_and_dequeue():
    states = []

    async def run():
        dispatcher = TopicDispatcher(lambda rows: states.append(rows))
        with patch.object(TopicWorker, "start", lambda self: None):
            worker = dispatcher._get_or_create("work", "work")
            item = _make_item(0, 124)
            await worker.enqueue(item)
            worker.drain()

    asyncio.run(run())
    assert states[0][0]["msg_id"] == 124
    assert states[-1] == []


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

    with patch("agent.stats_db.mark_assistant_cancelled"):
        (pos_first, pos_second), pos_second_after_cancel = asyncio.run(run())
    assert (pos_first, pos_second) == (1, 2)
    assert pos_second_after_cancel == 1


def test_worker_marks_item_processing_before_runner_starts():
    async def run():
        worker = TopicWorker("work")
        item = _make_item(seq=0, msg_id=101)
        await worker.q.put(item)
        await worker.q.put(None)

        async def fake_process(_item):
            await _item.out_q.put(None)

        with patch.object(worker, "_process", fake_process), \
             patch("agent.stats_db.insert_run_event") as insert_event:
            await worker._run()
        return await item.out_q.get(), insert_event.call_args

    event, persisted = asyncio.run(run())
    assert event == {"_processing": {"topic": "work"}}
    assert persisted.args == (101, 1, "processing", '{"topic": "work"}')


def test_worker_defers_worktree_setup_until_queued_item_runs(tmp_path, monkeypatch):
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    monkeypatch.setattr(worktree_mod, "_WORKTREES_HOME", tmp_path / ".squid" / "worktrees")
    monkeypatch.setattr(worktree_mod.config, "WORKTREE_TRACK_DIRTY_CHANGES", True)

    seen = {}

    def prompt_worktree_root(prompt: str) -> str:
        match = re.search(r"<squid_code_roots>\n(.+?)  \(repo:", prompt)
        assert match, prompt
        return match.group(1)

    async def fake_runner(prompt, **kwargs):
        wt_root = prompt_worktree_root(prompt)
        if "first turn" in prompt:
            (Path(wt_root) / "app.txt").write_text("first\n")
        elif "second turn" in prompt:
            seen["second_base"] = (Path(wt_root) / "app.txt").read_text()
            (Path(wt_root) / "app.txt").write_text("second\n")
        yield "done"

    async def run():
        topic = "queued-defer"
        worker = TopicWorker(topic)
        first = QueueItem(
            seq=0, topic=topic, agent="codex", prompt="first turn",
            context_history=[], backend="codex", model=None,
            cwd=str(repo), code_roots=[str(repo)], msg_id=901,
        )
        second = QueueItem(
            seq=0, topic=topic, agent="codex", prompt="second turn",
            context_history=[], backend="codex", model=None,
            cwd=str(repo), code_roots=[str(repo)], msg_id=902,
        )
        await worker.enqueue(first)
        await worker.enqueue(second)
        await worker.q.put(None)
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", True), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.runners.get_active_msg_ids", return_value=set()), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"), \
             patch.object(worker, "_trigger_chain_continuation", lambda msg_id: None):
            await asyncio.wait_for(worker._run(), timeout=5)

        chunks = []
        for item in (first, second):
            while True:
                chunk = await item.out_q.get()
                chunks.append(chunk)
                if chunk is None:
                    break
        return chunks

    chunks = asyncio.run(run())
    sync_tools = [chunk["_tool"] for chunk in chunks if isinstance(chunk, dict) and chunk.get("_tool", {}).get("name") == "WorktreeSync"]
    assert [tool["status"] for tool in sync_tools] == ["synced", "synced"]
    assert seen["second_base"] == "first\n"
    assert (repo / "app.txt").read_text() == "second\n"


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


def test_native_shell_never_receives_llm_prompt_prefixes(tmp_path):
    captured = {}

    async def fake_shell(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["cwd"] = kwargs["cwd"]
        yield "ok\n"

    async def run():
        worker = TopicWorker("ops")
        item = QueueItem(
            seq=0,
            topic="ops",
            agent="codex",
            prompt="ls -al",
            context_history=[],
            backend="codex",
            cwd=str(tmp_path),
            code_roots=[str(tmp_path)],
            msg_id=11146,
            native_shell=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_native_shell", fake_shell), \
             patch("agent.git_changes.prepare_trackers", return_value=[]), \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)

    asyncio.run(run())
    assert captured == {"prompt": "ls -al", "cwd": str(tmp_path)}


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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
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
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.get_topic_session", return_value=None), \
             patch("agent.stats_db.set_topic_session") as set_topic_session, \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        return set_topic_session.call_args

    call_args = asyncio.run(run())
    assert call_args.args[:4] == ("work", "codex", "thread-1", "/agent/config/cwd")
    assert len(call_args.args[4]) == 16  # backend configuration fingerprint


def test_queued_turn_inherits_session_when_it_starts():
    captured = {}

    async def fake_runner(_prompt, **kwargs):
        captured.update(kwargs)
        yield {"_stats": {"session_id": "session-from-first-turn"}}

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="second",
            context_history=[], backend="codex", cwd="/old/cwd",
            source_cwd="/old/cwd", configured_cwd="/configured/cwd", adhoc=False, msg_id=794,
            refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.get_topic_session", return_value={
                 "session_id": "session-from-first-turn",
                 "cwd": "/new/cwd",
                 "runtime_fingerprint": None,
             }), \
             patch("agent.stats_db.rebind_pending_assistant_session") as rebind_session, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.save_stats"):
            await worker._process(item)
        return item, rebind_session.call_args

    item, rebind_call = asyncio.run(run())
    assert captured["resume_session_id"] == "session-from-first-turn"
    assert captured["cwd"] == "/new/cwd"
    assert item.source_cwd == "/new/cwd"
    assert rebind_call.args == (794, "session-from-first-turn")


def test_queued_turn_does_not_start_after_pending_row_was_cancelled():
    runner_called = False

    async def fake_runner(_prompt, **kwargs):
        nonlocal runner_called
        runner_called = True
        yield "unexpected"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="cancelled",
            context_history=[], backend="codex", cwd=None, source_cwd=None,
            configured_cwd=None, adhoc=False, msg_id=800,
            refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.get_topic_session", return_value=None), \
             patch("agent.stats_db.rebind_pending_assistant_session", return_value=False):
            await worker._process(item)
        return await item.out_q.get(), await item.out_q.get()

    error, sentinel = asyncio.run(run())
    assert error == {"_error": "Cancelled before start"}
    assert sentinel is None
    assert not runner_called


def test_queued_turn_clears_session_with_stale_runtime_fingerprint():
    captured = {}

    async def fake_runner(_prompt, **kwargs):
        captured.update(kwargs)
        yield "fresh"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="second",
            context_history=[], backend="codex", cwd="/old/cwd",
            source_cwd="/old/cwd", configured_cwd="/configured/cwd",
            adhoc=False, msg_id=799, refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.get_topic_session", return_value={
                 "session_id": "stale-session", "cwd": "/stale/cwd",
                 "runtime_fingerprint": "stale-fingerprint",
             }), \
             patch("agent.stats_db.clear_topic_session") as clear_session, \
             patch("agent.stats_db.rebind_pending_assistant_session") as rebind_session, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.get_worktrees", return_value=[]):
            await worker._process(item)
        return clear_session.call_args, rebind_session.call_args

    clear_call, rebind_call = asyncio.run(run())
    assert "resume_session_id" not in captured
    assert captured["cwd"] == "/configured/cwd"
    assert clear_call.args == ("work", "codex")
    assert rebind_call.args == (799, None)


def test_queued_native_shell_inherits_latest_session_cwd_without_resuming():
    captured = {}

    async def fake_shell(_prompt, **kwargs):
        captured.update(kwargs)
        yield "ok"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="pwd",
            context_history=[], backend="codex", cwd="/old/cwd",
            source_cwd="/old/cwd", configured_cwd="/configured/cwd", native_shell=True, msg_id=795,
            refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_native_shell", fake_shell), \
             patch("agent.stats_db.get_topic_session", return_value={
                 "session_id": "session-from-first-turn",
                 "cwd": "/new/cwd",
                 "runtime_fingerprint": None,
             }), \
             patch("agent.stats_db.rebind_pending_assistant_session") as rebind_session, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.get_worktrees", return_value=[]):
            await worker._process(item)
        return rebind_session.call_args

    rebind_call = asyncio.run(run())
    assert captured["cwd"] == "/new/cwd"
    assert "resume_session_id" not in captured
    assert rebind_call is None


def test_queued_turn_drops_stale_session_when_preceding_turn_cleared_it():
    captured = {}

    async def fake_runner(_prompt, **kwargs):
        captured.update(kwargs)
        yield "fresh"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="second",
            context_history=[], backend="codex", cwd="/old/session/cwd",
            source_cwd="/old/session/cwd", configured_cwd="/agent/cwd",
            resume_session_id="stale-session", adhoc=False, msg_id=796,
            refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.get_topic_session", return_value=None), \
             patch("agent.stats_db.rebind_pending_assistant_session") as rebind_session, \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.get_worktrees", return_value=[]):
            await worker._process(item)
        return item, rebind_session.call_args

    item, rebind_call = asyncio.run(run())
    assert "resume_session_id" not in captured
    assert captured["cwd"] == "/agent/cwd"
    assert item.resume_session_id is None
    assert rebind_call.args == (796, None)


def test_queued_turn_without_configured_cwd_drops_stale_session_cwd():
    captured = {}

    async def fake_runner(_prompt, **kwargs):
        captured.update(kwargs)
        yield "fresh"

    async def run():
        worker = TopicWorker("work")
        item = QueueItem(
            seq=1, topic="work", agent="codex", prompt="second",
            context_history=[], backend="codex", cwd="/stale/session/cwd",
            source_cwd="/stale/session/cwd", configured_cwd=None,
            resume_session_id="stale-session", adhoc=False, msg_id=797,
            refresh_session_at_start=True,
        )
        with patch("agent.config.WORKTREE_ISOLATION_ENABLED", False), \
             patch("agent.runners.run_codex", fake_runner), \
             patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.get_topic_session", return_value=None), \
             patch("agent.stats_db.rebind_pending_assistant_session"), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.get_worktrees", return_value=[]):
            await worker._process(item)

    asyncio.run(run())
    assert captured["cwd"] != "/stale/session/cwd"
    assert "resume_session_id" not in captured


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
             patch("agent.stats_db.get_completed_run_text", return_value=None), \
             patch("agent.stats_db.get_completed_run_status_raw", return_value=""), \
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
             patch("agent.stats_db.get_completed_run_text", return_value=None), \
             patch("agent.stats_db.get_completed_run_status_raw", return_value=""), \
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


def test_worker_keeps_session_on_auth_required():
    async def fake_runner(prompt, **kwargs):
        from agent.runners import CLIAuthRequired
        if False:
            yield ""
        raise CLIAuthRequired("claudecode", "Claude auth failed")

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
            resume_session_id="stale-session-123",
            msg_id=501,
        )
        with patch("agent.runners.runner_for_agent", return_value=fake_runner), \
             patch("agent.stats_db.insert_run_event"), \
             patch("agent.stats_db.update_assistant_message"), \
             patch("agent.stats_db.set_topic_session"), \
             patch("agent.stats_db.clear_topic_session") as clear_session, \
             patch("agent.stats_db.get_worktrees", return_value=[]), \
             patch("agent.stats_db.get_completed_run_text", return_value=None), \
             patch("agent.stats_db.get_completed_run_status_raw", return_value=""), \
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
    assert clear_call is None
    errors = [c["_error"] for c in chunks if isinstance(c, dict) and "_error" in c]
    assert errors == ["[[cli-auth-required:claudecode]] Claude auth failed"]


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

        processing = await asyncio.wait_for(item.out_q.get(), timeout=1)
        error = await asyncio.wait_for(item.out_q.get(), timeout=1)
        sentinel = await asyncio.wait_for(item.out_q.get(), timeout=1)
        return processing, error, sentinel

    processing, error, sentinel = asyncio.run(run())
    assert processing == {"_processing": {"topic": "work"}}
    assert error == {"_error": "boom"}
    assert sentinel is None


# --- ADR-0037: provider-scoped queueing + active load/unload ---------------


def test_dispatch_native_shell_uses_sequential_lane_unless_adhoc():
    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None):
            _out1, _seq1, normal1 = await dispatcher.dispatch(
                topic="ops", prompt="sleep 1", context_history=[],
                harness="codex", agent="codex", native_shell=True,
            )
            _out2, _seq2, normal2 = await dispatcher.dispatch(
                topic="ops", prompt="pwd", context_history=[],
                harness="codex", agent="codex", native_shell=True,
            )
            _out3, _seq3, adhoc1 = await dispatcher.dispatch(
                topic="ops", prompt="top", context_history=[],
                harness="codex", agent="codex", native_shell=True, adhoc=True,
            )
            _out4, _seq4, adhoc2 = await dispatcher.dispatch(
                topic="ops", prompt="date", context_history=[],
                harness="codex", agent="codex", native_shell=True, adhoc=True,
            )
        return normal1, normal2, adhoc1, adhoc2, list(dispatcher._workers)

    normal1, normal2, adhoc1, adhoc2, keys = asyncio.run(run())
    assert normal1 is normal2
    assert adhoc1 is not normal1
    assert adhoc2 is not normal1
    assert adhoc1 is not adhoc2
    assert keys == ["ops@codex", "__adhoc_1", "__adhoc_2"]


def _fake_local_provider(provider_id="ollama"):
    from agent.providers import Provider

    return Provider(id=provider_id, label="Ollama", auth_type="none", parallel=False,
                     base_url="http://localhost:11434/v1")


def test_dispatch_collapses_local_provider_agents_into_one_shared_lane():
    # Two different topics/agents that both resolve to a parallel: false
    # provider must land in the same TopicWorker (one FIFO lane per physical
    # resource), unlike every other provider which keeps per-(topic, agent)
    # parallel lanes. Using two different harnesses (pi, opencode) confirms
    # the lane is keyed by provider, not by harness.
    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch("agent.providers.get_provider", lambda pid: _fake_local_provider(pid)):
            _out_q1, _seq1, worker1 = await dispatcher.dispatch(
                topic="topicA", prompt="hi", context_history=[],
                harness="pi", provider="ollama", agent="qwen25-pi",
            )
            _out_q2, _seq2, worker2 = await dispatcher.dispatch(
                topic="topicB", prompt="hi", context_history=[],
                harness="opencode", provider="ollama", agent="qwen30-opencode",
            )
        return worker1, worker2, dispatcher._workers

    worker1, worker2, workers = asyncio.run(run())
    assert worker1 is worker2
    assert list(workers.keys()) == ["provider:ollama"]


def test_dispatch_collapses_adhoc_into_shared_lane_for_local_provider():
    # An adhoc prompt against a parallel: false provider must join the same
    # shared lane as session traffic, not get its own always-parallel
    # ephemeral worker — otherwise adhoc requests would thrash the local
    # daemon/GPU alongside whatever's already queued there.
    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch("agent.providers.get_provider", lambda pid: _fake_local_provider(pid)):
            _out_q1, _seq1, worker1 = await dispatcher.dispatch(
                topic="topicA", prompt="hi", context_history=[],
                harness="pi", provider="ollama", agent="qwen25-pi",
            )
            _out_q2, _seq2, worker2 = await dispatcher.dispatch(
                topic="topicB", prompt="hi", context_history=[],
                harness="opencode", provider="ollama", agent="qwen30-opencode",
                adhoc=True,
            )
        return worker1, worker2, dispatcher._workers

    worker1, worker2, workers = asyncio.run(run())
    assert worker1 is worker2
    assert list(workers.keys()) == ["provider:ollama"]


def test_dispatch_parallel_flag_overrides_auth_type():
    # parallel is independent of auth.type: a credential-free provider that
    # opts back into parallel: true must NOT collapse into a shared lane...
    from agent.providers import Provider

    async def run_parallel_none_auth():
        dispatcher = TopicDispatcher()
        beefy = Provider(id="ollama", label="Ollama", auth_type="none", parallel=True,
                          base_url="http://localhost:11434/v1")
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch("agent.providers.get_provider", lambda pid: beefy):
            _out_q1, _seq1, worker1 = await dispatcher.dispatch(
                topic="topicA", prompt="hi", context_history=[],
                harness="pi", provider="ollama", agent="qwen25-pi",
            )
            _out_q2, _seq2, worker2 = await dispatcher.dispatch(
                topic="topicB", prompt="hi", context_history=[],
                harness="pi", provider="ollama", agent="qwen30-pi",
            )
        return worker1, worker2

    worker1, worker2 = asyncio.run(run_parallel_none_auth())
    assert worker1 is not worker2

    # ...and conversely, an api_key/subscription provider marked
    # parallel: false must collapse just like a none-auth one would.
    async def run_serial_key_auth():
        dispatcher = TopicDispatcher()
        shared = Provider(id="shared-vllm", label="Shared vLLM", auth_type="api_key",
                           api_key="secret", parallel=False, base_url="http://vllm.internal/v1")
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch("agent.providers.get_provider", lambda pid: shared):
            _out_q1, _seq1, worker1 = await dispatcher.dispatch(
                topic="topicA", prompt="hi", context_history=[],
                harness="pi", provider="shared-vllm", agent="a",
            )
            _out_q2, _seq2, worker2 = await dispatcher.dispatch(
                topic="topicB", prompt="hi", context_history=[],
                harness="pi", provider="shared-vllm", agent="b",
            )
        return worker1, worker2, dispatcher._workers

    worker1, worker2, workers = asyncio.run(run_serial_key_auth())
    assert worker1 is worker2
    assert list(workers.keys()) == ["provider:shared-vllm"]


def test_dispatch_keeps_parallel_lanes_for_non_local_provider():
    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None):
            _out_q1, _seq1, worker1 = await dispatcher.dispatch(
                topic="topicA", prompt="hi", context_history=[],
                harness="codex", provider="openai", agent="codex-a",
            )
            _out_q2, _seq2, worker2 = await dispatcher.dispatch(
                topic="topicB", prompt="hi", context_history=[],
                harness="codex", provider="openai", agent="codex-b",
            )
        return worker1, worker2

    worker1, worker2 = asyncio.run(run())
    assert worker1 is not worker2


def test_dispatch_falls_back_to_legacy_key_for_unknown_harness():
    # default_provider_for() KeyErrors on an unresolvable harness — dispatch
    # must swallow that and enqueue anyway so resolve_agent() inside
    # _process() can report the real error, instead of raising before the
    # item is even queued.
    async def run():
        dispatcher = TopicDispatcher()
        with patch.object(TopicWorker, "start", lambda self: None), \
             patch.object(TopicWorker, "enqueue", return_value=9):
            _out_q, seq, worker = await dispatcher.dispatch(
                topic="work", prompt="hi", context_history=[],
                harness="not-a-real-harness", agent="ghost",
            )
        return seq, dispatcher._workers

    seq, workers = asyncio.run(run())
    assert seq == 9
    assert list(workers.keys()) == ["work@ghost"]


def test_drain_topic_scopes_to_requesting_topic_in_shared_provider_lane():
    # A provider-scoped lane can hold items from multiple topics at once —
    # draining topic A must not touch topic B's queued items.
    async def run():
        dispatcher = TopicDispatcher()
        worker = TopicWorker("topicA")
        dispatcher._workers["provider:ollama"] = worker
        item_a = QueueItem(seq=0, topic="topicA", agent="qwen25-pi", prompt="p",
                            context_history=[], backend="pi", model=None, msg_id=1)
        item_b = QueueItem(seq=1, topic="topicB", agent="qwen30-ollama", prompt="p",
                            context_history=[], backend="ollama", model=None, msg_id=2)
        worker.q.put_nowait(item_a)
        worker.q.put_nowait(item_b)

        drained = dispatcher.drain_topic("topicA")
        remaining = worker.queue_items()
        return drained, remaining

    with patch("agent.stats_db.mark_assistant_cancelled"):
        drained, remaining = asyncio.run(run())
    assert drained == 1
    assert len(remaining) == 1
    assert remaining[0]["topic"] == "topicB"


def test_sync_local_model_emits_loading_event_and_unloads_on_switch():
    async def run():
        worker = TopicWorker("work")
        provider = _fake_local_provider()
        out_q: asyncio.Queue = asyncio.Queue()
        posted = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"models": []}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return FakeResponse()

            async def post(self, url, json):
                posted.append((url, json))
                return FakeResponse()

        with patch("httpx.AsyncClient", FakeClient):
            await worker._sync_local_model(provider, "qwen2.5:7b", out_q)
            first_event = await asyncio.wait_for(out_q.get(), timeout=1)
            await worker._sync_local_model(provider, "qwen3:8b", out_q)
            second_event = await asyncio.wait_for(out_q.get(), timeout=1)
        return first_event, second_event, posted

    first_event, second_event, posted = asyncio.run(run())
    assert first_event == {"_loading": {"to": "qwen2.5:7b"}}
    assert second_event == {"_loading": {"to": "qwen3:8b", "from": "qwen2.5:7b"}}
    assert len(posted) == 1
    assert posted[0][1]["model"] == "qwen2.5:7b"
    assert posted[0][1]["keep_alive"] == 0


def test_sync_local_model_stays_quiet_when_resident_and_unchanged():
    async def run():
        worker = TopicWorker("work")
        worker._last_local_model = "qwen2.5:7b"
        provider = _fake_local_provider()
        out_q: asyncio.Queue = asyncio.Queue()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"models": [{"model": "qwen2.5:7b"}]}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return FakeResponse()

        with patch("httpx.AsyncClient", FakeClient):
            await worker._sync_local_model(provider, "qwen2.5:7b", out_q)
        return out_q.empty()

    assert asyncio.run(run()) is True


def test_sync_local_model_treats_implicit_latest_as_resident_and_unchanged():
    async def run():
        worker = TopicWorker("work")
        worker._last_local_model = "qwen3.5:latest"
        provider = _fake_local_provider()
        out_q: asyncio.Queue = asyncio.Queue()
        posted = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"models": [{"model": "qwen3.5:latest"}]}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return FakeResponse()

            async def post(self, url, json):
                posted.append((url, json))
                return FakeResponse()

        with patch("httpx.AsyncClient", FakeClient):
            await worker._sync_local_model(provider, "qwen3.5", out_q)
        return out_q.empty(), posted

    quiet, posted = asyncio.run(run())
    assert quiet is True
    assert posted == []
