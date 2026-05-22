"""
runners.py — spawn claude or codex CLI and stream their output.

Yields text strings for content chunks, and a single dict {"_stats": {...}}
as the last item when usage data is available (claude only).
"""

import asyncio
import json
import os
from typing import AsyncGenerator, List, Optional, Union

from .config import CLAUDE_PATH, CODEX_PATH, FIRST_BYTE_TIMEOUT, RESPONSE_TIMEOUT


class CLINotFoundError(RuntimeError):
    pass


class CLIError(RuntimeError):
    pass


async def _stream_lines(
    cmd: List[str],
    cwd: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Run cmd and yield stdout line by line."""
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
    deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT

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
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        assert proc.stderr is not None
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        raise CLIError(f"CLI exited {proc.returncode}: {err}")


def _build_prompt(prompt: str, history: Optional[List[dict]]) -> str:
    if not history:
        return prompt
    lines = [
        "The following is the conversation so far. Continue it naturally.\n",
        "<conversation_history>",
    ]
    for msg in history:
        role = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {msg.get('content', '').strip()}")
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
        _build_prompt(prompt, history),
    ]

    session_id: Optional[str] = None

    async for line in _stream_lines(cmd, cwd=cwd):
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
                    yield {"_status": text}

        elif t == "result":
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
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream a response from codex CLI using non-interactive exec mode."""
    if not CODEX_PATH:
        raise CLINotFoundError(
            "codex CLI not found in PATH. Install with: npm install -g @openai/codex"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise CLIError(
            "OPENAI_API_KEY is not set. Export it before starting the server."
        )
    cmd = [CODEX_PATH, "exec", "--json", _build_prompt(prompt, history)]
    async for line in _stream_lines(cmd, cwd=cwd):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = event.get("type", "")
        if t == "agent.message" or t == "message":
            text = event.get("content") or event.get("text") or event.get("message", "")
            if text:
                yield str(text)
        elif t == "error":
            raise CLIError(event.get("message", "codex error"))


async def run_auto(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Try claude first, fall back to codex."""
    if CLAUDE_PATH:
        async for chunk in run_claude(prompt, cwd=cwd, history=history):
            yield chunk
    elif CODEX_PATH:
        async for chunk in run_codex(prompt, cwd=cwd, history=history):
            yield chunk
    else:
        raise CLINotFoundError("Neither claude nor codex CLI found in PATH")
