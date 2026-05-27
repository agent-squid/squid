"""
runners.py — spawn claude or codex CLI and stream their output.

Yields text strings for content chunks, and a single dict {"_stats": {...}}
as the last item when usage data is available (claude only).
"""

import asyncio
import json
import os
import time
from typing import AsyncGenerator, List, Optional, Union

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH, FIRST_BYTE_TIMEOUT, RESPONSE_TIMEOUT

# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------

_proc_registry: dict[int, dict] = {}


def _register_proc(pid: int, backend: str, topic: str, alias: str) -> None:
    _proc_registry[pid] = {
        "pid": pid,
        "backend": backend,
        "topic": topic,
        "alias": alias,
        "started_at": time.monotonic(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _deregister_proc(pid: int) -> None:
    _proc_registry.pop(pid, None)


def kill_procs_by_topic(topic: str) -> int:
    """Send SIGTERM to all subprocesses registered under topic. Returns kill count."""
    import signal
    killed = 0
    for pid, info in list(_proc_registry.items()):
        if info.get("topic") == topic:
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
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
    alias: str = "",
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
    )

    assert proc.stdout is not None
    assert proc.stderr is not None
    pid = proc.pid
    _register_proc(pid, backend=backend, topic=topic, alias=alias)

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

    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                proc.kill()
                raise CLIError("Response timeout exceeded")
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(FIRST_BYTE_TIMEOUT, remaining),
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise CLIError("Timed out waiting for CLI response")
            if not line:
                break
            yield line.decode(errors="replace").rstrip("\n")
    finally:
        _deregister_proc(pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
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


async def run_claude(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="claude", topic=topic, alias=alias, response_timeout=response_timeout):
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
            if (
                inner.get("type") == "content_block_delta"
                and inner.get("delta", {}).get("type") == "text_delta"
            ):
                text = inner["delta"].get("text", "")
                if text:
                    streamed_text = True
                    yield text  # stream directly as response content, not status

        elif t == "result":
            # Skip final_text if we already streamed it chunk by chunk above
            if not streamed_text:
                final_text = event.get("result", "")
                if final_text:
                    yield final_text
            usage = event.get("usage", {})
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
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="codex", topic=topic, alias=alias, response_timeout=response_timeout):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = event.get("type", "")
        if t == "thread.started":
            thread_id = event.get("thread_id")
        elif t == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    yield str(text)
        elif t == "turn.completed":
            usage = event.get("usage", {})
            yield {
                "_stats": {
                    "session_id": thread_id,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cached_input_tokens", 0),
                    "cache_write_tokens": 0,
                    "reasoning_tokens": usage.get("reasoning_output_tokens", 0),
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": None,
                    "duration_ms": int(time.monotonic() * 1000 - start_ms),
                }
            }
        elif t == "error":
            raise CLIError(event.get("message", "codex error"))


async def run_copilot(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="copilot", topic=topic, alias=alias, response_timeout=response_timeout):
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
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="cursor", topic=topic, alias=alias, response_timeout=response_timeout):
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
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="antigravity", topic=topic, alias=alias, response_timeout=response_timeout):
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
            if (
                inner.get("type") == "content_block_delta"
                and inner.get("delta", {}).get("type") == "text_delta"
            ):
                text = inner["delta"].get("text", "")
                if text:
                    streamed_text = True
                    yield text  # stream directly as response content, not status

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


async def run_auto(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", alias: str = "",
    response_timeout: Optional[int] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Try claude → cursor → antigravity → codex → copilot in order of availability."""
    if CLAUDE_PATH:
        async for chunk in run_claude(prompt, cwd=cwd, history=history, model=model, topic=topic, alias=alias, response_timeout=response_timeout):
            yield chunk
    elif CURSOR_PATH:
        async for chunk in run_cursor(prompt, cwd=cwd, history=history, model=model, topic=topic, alias=alias, response_timeout=response_timeout):
            yield chunk
    elif AGY_PATH:
        async for chunk in run_antigravity(prompt, cwd=cwd, history=history, model=model, topic=topic, alias=alias, response_timeout=response_timeout):
            yield chunk
    elif CODEX_PATH:
        async for chunk in run_codex(prompt, cwd=cwd, history=history, model=model, topic=topic, alias=alias, response_timeout=response_timeout):
            yield chunk
    elif COPILOT_PATH:
        async for chunk in run_copilot(prompt, cwd=cwd, history=history, model=model, topic=topic, alias=alias, response_timeout=response_timeout):
            yield chunk
    else:
        raise CLINotFoundError("No AI CLI found in PATH (claude, cursor-agent, agy, codex, or copilot)")
