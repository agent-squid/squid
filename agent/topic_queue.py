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
    agent: Optional[str]
    prompt: str
    context_history: list[dict]
    backend: str
    model: Optional[str]
    cwd: Optional[str] = None
    timeout: Optional[int] = None
    resume_session_id: Optional[str] = None
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
        from .runners import run_claude, run_codex, run_copilot, run_cursor, run_antigravity, CLINotFoundError, CLIError
        from .config import SQUID_HOME
        runner = {"claude": run_claude, "cursor": run_cursor, "antigravity": run_antigravity, "codex": run_codex, "copilot": run_copilot}.get(item.backend)
        if runner is None:
            raise CLINotFoundError(f"Unknown backend: {item.backend!r}")
        effective_cwd = item.cwd or SQUID_HOME
        kwargs: dict = dict(
            history=item.context_history, model=item.model,
            cwd=effective_cwd,
            topic=item.topic, agent=item.agent or "",
            response_timeout=item.timeout,
        )
        if item.backend in ("claude", "codex", "cursor", "copilot", "antigravity") and item.resume_session_id:
            kwargs["resume_session_id"] = item.resume_session_id

        try:
            async for chunk in runner(item.prompt, **kwargs):
                await item.out_q.put(chunk)
        except CLIError as exc:
            if item.resume_session_id and "No conversation found" in str(exc):
                status = (
                    f"Session not found — starting fresh\n"
                    f"  session: {item.resume_session_id}\n"
                    f"  cwd: {effective_cwd}\n"
                    f"  backend: {item.backend}"
                    + (f"  model: {item.model}" if item.model else "")
                )
                await item.out_q.put({"_status": status})
                kwargs.pop("resume_session_id", None)
                async for chunk in runner(item.prompt, **kwargs):
                    await item.out_q.put(chunk)
            else:
                raise


class TopicDispatcher:
    def __init__(self):
        self._workers: dict[str, TopicWorker] = {}
        self._adhoc_counter: int = 0

    def _get_or_create(self, key: str, topic: str) -> TopicWorker:
        if key not in self._workers:
            worker = TopicWorker(topic)
            worker.start()
            self._workers[key] = worker
        return self._workers[key]

    async def dispatch(
        self,
        topic: str,
        prompt: str,
        context_history: list[dict],
        backend: str,
        model: Optional[str],
        agent: Optional[str] = None,
        cwd: Optional[str] = None,
        response_timeout: Optional[int] = None,
        resume_session_id: Optional[str] = None,
        adhoc: bool = False,
    ) -> tuple[asyncio.Queue, int, TopicWorker]:
        if adhoc:
            # Each adhoc message gets its own ephemeral worker — never queued, always parallel.
            self._adhoc_counter += 1
            queue_key = f"__adhoc_{self._adhoc_counter}"
        else:
            queue_key = f"{topic}@{agent}" if agent else topic
        worker = self._get_or_create(queue_key, topic)
        item = QueueItem(
            seq=0, topic=topic, agent=agent,
            prompt=prompt, context_history=context_history,
            backend=backend, model=model, cwd=cwd, timeout=response_timeout,
            resume_session_id=resume_session_id,
        )
        seq = await worker.enqueue(item)
        return item.out_q, seq, worker

    def _workers_for_topic(self, topic: str) -> list[TopicWorker]:
        """Return all workers whose queue key starts with this topic."""
        return [w for k, w in self._workers.items() if k == topic or k.startswith(f"{topic}@")]

    def stop_topic(self, topic: str) -> int:
        """Kill only the running process for topic; leave queue intact."""
        from .runners import kill_procs_by_topic
        return kill_procs_by_topic(topic)

    def stopall_topic(self, topic: str) -> dict:
        """Kill running process + drain entire queue for topic (all agent lanes)."""
        from .runners import kill_procs_by_topic
        killed = kill_procs_by_topic(topic)
        drained = sum(w.drain() for w in self._workers_for_topic(topic))
        return {"killed": killed, "drained": drained}

    def drain_topic(self, topic: str, pos: Optional[int] = None) -> int:
        """Drain pending items for topic across all agent lanes."""
        return sum(w.drain(pos) for w in self._workers_for_topic(topic))

    def topics_info(self) -> list[dict]:
        return [
            {
                "name": w.topic,
                "queue_key": k,
                "queue_depth": w.queue_depth(),
                "active": w._processing_seq is not None,
            }
            for k, w in self._workers.items()
        ]
