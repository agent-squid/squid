"""
topic_queue.py — Per-topic FIFO queues with parallel execution across topics.
"""
import asyncio
import json
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
    code_roots: Optional[list[str]] = None
    timeout: Optional[int] = None
    resume_session_id: Optional[str] = None
    adhoc: bool = False
    lookback: int = 0
    msg_id: Optional[int] = None
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

    def queue_items(self) -> list[dict]:
        """Peek at pending items without consuming them."""
        items = list(self.q._queue)  # deque peek — non-destructive
        return [
            {
                "topic": it.topic,
                "agent": it.agent,
                "msg_id": it.msg_id,
                "position": idx + 1,
                "prompt_preview": (it.prompt[:80] + "…") if len(it.prompt) > 80 else it.prompt,
            }
            for idx, it in enumerate(items) if it is not None
        ]

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
                log.exception("Worker bug (topic=%s)", self.topic)
                await item.out_q.put({"_error": str(exc)})
                await item.out_q.put(None)
            finally:
                self._processing_seq = None
            self.q.task_done()

    async def _process(self, item: QueueItem):
        from .runners import run_claude, run_codex, run_copilot, run_cursor, run_antigravity, run_opencode, CLINotFoundError, CLIError
        from .config import SQUID_HOME
        from .backends import get_backend
        from .stats_db import insert_run_event, update_assistant_message, save_stats, set_topic_session
        from .git_changes import prepare_trackers

        backend = get_backend(item.backend)
        if backend is None:
            await item.out_q.put({"_error": f"Backend {item.backend!r} is not configured"})
            await item.out_q.put(None)
            return
        runner = {"claude": run_claude, "codex": run_codex, "cursor": run_cursor,
                  "copilot": run_copilot, "antigravity": run_antigravity,
                  "opencode": run_opencode}.get(backend.driver)
        if runner is None:
            await item.out_q.put({"_error": f"Driver {backend.driver!r} is not supported"})
            await item.out_q.put(None)
            return
        try:
            backend_env = backend.execution_env()
        except ValueError as exc:
            await item.out_q.put({"_error": str(exc)})
            await item.out_q.put(None)
            return

        source_cwd = item.cwd or SQUID_HOME
        tracking_roots = item.code_roots or []
        git_trackers = await asyncio.to_thread(
            prepare_trackers,
            tracking_roots,
            topic=item.topic,
            agent=item.agent,
            adhoc=item.adhoc,
            msg_id=item.msg_id,
        )
        effective_cwd = source_cwd
        effective_prompt = item.prompt
        kwargs: dict = dict(
            history=item.context_history, model=item.model,
            cwd=effective_cwd,
            topic=item.topic, agent=item.agent or "",
            response_timeout=item.timeout,
            adhoc=item.adhoc, msg_id=item.msg_id,
            backend_id=item.backend, backend_env=backend_env,
            backend_settings=backend.driver_settings(), backend_args=backend.args,
        )
        if item.resume_session_id:
            kwargs["resume_session_id"] = item.resume_session_id

        run_seq = 0
        raw = ""
        status_raw = ""
        tool_events: list[dict] = []
        session_id: Optional[str] = None

        async def _emit_tool(tool: dict):
            nonlocal run_seq
            tool_events.append(tool)
            insert_run_event(item.msg_id, run_seq, "tool", json.dumps(tool))
            await item.out_q.put({"_tool": tool})
            run_seq += 1

        async def _emit_git_diff():
            if not git_trackers:
                return
            for tracker in git_trackers:
                try:
                    try:
                        tool = await asyncio.to_thread(tracker.build_event)
                    finally:
                        await asyncio.to_thread(tracker.cleanup)
                except Exception:
                    log.exception("Failed to build git diff for msg_id=%s", item.msg_id)
                    continue
                if tool:
                    await _emit_tool(tool)

        async def _stream(prompt, **kw):
            nonlocal run_seq, session_id, raw, status_raw
            async for chunk in runner(prompt, **kw):
                if isinstance(chunk, dict):
                    if "_stats" in chunk:
                        inner = chunk["_stats"]
                        session_id = inner.get("session_id")
                        enriched = {k: v for k, v in inner.items() if k != "session_id"}
                        enriched["adhoc"] = item.adhoc
                        enriched["lookback"] = item.lookback
                        if session_id:
                            save_stats(session_id, enriched, topic=item.topic, agent=item.agent,
                                       backend=item.backend, model=item.model, cwd=effective_cwd,
                                       lookback=item.lookback)
                            if item.agent and not item.adhoc:
                                set_topic_session(
                                    item.topic, item.agent, session_id, effective_cwd,
                                    backend.fingerprint,
                                )
                            enriched["session_id"] = session_id
                            enriched["cwd"] = effective_cwd
                        insert_run_event(item.msg_id, run_seq, "stats", json.dumps(enriched))
                        await item.out_q.put({"_stats": enriched})
                    elif "_tool" in chunk:
                        await _emit_tool(chunk["_tool"])
                        continue
                    elif "_status" in chunk:
                        status_raw += chunk["_status"]
                        insert_run_event(item.msg_id, run_seq, "status", chunk["_status"])
                        await item.out_q.put(chunk)
                    elif "_error" in chunk:
                        insert_run_event(item.msg_id, run_seq, "error", chunk["_error"])
                        await item.out_q.put(chunk)
                else:
                    raw += chunk
                    insert_run_event(item.msg_id, run_seq, "text", chunk)
                    await item.out_q.put(chunk)
                run_seq += 1

        try:
            try:
                await _stream(effective_prompt, **kwargs)
            except CLIError as exc:
                if item.resume_session_id and "No conversation found" in str(exc):
                    status = (
                        f"Session not found — starting fresh\n"
                        f"  session: {item.resume_session_id}\n"
                        f"  cwd: {effective_cwd}\n"
                        f"  backend: {item.backend}"
                        + (f"  model: {item.model}" if item.model else "")
                    )
                    insert_run_event(item.msg_id, run_seq, "status", status)
                    run_seq += 1
                    await item.out_q.put({"_status": status})
                    kwargs.pop("resume_session_id", None)
                    await _stream(effective_prompt, **kwargs)
                else:
                    raise

            await _emit_git_diff()
            content = raw or status_raw or ""
            context_json = json.dumps(tool_events) if tool_events else None
            update_assistant_message(item.msg_id, content, session_id,
                                     "done" if content else "error", context=context_json)
            insert_run_event(item.msg_id, run_seq, "done", None)

        except Exception as exc:
            err_text = str(exc)
            if not isinstance(exc, CLIError):
                log.exception("Unexpected error processing msg_id=%s", item.msg_id)
            content = raw or err_text
            try:
                await _emit_git_diff()
            except Exception:
                log.exception("Failed to emit git diff for msg_id=%s", item.msg_id)
            context_json = json.dumps(tool_events) if tool_events else None
            try:
                update_assistant_message(item.msg_id, content, session_id,
                                         "done" if raw else "error", context=context_json)
                insert_run_event(item.msg_id, run_seq, "error", err_text)
            except Exception:
                pass
            await item.out_q.put({"_error": err_text})

        finally:
            await item.out_q.put(None)


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
        lookback: int = 0,
        msg_id: Optional[int] = None,
        code_roots: Optional[list[str]] = None,
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
            backend=backend, model=model, cwd=cwd, code_roots=code_roots, timeout=response_timeout,
            resume_session_id=resume_session_id,
            adhoc=adhoc, lookback=lookback, msg_id=msg_id,
        )
        seq = await worker.enqueue(item)
        return item.out_q, seq, worker

    def _workers_for_topic(self, topic: str) -> list[TopicWorker]:
        """Return all workers whose queue key starts with this topic."""
        return [w for k, w in self._workers.items() if k == topic or k.startswith(f"{topic}@")]

    def stop_topic(self, topic: str, agent: Optional[str] = None,
                   adhoc: Optional[bool] = None) -> int:
        """Kill running processes matching topic + optional agent/adhoc filters.
        Adhoc stop is LIFO — kills only the most recently started adhoc process."""
        from .runners import kill_procs_by_topic
        return kill_procs_by_topic(topic, agent=agent, adhoc=adhoc, lifo=(adhoc is True))

    def stopall_topic(self, topic: str, agent: Optional[str] = None,
                      adhoc: Optional[bool] = None) -> dict:
        """Kill running processes + drain queues for topic (scoped by agent/adhoc)."""
        from .runners import kill_procs_by_topic
        killed = kill_procs_by_topic(topic, agent=agent, adhoc=adhoc)
        drained = sum(w.drain() for w in self._workers_for_topic(topic))
        return {"killed": killed, "drained": drained}

    def drain_topic(self, topic: str, pos: Optional[int] = None) -> int:
        """Drain pending items for topic across all agent lanes."""
        return sum(w.drain(pos) for w in self._workers_for_topic(topic))

    def all_queued_items(self) -> list[dict]:
        """Return pending (not yet running) items across all topic workers."""
        result = []
        for w in self._workers.values():
            result.extend(w.queue_items())
        return result

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
