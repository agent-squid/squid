"""
runners.py — spawn claude or codex CLI and stream their output.

Yields text strings for content chunks, and a single dict {"_stats": {...}}
as the last item when usage data is available (claude only).
"""

import asyncio
import json
import os
import signal
import subprocess
import time
from typing import AsyncGenerator, List, Optional, Union

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH, OPENCODE_PATH, FIRST_BYTE_TIMEOUT, RESPONSE_TIMEOUT, PROXY_ENV

# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------

_proc_registry: dict[int, dict] = {}


def _register_proc(pid: int, backend: str, topic: str, agent: str,
                   adhoc: bool = False, msg_id: Optional[int] = None,
                   prompt: str = "", state: str = "running") -> None:
    preview = (prompt[:80] + "…") if len(prompt) > 80 else prompt
    now = time.monotonic()
    _proc_registry[pid] = {
        "pid": pid,
        "backend": backend,
        "topic": topic,
        "agent": agent,
        "adhoc": adhoc,
        "msg_id": msg_id,
        "prompt_preview": preview,
        "state": state,
        "started_at": now,
        "state_since": now,
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _deregister_proc(pid: int) -> None:
    _proc_registry.pop(pid, None)


def _update_proc(pid: int, **updates) -> None:
    if pid in _proc_registry:
        if "state" in updates and updates["state"] != _proc_registry[pid].get("state"):
            updates.setdefault("state_since", time.monotonic())
        _proc_registry[pid].update(updates)


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


def get_active_agent_for_topic(topic: str) -> Optional[str]:
    """Return the agent of an active non-adhoc process for the topic, or None."""
    for info in _proc_registry.values():
        if info.get("topic") == topic and not info.get("adhoc"):
            return info.get("agent")
    return None


def list_active_procs() -> list[dict]:
    now = time.monotonic()
    return [
        {
            **info,
            "duration_s": round(now - info["started_at"], 1),
            "state_duration_s": round(now - info.get("state_since", info["started_at"]), 1),
        }
        for info in list(_proc_registry.values())
    ]


class CLINotFoundError(RuntimeError):
    pass


class CLIError(RuntimeError):
    pass


def _claude_child_env(backend_id: str, backend_env: Optional[dict]) -> dict:
    env_for_claude = dict(backend_env) if backend_env else {}
    if backend_id == "claude":
        # Claude Code owns its OAuth credentials and refresh lifecycle. Inherited
        # API/gateway variables take precedence over its claude.ai login, so remove
        # them only for the native backend. Gateway backends such as deepseek keep
        # their explicitly resolved child-process environment.
        for name in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
            "SQUID_NATIVE_CLAUDE_TOKEN",
        ):
            env_for_claude[name] = None
    return env_for_claude


def _claude_replayed_user_content(event: dict) -> Optional[str]:
    if event.get("type") != "user" or not event.get("isReplay"):
        return None
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        if parts:
            return "".join(parts)
    return None


def _xmlish_tag_value(text: str, tag: str) -> Optional[str]:
    start = f"<{tag}>"
    end = f"</{tag}>"
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    j = text.find(end, i)
    if j < 0:
        return None
    return text[i:j]


def _claude_task_notification_tool_use_id(content: Optional[str]) -> Optional[str]:
    if not content or not content.lstrip().startswith("<task-notification>"):
        return None
    return _xmlish_tag_value(content, "tool-use-id")


def _claude_event_task_notification_tool_use_id(event: dict) -> Optional[str]:
    tool_use_id = _claude_task_notification_tool_use_id(_claude_replayed_user_content(event))
    if tool_use_id:
        return tool_use_id
    if event.get("type") == "queue-operation":
        content = event.get("content")
        if isinstance(content, str):
            return _claude_task_notification_tool_use_id(content)
    return None


class _ClaudeStreamParser:
    def __init__(self, history: Optional[List[dict]]):
        self.history = history
        self.session_id: Optional[str] = None
        self.tool_blocks: dict[int, dict] = {}
        self.done = False

    def feed_line(self, line: str) -> list[Union[str, dict]]:
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []

        chunks: list[Union[str, dict]] = []
        t = event.get("type")

        if t == "system":
            self.session_id = event.get("session_id")

        elif t == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")
            idx = inner.get("index", 0)

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    self.tool_blocks[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_json": "",
                    }

            elif inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        # Claude can emit assistant text, continue with tool calls,
                        # and revise or replace that text before the turn completes.
                        # Keep these in-progress deltas in Squid's status bubble;
                        # the result event below is the completed response.
                        chunks.append({"_status": text})
                elif dtype == "input_json_delta" and idx in self.tool_blocks:
                    self.tool_blocks[idx]["input_json"] += delta.get("partial_json", "")

            elif inner_type == "content_block_stop" and idx in self.tool_blocks:
                block = self.tool_blocks.pop(idx)
                chunks.append({"_tool": _tool_data(block["name"], block["input_json"], block.get("id"))})

        elif t == "result":
            subtype = event.get("subtype")
            final_text = event.get("result", "")
            if subtype and subtype != "success":
                self.done = True
                raise CLIError(final_text or f"Claude result: {subtype}")
            if final_text:
                if "Not logged in" in final_text and "/login" in final_text:
                    raise CLIError("Claude auth failed (network down or token expired) — run: claude login")
                chunks.append(final_text)
            usage = event.get("usage", {})
            # ── Claude token semantics (verified via stream-json output, 2026-06) ──────────
            # The Anthropic API / Claude Code CLI splits input into THREE buckets:
            #
            #   input_tokens                → tiny uncacheable residual (~2–4 tokens).
            #   cache_creation_input_tokens → tokens written to the prompt cache this turn.
            #   cache_read_input_tokens     → tokens served from a previous cache entry.
            #
            # True total processed this turn = input + cache_creation + cache_read.
            #
            # Codex is the OPPOSITE: input_tokens = full total (cache already included);
            # cache_read_tokens is a subset breakdown. See run_codex() for that path.
            # ─────────────────────────────────────────────────────────────────────────────
            chunks.append({
                "_stats": {
                    "session_id": self.session_id or event.get("session_id"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
                    "history_input_tokens": _estimate_history_tokens(self.history),
                    "cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                }
            })
            self.done = True

        return chunks


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
    prompt: str = "",
    prompt_preview: Optional[str] = None,
    extra_env: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Run cmd and yield stdout line by line.

    Drains stderr concurrently to prevent the 64KB pipe buffer from filling
    and blocking the subprocess before it can write to stdout.
    """
    env = os.environ.copy()
    if PROXY_ENV:
        env.update(PROXY_ENV)
    if extra_env:
        for name, value in extra_env.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        preexec_fn=os.setpgrp,  # new process group (killpg works) without new session (keychain stays accessible)
        limit=8 * 1024 * 1024,  # 8 MB — default 64 KB overflows on long Claude responses
    )

    assert proc.stdout is not None
    assert proc.stderr is not None
    pid = proc.pid
    _register_proc(pid, backend=backend, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, prompt=prompt_preview or prompt)

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
        # Wait for stderr drain to complete now that the process has exited;
        # cancel only if it somehow stalls.
        try:
            await asyncio.wait_for(drain_task, timeout=3)
        except Exception:
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


def _tool_data(name: str, input_json: str, tool_use_id: Optional[str] = None) -> dict:
    """Parse a completed tool call into structured data for the client to render."""
    try:
        inp = json.loads(input_json) if input_json.strip() else {}
    except json.JSONDecodeError:
        inp = {}
    base: dict = {"name": name}
    if tool_use_id:
        base["tool_use_id"] = tool_use_id
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
    if name == "ask_followup_question":
        result: dict = {**base, "question": inp.get("question", "")}
        if inp.get("options"):
            result["options"] = inp["options"]
        return result
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
    backend_id: str = "claude", backend_env: Optional[dict] = None,
    backend_settings: Optional[dict] = None, backend_args: tuple[str, ...] = (),
    prompt_preview: Optional[str] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from claude CLI, then yield a stats dict."""
    if not CLAUDE_PATH:
        raise CLINotFoundError(
            "claude CLI not found in PATH. Install with: curl -fsSL https://claude.ai/install.sh | bash"
        )

    cmd = [
        CLAUDE_PATH, "--print",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    cmd += list(backend_args)
    if model:
        cmd += ["--model", model]

    if resume_session_id:
        cmd += ["--resume", resume_session_id]
        cmd.append(prompt)
    else:
        cmd.append(_build_prompt(prompt, history))

    parser = _ClaudeStreamParser(history)
    env_for_claude = _claude_child_env(backend_id, backend_env)

    async for line in _stream_lines(cmd, cwd=cwd, backend=backend_id, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview, extra_env=env_for_claude):
        for chunk in parser.feed_line(line):
            yield chunk


# After ask_followup_question tool use is detected, wait this long for a result
# event before assuming Claude Code is blocking on stdin (soft-complete the turn).
_ASK_FOLLOWUP_RESULT_WAIT: float = 10.0


def _format_followup_text(tool: dict) -> str:
    """Render an ask_followup_question tool into the plain text shown to the user:
    the question, plus any offered options as a bulleted list."""
    text = tool.get("question", "") or ""
    options = tool.get("options")
    if isinstance(options, (list, tuple)) and options:
        lines = [f"- {opt}" for opt in options if str(opt).strip()]
        if lines:
            text = (text + "\n\n" + "\n".join(lines)) if text else "\n".join(lines)
    return text


class _ClaudeInteractiveCLI:
    def __init__(
        self,
        *,
        key: tuple,
        cwd: Optional[str],
        backend_id: str,
        backend_env: Optional[dict],
        backend_args: tuple[str, ...],
        model: Optional[str],
        topic: str,
        agent: str,
        idle_timeout_s: float,
    ):
        self.key = key
        self.cwd = cwd
        self.backend_id = backend_id
        self.backend_env = backend_env
        self.backend_args = backend_args
        self.model = model
        self.topic = topic
        self.agent = agent
        self.idle_timeout_s = idle_timeout_s
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.stderr_buf: list[bytes] = []
        self.drain_task: Optional[asyncio.Task] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.pending_followup: Optional[dict] = None

    async def _start(self, resume_session_id: Optional[str], prompt: str, msg_id: Optional[int], prompt_preview: Optional[str]) -> None:
        self._cancel_idle_close()
        if self.proc and self.proc.returncode is None and self.proc.pid in _proc_registry:
            return
        if self.proc:
            await self.close()
        if not CLAUDE_PATH:
            raise CLINotFoundError(
                "claude CLI not found in PATH. Install with: curl -fsSL https://claude.ai/install.sh | bash"
            )
        cmd = [
            CLAUDE_PATH, "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        cmd += list(self.backend_args)
        if self.model:
            cmd += ["--model", self.model]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]

        env = os.environ.copy()
        if PROXY_ENV:
            env.update(PROXY_ENV)
        for name, value in _claude_child_env(self.backend_id, self.backend_env).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd,
            preexec_fn=os.setpgrp,
            limit=8 * 1024 * 1024,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdin is not None
        assert self.proc.stderr is not None
        self.stderr_buf = []
        _register_proc(self.proc.pid, backend=self.backend_id, topic=self.topic, agent=self.agent,
                       adhoc=False, msg_id=msg_id, prompt=prompt_preview or prompt, state="running")

        async def _drain() -> None:
            try:
                assert self.proc is not None and self.proc.stderr is not None
                while chunk := await self.proc.stderr.read(4096):
                    self.stderr_buf.append(chunk)
            except Exception:
                pass

        self.drain_task = asyncio.create_task(_drain(), name=f"claude-interactive-stderr-{self.proc.pid}")

    def _cancel_idle_close(self) -> None:
        task = self.idle_task
        self.idle_task = None
        if task and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_close()
        if not self.proc or self.proc.returncode is not None or self.idle_timeout_s <= 0:
            return

        async def _close_after_idle() -> None:
            try:
                await asyncio.sleep(self.idle_timeout_s)
                async with self.lock:
                    if self.proc and self.proc.returncode is None:
                        await self.close()
            except asyncio.CancelledError:
                pass

        self.idle_task = asyncio.create_task(
            _close_after_idle(),
            name=f"claude-interactive-idle-close-{self.proc.pid}",
        )

    async def close(self) -> None:
        self._cancel_idle_close()
        proc = self.proc
        self.proc = None
        if not proc:
            return
        _deregister_proc(proc.pid)
        try:
            if proc.returncode is None:
                if not _signal_process_group(proc.pid, signal.SIGTERM):
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            try:
                if proc.returncode is None:
                    if not _signal_process_group(proc.pid, signal.SIGKILL):
                        proc.kill()
                    await proc.wait()
            except Exception:
                pass
        if self.drain_task:
            try:
                await asyncio.wait_for(self.drain_task, timeout=3)
            except Exception:
                self.drain_task.cancel()
            self.drain_task = None

    async def query(
        self,
        prompt: str,
        *,
        history: Optional[List[dict]],
        resume_session_id: Optional[str],
        msg_id: Optional[int],
        response_timeout: Optional[int],
        prompt_preview: Optional[str],
    ) -> AsyncGenerator[Union[str, dict], None]:
        async with self.lock:
            had_live_process = bool(self.proc and self.proc.returncode is None)
            await self._start(resume_session_id, prompt, msg_id, prompt_preview)
            assert self.proc is not None
            assert self.proc.stdin is not None
            assert self.proc.stdout is not None
            _update_proc(
                self.proc.pid,
                state="running",
                msg_id=msg_id,
                prompt_preview=((prompt_preview or prompt)[:80] + "…") if len(prompt_preview or prompt) > 80 else (prompt_preview or prompt),
            )
            content = prompt if resume_session_id or had_live_process else _build_prompt(prompt, history)
            # If the previous turn ended with ask_followup_question, send this
            # message as the tool result (via parent_tool_use_id). This unblocks
            # Claude Code which was waiting for the user's answer on stdin.
            # Only valid when reusing the same live process; a restarted process
            # has no memory of the pending tool call.
            followup_id = self.pending_followup.get("tool_use_id") if (self.pending_followup and had_live_process) else None
            self.pending_followup = None
            payload = {
                "type": "user",
                "message": {"role": "user", "content": content},
            }
            if followup_id:
                payload["parent_tool_use_id"] = followup_id
            try:
                self.proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                await self.proc.stdin.drain()
            except Exception as exc:
                await self.close()
                raise CLIError(f"Claude interactive stdin failed: {exc}") from exc

            parser = _ClaudeStreamParser(history)
            accepting_turn = False
            current_turn_is_prompt = False
            pending_agent_tool_use_id: Optional[str] = None
            turn_live_chunks: list[dict] = []
            turn_result_chunks: list[Union[str, dict]] = []
            turn_agent_tool_use_id: Optional[str] = None
            ask_followup_tool_use_id: Optional[str] = None
            ask_followup_question_text: str = ""
            accepted_agent_notification = False
            timeout = response_timeout if response_timeout is not None else RESPONSE_TIMEOUT
            deadline = asyncio.get_event_loop().time() + timeout
            first_byte = True

            try:
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        await self.close()
                        raise CLIError("Response timeout exceeded")
                    per_line = min(FIRST_BYTE_TIMEOUT if first_byte else remaining, remaining)
                    if ask_followup_tool_use_id:
                        # Claude Code is likely blocking on stdin waiting for the
                        # user's answer; use a short window before soft-completing.
                        per_line = min(_ASK_FOLLOWUP_RESULT_WAIT, per_line)
                    try:
                        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=per_line)
                    except asyncio.TimeoutError:
                        if ask_followup_tool_use_id:
                            # Claude Code is blocked waiting for a tool result on
                            # stdin. Soft-complete: surface the question as the
                            # turn response, keep the process alive, and store the
                            # pending tool_use_id so the next query() sends the
                            # user's reply as parent_tool_use_id (unblocking it).
                            self.pending_followup = {"tool_use_id": ask_followup_tool_use_id}
                            if ask_followup_question_text:
                                turn_result_chunks.append(ask_followup_question_text)
                            if accepting_turn:
                                for chunk in turn_result_chunks:
                                    yield chunk
                            break
                        await self.close()
                        raise CLIError("Timed out waiting for CLI response")
                    if not line:
                        returncode = self.proc.returncode
                        await self.close()
                        err = b"".join(self.stderr_buf).decode(errors="replace").strip()
                        if returncode:
                            raise CLIError(f"CLI exited {returncode}: {err}")
                        raise CLIError("Claude interactive stream closed before result")
                    first_byte = False
                    line_text = line.decode(errors="replace").rstrip("\n")
                    try:
                        event = json.loads(line_text)
                    except json.JSONDecodeError:
                        event = {}
                    replayed = _claude_replayed_user_content(event)
                    if replayed == content:
                        accepting_turn = True
                        current_turn_is_prompt = True
                        for chunk in turn_live_chunks:
                            yield chunk
                        turn_live_chunks = []
                    elif (
                        pending_agent_tool_use_id
                        and _claude_event_task_notification_tool_use_id(event) == pending_agent_tool_use_id
                    ):
                        accepting_turn = True
                        current_turn_is_prompt = False
                        accepted_agent_notification = True
                        for chunk in turn_live_chunks:
                            yield chunk
                        turn_live_chunks = []

                    chunks = parser.feed_line(line_text)
                    for chunk in chunks:
                        if isinstance(chunk, dict) and ("_status" in chunk or "_tool" in chunk):
                            tool = chunk.get("_tool")
                            if isinstance(tool, dict) and tool.get("name") == "Agent":
                                turn_agent_tool_use_id = tool.get("tool_use_id") or ""
                            if isinstance(tool, dict) and tool.get("name") == "ask_followup_question":
                                # Record tool info; don't emit as a tool widget.
                                # The question text is surfaced as plain response text.
                                ask_followup_tool_use_id = tool.get("tool_use_id") or ""
                                ask_followup_question_text = _format_followup_text(tool)
                                continue
                            if accepting_turn:
                                yield chunk
                            else:
                                turn_live_chunks.append(chunk)
                        else:
                            turn_result_chunks.append(chunk)

                    if parser.done:
                        if accepting_turn and current_turn_is_prompt and turn_agent_tool_use_id:
                            pending_agent_tool_use_id = turn_agent_tool_use_id
                            parser = _ClaudeStreamParser(history)
                            accepting_turn = False
                            current_turn_is_prompt = False
                            accepted_agent_notification = False
                            turn_live_chunks = []
                            turn_result_chunks = []
                            turn_agent_tool_use_id = None
                            ask_followup_tool_use_id = None
                            ask_followup_question_text = ""
                            continue
                        if accepting_turn:
                            if ask_followup_tool_use_id and ask_followup_question_text:
                                # Claude Code auto-handled ask_followup_question (result
                                # fired normally). Ensure the question is visible when the
                                # result body is absent (Claude Code returned empty text).
                                if not any(isinstance(c, str) and c.strip() for c in turn_result_chunks):
                                    turn_result_chunks.insert(0, ask_followup_question_text)
                            elif (
                                accepted_agent_notification
                                and not any(isinstance(c, str) and c.strip() for c in turn_result_chunks)
                            ):
                                # Claude can emit a successful, result-empty thinking-only
                                # assistant turn immediately after replaying an async Agent
                                # task notification, then emit the actual user-visible
                                # answer as the next assistant result. Do not complete the
                                # Squid turn on stats-only output from that internal turn.
                                parser = _ClaudeStreamParser(history)
                                accepting_turn = True
                                current_turn_is_prompt = False
                                accepted_agent_notification = True
                                turn_live_chunks = []
                                turn_result_chunks = []
                                turn_agent_tool_use_id = None
                                ask_followup_tool_use_id = None
                                ask_followup_question_text = ""
                                continue
                            for chunk in turn_result_chunks:
                                yield chunk
                            break
                        # Claude may have processed a queued task-notification
                        # before the prompt Squid just wrote. Discard that turn
                        # and keep reading until the replayed prompt matches.
                        parser = _ClaudeStreamParser(history)
                        accepting_turn = False
                        current_turn_is_prompt = False
                        accepted_agent_notification = False
                        turn_live_chunks = []
                        turn_result_chunks = []
                        turn_agent_tool_use_id = None
                        ask_followup_tool_use_id = None
                        ask_followup_question_text = ""
            finally:
                if self.proc:
                    _update_proc(self.proc.pid, state="idle", msg_id=None)
                    self._schedule_idle_close()


_claude_interactive_sessions: dict[tuple, _ClaudeInteractiveCLI] = {}


async def run_claude_interactive_cli(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
    backend_id: str = "claude", backend_env: Optional[dict] = None,
    backend_settings: Optional[dict] = None, backend_args: tuple[str, ...] = (),
    interactive_idle_timeout_s: float = 3600,
    prompt_preview: Optional[str] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream one turn through a persistent Claude Code stream-json process."""
    if adhoc:
        async for chunk in run_claude(
            prompt, cwd=cwd, history=history, model=model, topic=topic, agent=agent,
            response_timeout=response_timeout, resume_session_id=resume_session_id,
            adhoc=adhoc, msg_id=msg_id, backend_id=backend_id, backend_env=backend_env,
            backend_settings=backend_settings, backend_args=backend_args,
            prompt_preview=prompt_preview,
        ):
            yield chunk
        return
    key = (backend_id, topic, agent, cwd, model, tuple(backend_args))
    session = _claude_interactive_sessions.get(key)
    if session is None:
        session = _ClaudeInteractiveCLI(
            key=key, cwd=cwd, backend_id=backend_id, backend_env=backend_env,
            backend_args=backend_args, model=model, topic=topic, agent=agent,
            idle_timeout_s=interactive_idle_timeout_s,
        )
        _claude_interactive_sessions[key] = session
    else:
        if not resume_session_id and session.proc and session.proc.returncode is None:
            # No stored session (cleared or first use) but a live process exists — its
            # accumulated context is stale. Kill it so the next turn starts fresh.
            await session.close()
        session.idle_timeout_s = interactive_idle_timeout_s
    response_text = ""
    try:
        async for chunk in session.query(
            prompt, history=history, resume_session_id=resume_session_id,
            msg_id=msg_id, response_timeout=response_timeout, prompt_preview=prompt_preview,
        ):
            if isinstance(chunk, str):
                response_text += chunk
            yield chunk
        if response_text.strip().lower() == "prompt is too long":
            await session.close()
            _claude_interactive_sessions.pop(key, None)
    except Exception:
        await session.close()
        _claude_interactive_sessions.pop(key, None)
        raise


def _codex_config_args(settings: dict) -> list[str]:
    """Flatten structured YAML settings into Codex `-c key=value` arguments."""
    result: list[str] = []

    def visit(prefix: str, value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(f"{prefix}.{key}" if prefix else str(key), child)
            return
        result.extend(["-c", f"{prefix}={json.dumps(value, separators=(',', ':'))}"])

    visit("", settings)
    return result


async def run_codex(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
    backend_id: str = "codex", backend_env: Optional[dict] = None,
    backend_settings: Optional[dict] = None, backend_args: tuple[str, ...] = (),
    prompt_preview: Optional[str] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream a response from codex CLI using non-interactive exec mode."""
    if not CODEX_PATH:
        raise CLINotFoundError(
            "codex CLI not found in PATH. Install with: curl -fsSL https://chatgpt.com/codex/install.sh | sh"
        )

    config_args = _codex_config_args(backend_settings or {})
    if resume_session_id:
        cmd = [CODEX_PATH, "exec", "resume", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        cmd += config_args + list(backend_args)
        if model:
            cmd += ["--model", model]
        cmd += [resume_session_id, prompt]
    else:
        cmd = [CODEX_PATH, "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        cmd += config_args + list(backend_args)
        if model:
            cmd += ["--model", model]
        cmd.append(_build_prompt(prompt, history))

    start_ms = time.monotonic() * 1000
    thread_id: Optional[str] = None

    async for line in _stream_lines(cmd, cwd=cwd, backend=backend_id, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview, extra_env=backend_env):
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
                    # Codex emits progress updates and the final answer with the
                    # same item type. Keep commentary in Squid's status bubble;
                    # only final_answer belongs in persisted response content.
                    if item.get("phase") == "commentary":
                        yield {"_status": str(text)}
                    else:
                        # Missing phase is the legacy CLI shape and remains
                        # response content for backward compatibility.
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
    prompt_preview: Optional[str] = None,
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
    output_tokens: int = 0
    streamed_text: bool = False
    stats_yielded = False
    session_error: Optional[str] = None

    async for line in _stream_lines(cmd, cwd=cwd, backend="copilot", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview):
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type", "")
        data = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}

        if t == "assistant.reasoning_delta":
            text = data.get("deltaContent", "")
            if text:
                yield {"_status": text}

        elif t == "assistant.message_delta":
            text = data.get("deltaContent", "")
            if text:
                streamed_text = True
                yield text

        elif t == "assistant.message":
            # Non-ephemeral summary — carries outputTokens and the canonical content.
            # Only yield content here if deltas were not streamed (shouldn't happen but safe).
            output_tokens = data.get("outputTokens", 0)
            if not streamed_text:
                content = data.get("content", "")
                if content:
                    streamed_text = True
                    yield content

        elif t == "session.error":
            session_error = data.get("message") or data.get("errorType") or "Unknown error"

        elif t == "result":
            exit_code = ev.get("exitCode", 0)
            if exit_code != 0 and not streamed_text:
                raise CLIError(f"copilot: {session_error or 'CLI exited with no output'}")
            usage = ev.get("usage", {}) or {}
            # premiumRequests is the only cost signal copilot exposes (input tokens not available)
            premium = usage.get("premiumRequests")
            yield {
                "_stats": {
                    "session_id": ev.get("sessionId"),
                    "input_tokens": 0,  # not exposed by copilot CLI
                    "output_tokens": output_tokens,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "history_input_tokens": _estimate_history_tokens(history),
                    "cost_usd": premium,  # fractional premium request count used as cost proxy
                    "duration_ms": usage.get("totalApiDurationMs") or int(time.monotonic() * 1000 - start_ms),
                }
            }
            stats_yielded = True
            return

    if not stats_yielded:
        yield {
            "_stats": {
                "session_id": None,
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
    backend_id: str = "cursor", backend_env: Optional[dict] = None,
    backend_settings: Optional[dict] = None, backend_args: tuple[str, ...] = (),
    prompt_preview: Optional[str] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from cursor-agent CLI, then yield a stats dict."""
    if not CURSOR_PATH:
        raise CLINotFoundError(
            "cursor-agent CLI not found in PATH. Install: curl -fsS https://cursor.com/install | bash"
        )

    cmd = [
        CURSOR_PATH, "--print",
        "--output-format", "stream-json",
        "--stream-partial-output",
        "--trust",
    ]
    cmd += list(backend_args)
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt if resume_session_id else _build_prompt(prompt, history))

    session_id: Optional[str] = None
    text_started = False

    async for line in _stream_lines(cmd, cwd=cwd, backend=backend_id, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview, extra_env=backend_env):
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
    prompt_preview: Optional[str] = None,
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

    async for line in _stream_lines(cmd, cwd=cwd, backend="antigravity", topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview):
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


def _opencode_tool(part: dict) -> Optional[dict]:
    """Map an opencode tool_use part to a squid _tool dict."""
    tool = part.get("tool", "")
    state = part.get("state", {})
    if state.get("status") != "completed":
        return None
    inp = state.get("input") or {}
    if tool == "bash":
        return {"name": "Bash", "command": inp.get("command", "")}
    if tool == "read":
        return {"name": "Read", "file": inp.get("filePath", inp.get("path", ""))}
    if tool == "write":
        return {"name": "Write", "file": inp.get("filePath", inp.get("path", "")),
                "content": inp.get("content", "")}
    if tool == "edit":
        return {"name": "Edit", "file": inp.get("filePath", inp.get("path", "")),
                "old": inp.get("oldString", ""), "new": inp.get("newString", "")}
    if inp:
        k, v = next(iter(inp.items()))
        return {"name": tool, "key": k, "value": str(v)[:300]}
    return {"name": tool}


async def run_opencode(
    prompt: str, cwd: Optional[str] = None, history: Optional[List[dict]] = None,
    model: Optional[str] = None, topic: str = "", agent: str = "",
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    adhoc: bool = False, msg_id: Optional[int] = None,
    backend_id: str = "opencode", backend_env: Optional[dict] = None,
    backend_settings: Optional[dict] = None, backend_args: tuple[str, ...] = (),
    prompt_preview: Optional[str] = None,
) -> AsyncGenerator[Union[str, dict], None]:
    """Stream text chunks from opencode CLI, then yield a stats dict."""
    if not OPENCODE_PATH:
        raise CLINotFoundError(
            "opencode CLI not found in PATH. Install with: curl -fsSL https://opencode.ai/install | bash"
        )

    cmd = [OPENCODE_PATH, "run", "--format", "json", "--dangerously-skip-permissions"]
    cmd += list(backend_args)
    if model:
        cmd += ["-m", model]
    if resume_session_id:
        cmd += ["--session", resume_session_id]
    cmd.append(prompt if resume_session_id else _build_prompt(prompt, history))

    start_ms = time.monotonic() * 1000
    session_id: Optional[str] = None
    # Accumulate tokens across all steps (opencode emits one step_finish per tool call)
    total_input = total_output = total_cache_read = total_cache_write = 0
    total_cost: float = 0.0

    async for line in _stream_lines(cmd, cwd=cwd, backend=backend_id, topic=topic, agent=agent, adhoc=adhoc, msg_id=msg_id, response_timeout=response_timeout, prompt=prompt, prompt_preview=prompt_preview, extra_env=backend_env):
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = event.get("type", "")

        if session_id is None:
            session_id = event.get("sessionID")

        if t == "text":
            text = event.get("part", {}).get("text", "")
            if text:
                yield text

        elif t == "tool_use":
            tool_dict = _opencode_tool(event.get("part", {}))
            if tool_dict:
                yield {"_tool": tool_dict}

        elif t == "step_finish":
            tokens = event.get("part", {}).get("tokens", {})
            total_input       += int(tokens.get("input", 0) or 0)
            total_output      += int(tokens.get("output", 0) or 0)
            cache             = tokens.get("cache") or {}
            total_cache_read  += int(cache.get("read", 0) or 0)
            total_cache_write += int(cache.get("write", 0) or 0)
            total_cost        += float(event.get("part", {}).get("cost", 0) or 0)

        elif t == "error":
            err = event.get("error", {})
            msg = (err.get("data", {}) or {}).get("message") or err.get("message") or "opencode error"
            raise CLIError(msg)

    yield {
        "_stats": {
            "session_id": session_id,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "history_input_tokens": _estimate_history_tokens(history),
            "cost_usd": total_cost if total_cost else None,
            "duration_ms": int(time.monotonic() * 1000 - start_ms),
        }
    }


RUNNER_NAMES_BY_DRIVER = {
    "claude": "run_claude",
    "codex": "run_codex",
    "cursor": "run_cursor",
    "copilot": "run_copilot",
    "antigravity": "run_antigravity",
    "opencode": "run_opencode",
}

RUNNER_NAMES_BY_DRIVER_PROTOCOL = {
    ("claude", "oneshot-cli"): "run_claude",
    ("claude", "interactive-cli"): "run_claude_interactive_cli",
    ("codex", "oneshot-cli"): "run_codex",
    ("cursor", "oneshot-cli"): "run_cursor",
    ("opencode", "oneshot-cli"): "run_opencode",
}


def runner_for_driver(driver: str):
    name = RUNNER_NAMES_BY_DRIVER.get(driver)
    return globals().get(name) if name else None


def runner_for_backend(backend, *, adhoc: bool = False):
    protocol = getattr(backend, "protocol", "oneshot-cli") or "oneshot-cli"
    if adhoc and protocol != "oneshot-cli":
        protocol = "oneshot-cli"
    name = RUNNER_NAMES_BY_DRIVER_PROTOCOL.get((backend.driver, protocol))
    return globals().get(name) if name else None
