import asyncio
import subprocess
from unittest.mock import patch

from agent.memory import code_roots_prompt_block
from agent.topic_queue import QueueItem, TopicDispatcher, TopicWorker


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
    assert call_args.args == ("work", "codex", "thread-1", "/agent/config/cwd")


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
