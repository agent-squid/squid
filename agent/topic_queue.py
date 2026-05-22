"""
topic_queue.py — Per-topic FIFO queues with parallel execution across topics.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class QueueItem:
    seq: int
    topic: str
    alias: Optional[str]
    prompt: str
    context_history: list[dict]
    backend: str
    model: Optional[str]
    cwd: Optional[str]
    out_q: asyncio.Queue = field(default_factory=asyncio.Queue)


class TopicWorker:
    def __init__(self, topic: str):
        self.topic = topic
        self.q: asyncio.Queue = asyncio.Queue()
        self._processing_seq: Optional[int] = None
        self._next_seq: int = 0

    def start(self):
        asyncio.create_task(self._run(), name=f"squid-worker-{self.topic}")

    def position_of(self, seq: int) -> int:
        """Returns 0 if the item is being processed, N if N items are ahead of it."""
        if self._processing_seq is None:
            return seq  # nothing running yet; seq=0 is next
        return seq - self._processing_seq

    def queue_depth(self) -> int:
        return self.q.qsize()

    async def enqueue(self, item: QueueItem) -> int:
        item.seq = self._next_seq
        self._next_seq += 1
        await self.q.put(item)
        return item.seq

    async def _run(self):
        while True:
            item = await self.q.get()
            if item is None:
                break
            self._processing_seq = item.seq
            try:
                await self._process(item)
            except Exception as exc:
                log.exception("Worker error (topic=%s)", self.topic)
                await item.out_q.put({"_error": str(exc)})
            finally:
                await item.out_q.put(None)  # sentinel
                self._processing_seq = None
            self.q.task_done()

    async def _process(self, item: QueueItem):
        from .runners import run_auto, run_claude, run_codex
        runner = {"auto": run_auto, "claude": run_claude, "codex": run_codex}.get(
            item.backend, run_auto
        )
        cwd = str(Path(item.cwd).expanduser()) if item.cwd else None
        async for chunk in runner(
            item.prompt, cwd=cwd, history=item.context_history, model=item.model
        ):
            await item.out_q.put(chunk)


class TopicDispatcher:
    def __init__(self):
        self._workers: dict[str, TopicWorker] = {}

    def _get_or_create(self, topic: str) -> TopicWorker:
        if topic not in self._workers:
            worker = TopicWorker(topic)
            worker.start()
            self._workers[topic] = worker
        return self._workers[topic]

    async def dispatch(
        self,
        topic: str,
        prompt: str,
        context_history: list[dict],
        backend: str,
        model: Optional[str],
        cwd: Optional[str],
        alias: Optional[str] = None,
    ) -> tuple[asyncio.Queue, int, TopicWorker]:
        worker = self._get_or_create(topic)
        item = QueueItem(
            seq=0, topic=topic, alias=alias,
            prompt=prompt, context_history=context_history,
            backend=backend, model=model, cwd=cwd,
        )
        seq = await worker.enqueue(item)
        return item.out_q, seq, worker

    def topics_info(self) -> list[dict]:
        return [
            {
                "name": t,
                "queue_depth": w.queue_depth(),
                "active": w._processing_seq is not None,
            }
            for t, w in self._workers.items()
        ]
