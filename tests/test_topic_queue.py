import asyncio
from unittest.mock import patch

from agent.topic_queue import QueueItem, TopicDispatcher, TopicWorker


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
