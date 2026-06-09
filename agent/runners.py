"""
runners.py — spawn claude or codex CLI and stream their output.

Yields text strings for content chunks, and a single dict {"_stats": {...}}
as the last item when usage data is available (claude only).
"""

import asyncio
import json
import os
import signal
import time
from typing import AsyncGenerator, List, Optional, Union

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH, FIRST_BYTE_TIMEOUT, RESPONSE_TIMEOUT

# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------

_proc_registry: dict[int, dict] = {}


def _register_proc(pid: int, backend: str, topic: str, agent: str,
                   adhoc: bool = False, msg_id: Optional[int] = None) -> None:
    _proc_registry[pid] = {
        "pid": pid,
        "backend": backend,
        "topic": topic,
        "agent": agent,
        "adhoc": adhoc,
        "msg_id": msg_id,
        "started_at": time.monotonic(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _deregister_proc(pid: int) -> None:
    _proc_registry.pop(pid, None)


def _signal_process_group(pid: int, sig: signal.Signals) -> bool:
    """Signal the CLI process group; fall back to the parent PID if needed."""
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def kill_procs_by_topic(topic: str, agent: Optional[str] = None,
                        adhoc: Optional[bool] = None, lifo: bool = False) -> int:
    """Send SIGTERM to subprocesses matching topic + optional agent/adhoc filters.

    lifo=True: kill only the most recently started matching process (for adhoc stop).
    lifo=False: kill all matching processes.
    """
    matching = [
        (pid, info) for pid, info in list(_proc_registry.items())
        if info.get("topic") == topic
        and (agent is None or info.get("agent") == agent)
        and (adhoc is None or bool(info.get("adhoc")) == adhoc)
    ]
    if not matching:
        return 0
    if lifo:
        matching = [max(matching, key=lambda x: x[1]["started_at"])]
    killed = 0
    for pid, _ in matching:
        _deregister_proc(pid)  # remove immediately so a second LIFO call skips it
        if _signal_process_group(pid, signal.SIGTERM):
            killed += 1
    return killed


def kill_proc_by_msg_id(msg_id: int) -> int:
    """Send SIGTERM to the process registered with msg_id. Returns 1 if killed, 0 if not found."""
    for pid, info in list(_proc_registry.items()):
        if info.get("msg_id") == msg_id:
            _deregister_proc(pid)
            if _signal_process_group(pid, signal.SIGTERM):
                return 1
            return 0
    return 0


def kill_all_procs() -> int:
    """Send SIGTERM to all tracked subprocesses. Returns kill count."""
    killed = 0
    for pid in list(_proc_registry):
        if _signal_process_group(pid, signal.SIGTERM):
            killed += 1
    return killed


def list_active_procs() -> list[dict]:
    now = time.monotonic()
    return [
        {**info, "duration_s": round(now - info["started_at"], 1)}
        for info in list(_proc_registry.values())
    ]


class CLINotFoundError(RuntimeError):
    pass


class CLIError(RuntimeError):
    pass


async def _stream_lines(
    cmd: List[str],
    cwd: Optional[str] = None,
    *,
    backend: str = "",
    topic: str = "",
    agent: str = "",
    adhoc: bool = False,
    msg_id: Optional[int] = None,
    response_timeout: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """Run cmd and yield stdout line by line.

    Drains stderr concurrently to prevent the 64KB pipe buffer from filling
    and blocking the subprocess before it can write to stdout.
    """
    env = os.environ.copy()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
        limit=8 * 1024 * 1024,  # 8 MB — default 64 KB overflows on long Claude responses
    )

    assert proc.stdout is not None
    assert proc.stderr is not None
    pid = proc.pid
    _register_proc(pid, backend=backend, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id)

    # Drain stderr concurrently — prevents buffer-full deadlock if the
    # subprocess writes > 64KB of diagnostics before exiting.
    stderr_buf: list[bytes] = []

    async def _drain() -> None:
        try:
            while chunk := await proc.stderr.read(4096):
                stderr_buf.append(chunk)
        except Exception:
            pass

    drain_task = asyncio.create_task(_drain(), name=f"stderr-drain-{pid}")
    timeout = response_timeout if response_timeout is not None else RESPONSE_TIMEOUT
    deadline = asyncio.get_event_loop().time() + timeout
    first_byte = True

    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                proc.kill()
                raise CLIError("Response timeout exceeded")
            # FIRST_BYTE_TIMEOUT guards only the initial response; subsequent lines
            # use the full remaining deadline so agentic multi-turn runs aren't cut
            # off mid-stream while the CLI is processing a slow tool call.
            per_line = min(FIRST_BYTE_TIMEOUT if first_byte else remaining, remaining)
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=per_line,
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise CLIError("Timed out waiting for CLI response")
            if not line:
                break
            first_byte = False
            yield line.decode(errors="replace").rstrip("\n")
    finally:
        _deregister_proc(pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            if not _signal_process_group(pid, signal.SIGKILL):
                proc.kill()
            await proc.wait()
        drain_task.cancel()

    if proc.returncode != 0:
        err = b"".join(stderr_buf).decode(errors="replace").strip()
        raise CLIError(f"CLI exited {proc.returncode}: {err}")


def _build_prompt(prompt: str, history: Optional[List[dict]]) -> str:
    if not history:
        return prompt
    lines = [
        "The following is the conversation so far. Continue it naturally.\n",
        "<conversation_history>",
    ]
    for msg in history:
        if msg.get("role") == "user":
            lines.append(f"User: {msg.get('content', '').strip()}")
        else:
            label = msg.get("model") or msg.get("backend") or "Assistant"
            lines.append(f"Assistant ({label}): {msg.get('content', '').strip()}")
    lines.append("</conversation_history>")
    lines.append("")
    lines.append(f"User: {prompt}")
    return "\n".join(lines)


def _estimate_history_tokens(history: Optional[List[dict]]) -> int:
    """Rough token estimate for injected history (4 chars ≈ 1 token)."""
    if not history:
        return 0
    total_chars = sum(len(m.get("content", "")) for m in history)
    return total_chars // 4


def _tool_data(name: str, input_json: str) -> dict:
    """Parse a completed tool call into structured data for the client to render."""
    try:
        inp = json.loads(input_json) if input_json.strip() else {}
    except json.JSONDecodeError:
        inp = {}
    base: dict = {"name": name}
    if name == "Edit":
        return {**base, "file": inp.get("file_path", ""),
                "old": inp.get("old_string", ""), "new": inp.get("new_string", "")}
    if name == "MultiEdit":
        return {**base, "file": inp.get("file_path", ""), "edits": inp.get("edits", [])}
    if name == "Write":
        return {**base, "file": inp.get("file_path", ""), "content": inp.get("content", "")}
    if name == "Read":
        return {**base, "file": inp.get("file_path", "")}
    if name == "Bash":
        return {**base, "command": inp.get("command", "")}
    if name == "Agent":
        return {**base, "description": (inp.get("description") or inp.get("prompt") or "")[:300]}
    if name in ("WebFetch", "WebSearch"):
        return {**base, "query": inp.get("url", inp.get("query", ""))}
    if name == "TodoWrite":
        return {**base, "todos": inp.get("todos", [])}
    if inp:
        k, v = next(iter(inp.items()))
        return {**base, "key": k, "value": str(v)[:300]}
    return base


def _codex_diff_tool(payload: dict) -> Optional[dict]:
    """Extract a Codex file-change event into a UI diff tool when possible."""
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    diff = (
        item.get("unified_diff")
        or item.get("unifiedDiff")
        or item.get("diff")
        or payload.get("unified_diff")
        or payload.get("unifiedDiff")
        or payload.get("diff")
    )
    if not diff:
        return None
    file_path = (
        item.get("path")
        or item.get("file")
        or item.get("file_path")
        or item.get("filePath")
        or payload.get("path")
        or payload.get("file")
        or payload.get("file_path")
        or payload.get("filePath")
        or ""
    )
    return {"name": "Diff", "file": file_path, "diff": str(diff)}


async def run_claude(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from claude CLI, then yield a stats dict."""
    if not CLAUDE_PATH:
        raise CLINotFoundError(
            "claude CLI not found in PATH. Install with: npm install -g @anthropic-ai/claude-code"
        )

    cmd = [
        CLAUDE_PATH, "--print",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd += ["--model", model]

    if resume_session_id:
        cmd += ["--resume", resume_session_id]
        cmd.append(prompt)
    else:
        cmd.append(_build_prompt(prompt, history))

    session_id: Optional[str] = None
    streamed_text = False  # track whether any text chunks were streamed as content
    tool_blocks: dict[int, dict] = {}  # index -> {name, input_json}

    async for line in _stream_lines(cmd, cwd=cwd, backend="claude", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = event.get("type")

        if t == "system":
            session_id = event.get("session_id")

        elif t == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")
            idx = inner.get("index", 0)

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_blocks[idx] = {"name": block.get("name", ""), "input_json": ""}

            elif inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        streamed_text = True
                        yield text  # stream directly as response content, not status
                elif dtype == "input_json_delta" and idx in tool_blocks:
                    tool_blocks[idx]["input_json"] += delta.get("partial_json", "")

            elif inner_type == "content_block_stop" and idx in tool_blocks:
                block = tool_blocks.pop(idx)
                yield {"_tool": _tool_data(block["name"], block["input_json"])}

        elif t == "result":
            # Skip final_text if we already streamed it chunk by chunk above
            if not streamed_text:
                final_text = event.get("result", "")
                if final_text:
                    yield final_text
            usage = event.get("usage", {})
            # ── Claude token semantics (verified via stream-json output, 2026-06) ──────────
            # The Anthropic API / Claude Code CLI splits input into THREE buckets:
            #
            #   input_tokens               → tiny uncacheable residual (~2–4 tokens).
            #                                The user's actual message is NOT here.
            #   cache_creation_input_tokens → tokens written to the prompt cache this turn,
            #                                INCLUDING the user message. This is where the
            #                                bulk of "new" content lives.
            #   cache_read_input_tokens     → tokens served from a previous cache entry.
            #
            # True total processed this turn = input + cache_creation + cache_read.
            #
            # This is counter-intuitive: input_tokens alone (2–4) looks like a bug but it
            # is correct. We have gone back and forth on this — do not "fix" it by treating
            # input_tokens as the full user message count.
            #
            # Codex is the OPPOSITE: input_tokens = full total (cache already included);
            # cache_read_tokens is a subset breakdown. See run_codex() for that path.
            # ─────────────────────────────────────────────────────────────────────────────
            yield {
                "_stats": {
                    "session_id": session_id,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                }
            }


async def run_codex(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream a response from codex CLI using non-interactive exec mode."""
    if not CODEX_PATH:
        raise CLINotFoundError(
            "codex CLI not found in PATH. Install with: npm install -g @openai/codex"
        )
    if resume_session_id:
        cmd = [CODEX_PATH, "exec", "resume", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd += ["--model", model]
        cmd += [resume_session_id, prompt]
    else:
        cmd = [CODEX_PATH, "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd += ["--model", model]
        cmd.append(_build_prompt(prompt, history))

    start_ms = time.monotonic() * 1000
    thread_id: Optional[str] = None

    async for line in _stream_lines(cmd, cwd=cwd, backend="codex", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = event.get("type", "")
        method = event.get("method", "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if t == "thread.started":
            thread_id = event.get("thread_id")
        elif t == "item.completed" or method == "item/completed":
            item = event.get("item") or params.get("item") or {}
            if not isinstance(item, dict):
                item = {}
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    yield str(text)
            elif item.get("type") == "command_execution":
                cmd_str = item.get("command", "")
                # Strip /bin/zsh -lc "..." or /bin/bash -lc "..." wrapper
                if cmd_str and " -lc " in cmd_str:
                    inner = cmd_str.split(" -lc ", 1)[-1].strip()
                    if len(inner) >= 2 and inner[0] == inner[-1] == '"':
                        cmd_str = inner[1:-1].replace('\\"', '"')
                if cmd_str:
                    yield {"_tool": {"name": "Bash", "command": cmd_str}}
            diff_tool = _codex_diff_tool(item)
            if diff_tool:
                yield {"_tool": diff_tool}
        elif method in ("item/fileChange/patchUpdated", "turn/diff/updated"):
            diff_tool = _codex_diff_tool(params)
            if diff_tool:
                yield {"_tool": diff_tool}
        elif t == "turn.completed" or method == "turn/completed":
            usage = event.get("usage") or params.get("usage") or {}
            # ── Codex token semantics (opposite of Claude — do not conflate) ───────────────
            # Codex reports input_tokens as the FULL total, cache already included.
            # cached_input_tokens is a subset breakdown of input_tokens, not additive.
            # True total = input_tokens (do NOT add cache_read on top).
            # ─────────────────────────────────────────────────────────────────────────────
            total_in = int(usage.get("input_tokens", 0) or 0)
            cached_in = int(usage.get("cached_input_tokens", 0) or 0)
            # output_tokens already includes reasoning_output_tokens — no need to track separately.
            yield {
                "_stats": {
                    "session_id": thread_id,
                    "input_tokens": total_in,
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "cache_read_tokens": cached_in,
                    "cache_write_tokens": 0,
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": None,
                    "duration_ms": int(time.monotonic() * 1000 - start_ms),
                }
            }
        elif t == "error":
            raise CLIError(event.get("message", "codex error"))


async def run_copilot(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream a response from GitHub Copilot CLI using one-shot -p mode with JSONL output."""
    if not COPILOT_PATH:
        raise CLINotFoundError(
            "copilot CLI not found in PATH. Install with: brew install gh-copilot"
        )
    cmd = [
        COPILOT_PATH,
        "-p", prompt if resume_session_id else _build_prompt(prompt, history),
        "--allow-all-tools",
        "--output-format", "json",
    ]
    if resume_session_id:
        cmd.append(f"--resume={resume_session_id}")
    if model:
        cmd += ["--model", model]

    start_ms = time.monotonic() * 1000
    session_id: Optional[str] = None
    response_text: Optional[str] = None
    output_tokens: int = 0
    stats_yielded = False
    session_error: Optional[str] = None

    async for line in _stream_lines(cmd, cwd=cwd, backend="copilot", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout):
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type", "")
        data = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}

        if t == "assistant.message":
            # Full response text (non-ephemeral, arrives after deltas)
            response_text = data.get("content", "")
            output_tokens = data.get("outputTokens", 0)

        elif t == "session.error":
            session_error = data.get("message") or data.get("errorType") or "Unknown error"

        elif t == "result":
            exit_code = ev.get("exitCode", 0)
            if exit_code != 0 and not response_text:
                raise CLIError(f"copilot: {session_error or 'CLI exited with no output'}")
            # Final event — yield response + stats
            if response_text:
                yield response_text
            usage = ev.get("usage", {}) or {}
            yield {
                "_stats": {
                    "session_id": ev.get("sessionId"),
                    "input_tokens": 0,  # not exposed by copilot CLI
                    "output_tokens": output_tokens,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": None,
                    "duration_ms": usage.get("totalApiDurationMs") or int(time.monotonic() * 1000 - start_ms),
                }
            }
            stats_yielded = True
            return

    # Stream ended without a result event
    if response_text:
        yield response_text
    if not stats_yielded:
        yield {
            "_stats": {
                "session_id": session_id,
                "input_tokens": 0,
                "output_tokens": output_tokens,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "history_input_tokens": _estimate_history_tokens(history),
                "cost_usd": None,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }
        }


async def run_cursor(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from cursor-agent CLI, then yield a stats dict."""
    if not CURSOR_PATH:
        raise CLINotFoundError(
            "cursor-agent CLI not found in PATH. Install from https://cursor.com"
        )

    cmd = [
        CURSOR_PATH, "--print",
        "--output-format", "stream-json",
        "--stream-partial-output",
        "--trust",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt if resume_session_id else _build_prompt(prompt, history))

    session_id: Optional[str] = None
    text_started = False

    async for line in _stream_lines(cmd, cwd=cwd, backend="cursor", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = event.get("type")

        if t == "system":
            session_id = event.get("session_id")

        elif t == "thinking" and event.get("subtype") == "delta":
            text = event.get("text", "")
            if text:
                yield {"_status": text}

        elif t == "assistant" and "timestamp_ms" in event:
            # Streaming text delta (has timestamp_ms); final full message does not
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        text_started = True
                        yield text

        elif t == "result":
            usage = event.get("usage") or {}
            yield {
                "_stats": {
                    "session_id": session_id or event.get("session_id"),
                    "input_tokens": usage.get("inputTokens", 0),
                    "output_tokens": usage.get("outputTokens", 0),
                    "cache_read_tokens": usage.get("cacheReadTokens", 0),
                    "cache_write_tokens": usage.get("cacheWriteTokens", 0),
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": None,
                    "duration_ms": event.get("duration_ms"),
                }
            }


async def run_antigravity(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from agy (Google Antigravity) CLI, then yield a stats dict."""
    if not AGY_PATH:
        raise CLINotFoundError(
            "agy CLI not found in PATH. Install from https://antigravity.google"
        )

    cmd = [AGY_PATH, "--output-format", "stream-json"]
    if resume_session_id:
        cmd += ["--conversation", resume_session_id]
    if model:
        cmd += ["-m", model]
    cmd += ["-p", prompt if resume_session_id else _build_prompt(prompt, history)]

    start_ms = time.monotonic() * 1000
    session_id: Optional[str] = None
    stats_yielded = False
    streamed_text = False
    tool_blocks: dict[int, dict] = {}

    async for line in _stream_lines(cmd, cwd=cwd, backend="antigravity", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            yield line
            continue

        t = event.get("type", "")

        if t == "system":
            session_id = event.get("session_id")

        elif t == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")
            idx = inner.get("index", 0)

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_blocks[idx] = {"name": block.get("name", ""), "input_json": ""}

            elif inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        streamed_text = True
                        yield text
                elif dtype == "input_json_delta" and idx in tool_blocks:
                    tool_blocks[idx]["input_json"] += delta.get("partial_json", "")

            elif inner_type == "content_block_stop" and idx in tool_blocks:
                block = tool_blocks.pop(idx)
                yield {"_tool": _tool_data(block["name"], block["input_json"])}

        elif t == "result":
            if not streamed_text:
                final_text = event.get("result", "")
                if final_text:
                    yield final_text
            usage = event.get("usage", {})
            yield {
                "_stats": {
                    "session_id": session_id or event.get("session_id"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms") or int(time.monotonic() * 1000 - start_ms),
                }
            }
            stats_yielded = True
            return

    if not stats_yielded:
        yield {
            "_stats": {
                "session_id": session_id,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "history_input_tokens": _estimate_history_tokens(history),
                "cost_usd": None,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }
        }
