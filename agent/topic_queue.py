"""
topic_queue.py — Per-topic FIFO queues with parallel execution across topics.
"""
import asyncio
import logging
from dataclasses import dataclass, field
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
    cwd: Optional[str] = None
    timeout: Optional[int] = None
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
        """Returns 0 if the item is being processed (or worker is idle), N if N items are ahead."""
        if self._processing_seq is None:
            return 0  # worker is idle, item will start immediately
        return seq - self._processing_seq

    def queue_depth(self) -> int:
        return self.q.qsize()

    async def enqueue(self, item: QueueItem) -> int:
        item.seq = self._next_seq
        self._next_seq += 1
        await self.q.put(item)
        return item.seq

    def drain(self, pos: Optional[int] = None) -> int:
        """Remove pending items from the queue (not the currently-running one).
        pos=None  → drain all
        pos=N>0   → remove Nth item (1-based, from front)
        pos=N<0   → remove Nth item from end (-1=last, -2=second-to-last, …)
        Cancelled items get an error sentinel so waiting SSE clients close cleanly.
        """
        pending = []
        while not self.q.empty():
            try:
                pending.append(self.q.get_nowait())
            except asyncio.QueueEmpty:
                break

        def _cancel(item):
            item.out_q.put_nowait({"_error": "Cancelled"})
            item.out_q.put_nowait(None)

        if pos is None:
            for item in pending:
                if item is not None:
                    _cancel(item)
            return len(pending)

        real = [i for i in pending if i is not None]
        if not real:
            return 0

        # Convert to 0-based index
        idx = (pos - 1) if pos > 0 else pos
        if idx < -len(real) or idx >= len(real):
            for item in real:
                self.q.put_nowait(item)
            return 0

        removed = real.pop(idx)
        _cancel(removed)
        for item in real:
            self.q.put_nowait(item)
        return 1

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
        from .runners import run_auto, run_claude, run_codex, run_copilot, run_cursor
        from .config import SQUID_HOME
        runner = {"auto": run_auto, "claude": run_claude, "cursor": run_cursor, "codex": run_codex, "copilot": run_copilot}.get(
            item.backend, run_auto
        )
        async for chunk in runner(
            item.prompt, history=item.context_history, model=item.model,
            cwd=item.cwd or SQUID_HOME,
            topic=item.topic, alias=item.alias or "",
            response_timeout=item.timeout,
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
        alias: Optional[str] = None,
        cwd: Optional[str] = None,
        response_timeout: Optional[int] = None,
    ) -> tuple[asyncio.Queue, int, TopicWorker]:
        worker = self._get_or_create(topic)
        item = QueueItem(
            seq=0, topic=topic, alias=alias,
            prompt=prompt, context_history=context_history,
            backend=backend, model=model, cwd=cwd, timeout=response_timeout,
        )
        seq = await worker.enqueue(item)
        return item.out_q, seq, worker

    def stop_topic(self, topic: str) -> int:
        """Kill only the running process for topic; leave queue intact."""
        from .runners import kill_procs_by_topic
        return kill_procs_by_topic(topic)

    def stopall_topic(self, topic: str) -> dict:
        """Kill running process + drain entire queue for topic."""
        from .runners import kill_procs_by_topic
        killed = kill_procs_by_topic(topic)
        worker = self._workers.get(topic)
        drained = worker.drain() if worker else 0
        return {"killed": killed, "drained": drained}

    def drain_topic(self, topic: str, pos: Optional[int] = None) -> int:
        """Drain pending items for topic (leaves current process running)."""
        worker = self._workers.get(topic)
        return worker.drain(pos) if worker else 0

    def topics_info(self) -> list[dict]:
        return [
            {
                "name": t,
                "queue_depth": w.queue_depth(),
                "active": w._processing_seq is not None,
            }
            for t, w in self._workers.items()
        ]
