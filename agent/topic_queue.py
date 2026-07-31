"""
topic_queue.py — Per-topic FIFO queues with parallel execution across topics.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def remap_worktree_paths(text: str, worktree_sources: dict[str, str]) -> str:
    """Replace ephemeral Squid worktree path prefixes with source repo paths."""
    if not text or not worktree_sources:
        return text
    remapped = text
    for worktree_path, source_path in sorted(
        worktree_sources.items(), key=lambda item: len(item[0]), reverse=True
    ):
        remapped = remapped.replace(str(worktree_path), str(source_path))
    return remapped


def _longest_worktree_path_prefix_suffix(text: str, worktree_paths: list[str]) -> int:
    """Length of the longest text suffix that could become a worktree path."""
    max_len = 0
    for path in worktree_paths:
        limit = min(len(text), len(path) - 1)
        for size in range(limit, 0, -1):
            if size <= max_len:
                break
            if text.endswith(path[:size]):
                max_len = size
                break
    return max_len


class _StreamingWorktreePathRemapper:
    def __init__(self, worktree_sources: dict[str, str]):
        self._sources = worktree_sources
        self._paths = sorted(worktree_sources, key=len, reverse=True)
        self._pending = ""

    def feed(self, text: str) -> str:
        if not text or not self._sources:
            return text
        combined = self._pending + text
        keep = _longest_worktree_path_prefix_suffix(combined, self._paths)
        if keep:
            self._pending = combined[-keep:]
            combined = combined[:-keep]
        else:
            self._pending = ""
        return remap_worktree_paths(combined, self._sources)

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        return remap_worktree_paths(pending, self._sources)


def _worktree_blocker_tools(blockers: list[dict]) -> list[dict]:
    tools: list[dict] = []
    for blocker in blockers:
        conflicts = blocker.get("conflicts") if isinstance(blocker.get("conflicts"), list) else []
        files = (
            [{"status": "U", "path": path} for path in conflicts if isinstance(path, str) and path]
            or [{"status": "M", "path": "worktree changes"}]
        )
        tools.append({
            "name": "GitDiff",
            "repo": blocker.get("repo_root"),
            "source": blocker.get("repo_root"),
            "worktree_repo": blocker.get("worktree_path"),
            "worktree_status": blocker.get("status") or "pending",
            "worktree_conflicts": conflicts,
            "integration_worktree_path": blocker.get("integration_worktree_path") or "",
            "worktree_blocker": True,
            "files": files,
            "file_count": len(files),
            "additions": 0,
            "deletions": 0,
            "diff": "",
        })
    return tools


async def _repo_roots_for_code_roots(code_roots: list[str]) -> list[Path]:
    from .worktree import repo_root_for

    repo_roots: list[Path] = []
    seen: set[str] = set()
    for root in code_roots:
        repo_root = await asyncio.to_thread(repo_root_for, root)
        if not repo_root:
            continue
        key = str(repo_root.resolve())
        if key in seen:
            continue
        seen.add(key)
        repo_roots.append(repo_root)
    return repo_roots


async def _setup_worktrees(
    code_roots: list[str],
    cwd: Optional[str],
    topic: str,
    agent: str,
) -> tuple[list[str], Optional[str], bool]:
    """
    Create worktrees for each unique git repo in code_roots and remap paths.
    CWD is kept as the source repo path for session continuity.
    """
    from .stats_db import save_worktree
    from .worktree import (
        repo_root_for,
        ensure_worktree,
        map_to_worktree,
        branch_name,
        base_commit_for,
        integration_worktree_path,
    )

    worktree_map: dict[Path, Path] = {}
    repo_roots: list[Path] = []
    for root in code_roots:
        try:
            repo_root = await asyncio.to_thread(repo_root_for, root)
            if not repo_root:
                log.warning("worktree setup skipped non-git root=%s topic=%s agent=%s", root, topic, agent)
                return code_roots, cwd, False
            if repo_root not in repo_roots:
                repo_roots.append(repo_root)
        except Exception:
            log.exception("worktree setup failed for root=%s topic=%s agent=%s", root, topic, agent)
            return code_roots, cwd, False

    for repo_root in repo_roots:
        try:
            wt_path = await asyncio.to_thread(ensure_worktree, repo_root, topic, agent)
            worktree_map[repo_root] = wt_path
            await asyncio.to_thread(
                save_worktree, topic, agent,
                str(repo_root), str(wt_path), branch_name(topic, agent),
                await asyncio.to_thread(base_commit_for, repo_root, topic, agent),
                str(integration_worktree_path(repo_root, topic, agent)),
                "active",
            )
        except Exception:
            log.exception("worktree setup failed for repo=%s topic=%s agent=%s", repo_root, topic, agent)
            return code_roots, cwd, False

    if not worktree_map or len(worktree_map) != len(repo_roots):
        return code_roots, cwd, False

    effective_roots = [map_to_worktree(r, worktree_map) or r for r in code_roots]
    return effective_roots, cwd, True


@dataclass
class QueueItem:
    seq: int
    topic: str
    agent: Optional[str]
    prompt: str
    context_history: list[dict]
    harness: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    backend: Optional[str] = None
    cwd: Optional[str] = None
    source_cwd: Optional[str] = None
    code_roots: Optional[list[str]] = None
    timeout: Optional[int] = None
    resume_session_id: Optional[str] = None
    adhoc: bool = False
    lookback: int = 0
    msg_id: Optional[int] = None
    display_prompt: Optional[str] = None
    worktree_setup_elapsed_ms: Optional[float] = None
    worktree_isolated: bool = False
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
        """Returns 0 if the item is being processed (or worker is idle), N if N items are ahead.
        Counts actual live queue entries rather than subtracting seq numbers — a
        cancelled/drained item's seq isn't reassigned to the items behind it, so
        seq arithmetic overcounts by however many ahead-of-it items were removed.
        """
        if self._processing_seq is None or seq == self._processing_seq:
            return 0  # worker is idle, or this is the item currently running
        for idx, item in enumerate(self.q._queue):
            if item is not None and item.seq == seq:
                return idx + 1
        return 0

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
                "prompt_preview": (
                    ((it.display_prompt or it.prompt)[:80] + "…")
                    if len(it.display_prompt or it.prompt) > 80
                    else (it.display_prompt or it.prompt)
                ),
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
        from .stats_db import mark_assistant_cancelled
        pending = []
        while not self.q.empty():
            try:
                pending.append(self.q.get_nowait())
            except asyncio.QueueEmpty:
                break

        def _cancel(item):
            if item.msg_id:
                mark_assistant_cancelled(item.msg_id, "Cancelled before start")
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

    def _trigger_chain_continuation(self, msg_id: int) -> None:
        """Fire-and-forget: if msg_id is a step in a Squid Flow route chain
        with more steps to run, dispatch the next one server-side. Runs
        independent of this worker's own queue so it never blocks the next
        queued item. See agent/flow.py."""
        from . import flow

        async def _run():
            await flow.continue_chain(msg_id)

        asyncio.create_task(_run(), name=f"squid-chain-{msg_id}")

    async def _process(self, item: QueueItem):
        from .runners import CLIError, effective_protocol, runner_for_agent
        from .config import SQUID_HOME, WORKTREE_ISOLATION_ENABLED
        from .memory import code_roots_prompt_block, oneshot_protocol_prompt_block
        from .resolve import resolve_agent, split_agent_ref
        from .stats_db import (
            get_completed_run_status_raw,
            get_completed_run_text,
            insert_run_event,
            _utc_now_iso,
            update_assistant_message,
            save_stats,
            set_topic_session,
            clear_topic_session,
            get_worktrees,
        )
        from .git_changes import prepare_trackers

        try:
            harness, provider = split_agent_ref(item.harness or item.backend, item.provider)
            resolved = resolve_agent(harness, provider)
        except ValueError as exc:
            await item.out_q.put({"_error": str(exc)})
            await item.out_q.put(None)
            return
        runner = runner_for_agent(resolved, adhoc=item.adhoc)
        if runner is None:
            await item.out_q.put({"_error": f"Harness {resolved.harness!r} protocol {resolved.protocol!r} is not supported"})
            await item.out_q.put(None)
            return
        try:
            backend_env = resolved.execution_env()
        except ValueError as exc:
            await item.out_q.put({"_error": str(exc)})
            await item.out_q.put(None)
            return

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

        async def _emit_text(text: str):
            nonlocal run_seq, raw
            if not text:
                return
            raw += text
            insert_run_event(item.msg_id, run_seq, "text", text)
            await item.out_q.put(text)
            run_seq += 1

        proc_cwd = item.cwd or SQUID_HOME  # subprocess working dir (may be a worktree path)
        display_cwd = item.source_cwd or proc_cwd  # source repo path for stats/UI
        tracking_roots = item.code_roots or []
        if WORKTREE_ISOLATION_ENABLED and item.agent and item.msg_id and item.code_roots and not item.worktree_isolated:
            repo_roots = await _repo_roots_for_code_roots(item.code_roots)
            if repo_roots:
                from .worktree import settle_worktrees_before_turn

                blockers = await settle_worktrees_before_turn(item.topic, repo_roots)
                if blockers:
                    for tool in _worktree_blocker_tools(blockers):
                        await _emit_tool(tool)
                    blocker_desc = "; ".join(
                        f"{b.get('worktree_path') or b.get('repo_root')} ({b.get('status')})" for b in blockers
                    )
                    err_text = f"Blocked: worktree sync requires attention before starting another turn — {blocker_desc}"
                    update_assistant_message(
                        item.msg_id, err_text, None, "error",
                        context=json.dumps(tool_events) if tool_events else None,
                        status_raw=status_raw,
                        only_if_pending=True,
                    )
                    await item.out_q.put({"_error": err_text})
                    await item.out_q.put(None)
                    return

            setup_started_at = time.perf_counter()
            tracking_roots, proc_cwd, item.worktree_isolated = await _setup_worktrees(
                item.code_roots, item.cwd, item.topic, str(item.msg_id)
            )
            item.worktree_setup_elapsed_ms = (time.perf_counter() - setup_started_at) * 1000
            display_cwd = item.source_cwd or (item.cwd or SQUID_HOME)

        worktree_sources: dict[str, str] = {}
        if item.msg_id:
            try:
                wt_records = await asyncio.to_thread(get_worktrees, item.topic, str(item.msg_id))
                worktree_sources = {
                    rec["worktree_path"]: rec["repo_root"]
                    for rec in wt_records
                }
            except Exception:
                log.debug("worktree lookup skipped topic=%s msg_id=%s", item.topic, item.msg_id, exc_info=True)
        prefix_blocks: list[str] = []
        if effective_protocol(resolved, adhoc=item.adhoc) == "oneshot-cli":
            prefix_blocks.append(oneshot_protocol_prompt_block())
        if tracking_roots:
            code_roots_block = code_roots_prompt_block(
                tracking_roots,
                isolated=item.worktree_isolated,
                topic=item.topic,
                backing_roots=item.code_roots if item.worktree_isolated else None,
            )
            if code_roots_block:
                prefix_blocks.append(code_roots_block)

        effective_prompt = item.prompt
        if prefix_blocks:
            effective_prompt = "\n\n".join(prefix_blocks + [item.prompt])

        git_trackers = await asyncio.to_thread(
            prepare_trackers,
            tracking_roots,
            topic=item.topic,
            agent=item.agent,
            adhoc=item.adhoc,
            msg_id=item.msg_id,
            worktree_sources=worktree_sources,
        )
        effective_cwd = proc_cwd
        kwargs: dict = dict(
            history=item.context_history, model=item.model,
            cwd=effective_cwd,
            topic=item.topic, agent=item.agent or "",
            response_timeout=item.timeout,
            adhoc=item.adhoc, msg_id=item.msg_id,
            prompt_preview=item.display_prompt,
            backend_id=resolved.harness, backend_env=backend_env,
            backend_settings=resolved.harness_settings(), backend_args=resolved.args,
        )
        if resolved.protocol.startswith("interactive-") and not item.adhoc:
            kwargs["interactive_idle_timeout_s"] = resolved.interactive.idle_timeout_seconds
        if item.resume_session_id:
            kwargs["resume_session_id"] = item.resume_session_id

        response_remapper = _StreamingWorktreePathRemapper(worktree_sources)
        worktree_turn_trees: dict[tuple[str, str], str] = {}

        async def _emit_git_diff():
            if not git_trackers:
                return
            emitted = False
            no_change_tool: Optional[dict] = None
            for tracker in git_trackers:
                try:
                    try:
                        tool = await asyncio.to_thread(tracker.build_event)
                        work_tree = getattr(tracker, "work_tree", None)
                        event_repo_root = getattr(tracker, "event_repo_root", None)
                        head_tree = getattr(tracker, "head_tree", None)
                        if work_tree and event_repo_root and work_tree != event_repo_root and head_tree:
                            worktree_turn_trees[
                                (str(event_repo_root.resolve()), str(work_tree.resolve()))
                            ] = head_tree
                        if not tool and no_change_tool is None:
                            no_change_tool = tracker.build_no_change_event()
                    finally:
                        await asyncio.to_thread(tracker.cleanup)
                except Exception:
                    log.exception("Failed to build git diff for msg_id=%s", item.msg_id)
                    continue
                if tool:
                    await _emit_tool(tool)
                    emitted = True
            if not emitted and no_change_tool:
                await _emit_tool(no_change_tool)

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
                            turn_model = enriched.get("model") or item.model
                            save_stats(session_id, enriched, topic=item.topic, agent=item.agent,
                                       harness=resolved.harness, provider=resolved.provider.id,
                                       model=turn_model,
                                       cwd=display_cwd,
                                       lookback=item.lookback)
                            if item.agent and not item.adhoc:
                                set_topic_session(
                                    item.topic, item.agent, session_id,
                                    display_cwd,
                                    resolved.fingerprint,
                                )
                            enriched["session_id"] = session_id
                            enriched["cwd"] = display_cwd
                            enriched["model"] = turn_model
                            enriched["harness"] = resolved.harness
                            enriched["provider"] = resolved.provider.id
                        completed_at = _utc_now_iso()
                        enriched["completed_at"] = completed_at
                        insert_run_event(item.msg_id, run_seq, "stats", json.dumps(enriched), created_at=completed_at)
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
                    elif "_diag" in chunk:
                        insert_run_event(item.msg_id, run_seq, "diag", json.dumps(chunk["_diag"]))
                else:
                    await _emit_text(response_remapper.feed(chunk))
                    continue
                run_seq += 1
            await _emit_text(response_remapper.flush())

        try:
            try:
                await _stream(effective_prompt, **kwargs)
            except CLIError as exc:
                exc_str = str(exc)
                if item.resume_session_id and "No conversation found" in exc_str:
                    status = (
                        f"Session not found — starting fresh\n"
                        f"  session: {item.resume_session_id}\n"
                        f"  cwd: {effective_cwd}\n"
                        f"  harness: {item.harness}"
                        + (f"  model: {item.model}" if item.model else "")
                    )
                    insert_run_event(item.msg_id, run_seq, "status", status)
                    run_seq += 1
                    await item.out_q.put({"_status": status})
                    kwargs.pop("resume_session_id", None)
                    await _stream(effective_prompt, **kwargs)
                elif item.resume_session_id and "prompt is too long" in exc_str.lower():
                    if item.agent:
                        clear_topic_session(item.topic, item.agent)
                    status = (
                        f"Session context window exceeded — starting fresh\n"
                        f"  session: {item.resume_session_id}\n"
                        f"  cwd: {effective_cwd}\n"
                        f"  harness: {item.harness}"
                        + (f"  model: {item.model}" if item.model else "")
                    )
                    insert_run_event(item.msg_id, run_seq, "status", status)
                    run_seq += 1
                    await item.out_q.put({"_status": status})
                    kwargs.pop("resume_session_id", None)
                    await _stream(effective_prompt, **kwargs)
                else:
                    raise

            if raw.strip().lower() == "prompt is too long" and item.agent and not item.adhoc:
                clear_topic_session(item.topic, item.agent)

            await _emit_git_diff()

            worktree_sync_elapsed_ms: Optional[float] = None
            worktree_sync_statuses: list[str] = []
            worktree_sync_repo_count = 0
            if item.agent and item.msg_id and item.worktree_isolated:
                wt_key = str(item.msg_id)
                from .stats_db import mark_worktree_synced, mark_worktree_status
                from .worktree import sync_after_turn
                sync_started_at = time.perf_counter() if item.worktree_setup_elapsed_ms is not None else None
                try:
                    wt_records = await asyncio.to_thread(get_worktrees, item.topic, wt_key)
                except Exception:
                    wt_records = []
                    log.debug("worktree sync lookup skipped topic=%s msg_id=%s", item.topic, item.msg_id, exc_info=True)
                worktree_sync_repo_count = len(wt_records)
                for rec in wt_records:
                    repo_root = rec["repo_root"]
                    worktree_repo = rec["worktree_path"]
                    sync_status = "pending"
                    try:
                        turn_tree = worktree_turn_trees.get(
                            (str(Path(repo_root).resolve()), str(Path(worktree_repo).resolve()))
                        )
                        conflicts = await asyncio.to_thread(
                            sync_after_turn, Path(repo_root), item.topic, wt_key, item.msg_id,
                            request_text=(item.display_prompt or item.prompt),
                            response_text=raw,
                            turn_tree=turn_tree,
                        )
                        if conflicts:
                            log.warning("worktree merge conflicts after turn msg_id=%s: %s", item.msg_id, conflicts)
                            sync_status = "conflict"
                            await asyncio.to_thread(mark_worktree_status, item.topic, wt_key, repo_root, sync_status)
                            await _emit_tool({
                                "name": "WorktreeSync",
                                "status": sync_status,
                                "repo": repo_root,
                                "worktree_repo": worktree_repo,
                                "integration_worktree_path": rec.get("integration_worktree_path"),
                                "conflicts": conflicts,
                            })
                        else:
                            # Merge is done, so the source repo is current — but the worktree
                            # dir itself is left in place. A tool the turn ran may have spawned
                            # a background process still using it as cwd; removal happens later
                            # via worktree.cleanup_worktrees (see TopicDispatcher.dispatch).
                            sync_status = "synced"
                            await asyncio.to_thread(mark_worktree_synced, item.topic, wt_key, repo_root)
                            await _emit_tool({
                                "name": "WorktreeSync",
                                "status": sync_status,
                                "repo": repo_root,
                                "worktree_repo": worktree_repo,
                            })
                    except Exception:
                        sync_status = "promotion_failed"
                        await asyncio.to_thread(mark_worktree_status, item.topic, wt_key, repo_root, sync_status)
                        await _emit_tool({
                            "name": "WorktreeSync",
                            "status": sync_status,
                            "repo": repo_root,
                            "worktree_repo": worktree_repo,
                            "integration_worktree_path": rec.get("integration_worktree_path"),
                        })
                        log.exception("worktree sync failed after turn msg_id=%s", item.msg_id)
                    finally:
                        worktree_sync_statuses.append(sync_status)
                        for tool in tool_events:
                            if tool.get("name") == "GitDiff" and tool.get("worktree_repo") == worktree_repo:
                                tool["worktree_status"] = sync_status
                                if rec.get("integration_worktree_path"):
                                    tool["integration_worktree_path"] = rec.get("integration_worktree_path")
                if sync_started_at is not None:
                    worktree_sync_elapsed_ms = (time.perf_counter() - sync_started_at) * 1000

            if item.worktree_setup_elapsed_ms is not None:
                log.info(
                    "worktree turn timing topic=%s agent=%s msg_id=%s isolated=%s repos=%d setup_ms=%.1f sync_ms=%.1f statuses=%s",
                    item.topic, item.agent, item.msg_id, item.worktree_isolated,
                    worktree_sync_repo_count, item.worktree_setup_elapsed_ms,
                    worktree_sync_elapsed_ms or 0.0,
                    ",".join(worktree_sync_statuses) if worktree_sync_statuses else "-",
                )

            content = raw
            context_json = json.dumps(tool_events) if tool_events else None
            update_assistant_message(item.msg_id, content, session_id,
                                     "done" if content else "error", context=context_json,
                                     status_raw=status_raw, only_if_pending=True)
            insert_run_event(item.msg_id, run_seq, "done", None)
            if content and item.msg_id:
                self._trigger_chain_continuation(item.msg_id)

        except Exception as exc:
            err_text = str(exc)
            if not isinstance(exc, CLIError):
                log.exception("Unexpected error processing msg_id=%s", item.msg_id)
            recovered_content = get_completed_run_text(item.msg_id) if not raw else None
            content = raw or recovered_content or err_text
            try:
                await _emit_git_diff()
            except Exception:
                log.exception("Failed to emit git diff for msg_id=%s", item.msg_id)
            context_json = json.dumps(tool_events) if tool_events else None
            try:
                update_assistant_message(item.msg_id, content, session_id,
                                         "done" if raw or recovered_content else "error", context=context_json,
                                         status_raw=status_raw or get_completed_run_status_raw(item.msg_id),
                                         only_if_pending=True)
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

    def _sweep_worktrees(self, topic: str) -> None:
        """Best-effort, non-blocking sweep of worktrees left over from prior
        turns of this topic. Never delays admitting the new turn."""
        from .config import WORKTREE_ISOLATION_ENABLED
        if not WORKTREE_ISOLATION_ENABLED:
            return

        async def _run():
            from .worktree import cleanup_worktrees
            try:
                await cleanup_worktrees(topic)
            except Exception:
                log.exception("worktree sweep failed for topic=%s", topic)
        asyncio.create_task(_run(), name=f"squid-worktree-sweep-{topic}")

    async def dispatch(
        self,
        topic: str,
        prompt: str,
        context_history: list[dict],
        harness: Optional[str] = None,
        provider: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
        cwd: Optional[str] = None,
        source_cwd: Optional[str] = None,
        response_timeout: Optional[int] = None,
        resume_session_id: Optional[str] = None,
        adhoc: bool = False,
        lookback: int = 0,
        msg_id: Optional[int] = None,
        code_roots: Optional[list[str]] = None,
        display_prompt: Optional[str] = None,
        worktree_setup_elapsed_ms: Optional[float] = None,
        worktree_isolated: bool = False,
    ) -> tuple[asyncio.Queue, int, TopicWorker]:
        if adhoc:
            # Each adhoc message gets its own ephemeral worker — never queued, always parallel.
            self._adhoc_counter += 1
            queue_key = f"__adhoc_{self._adhoc_counter}"
        else:
            queue_key = f"{topic}@{agent}" if agent else topic
        worker = self._get_or_create(queue_key, topic)
        self._sweep_worktrees(topic)
        from .resolve import split_agent_ref
        resolved_harness, resolved_provider = split_agent_ref(harness or backend, provider)
        item = QueueItem(
            seq=0, topic=topic, agent=agent,
            prompt=prompt, display_prompt=display_prompt, context_history=context_history,
            harness=resolved_harness, provider=resolved_provider, backend=backend,
            model=model, cwd=cwd, source_cwd=source_cwd,
            code_roots=code_roots, timeout=response_timeout,
            resume_session_id=resume_session_id,
            adhoc=adhoc, lookback=lookback, msg_id=msg_id,
            worktree_setup_elapsed_ms=worktree_setup_elapsed_ms,
            worktree_isolated=worktree_isolated,
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
        from .runners import active_msg_ids_by_topic, kill_procs_by_topic
        from .stats_db import mark_assistant_cancelled
        for msg_id in active_msg_ids_by_topic(topic, agent=agent, adhoc=adhoc, lifo=(adhoc is True)):
            mark_assistant_cancelled(msg_id, "Cancelled")
        return kill_procs_by_topic(topic, agent=agent, adhoc=adhoc, lifo=(adhoc is True))

    def stopall_topic(self, topic: str, agent: Optional[str] = None,
                      adhoc: Optional[bool] = None) -> dict:
        """Kill running processes + drain queues for topic (scoped by agent/adhoc)."""
        from .runners import active_msg_ids_by_topic, kill_procs_by_topic
        from .stats_db import mark_assistant_cancelled
        for msg_id in active_msg_ids_by_topic(topic, agent=agent, adhoc=adhoc):
            mark_assistant_cancelled(msg_id, "Cancelled")
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
