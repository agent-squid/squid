"""agent/auth_sessions.py — CLI auth sessions via scoped PTY (ADR-0035).

A narrow, allowlisted feature for running a harness's own interactive login
command (device-code flow, OAuth browser flow, or provider-picker wizard)
inside a real PTY so the user can complete it without leaving Squid. This is
deliberately not a general in-app shell: the command run is always chosen
server-side from ALLOWLISTED_LOGIN_COMMANDS, never constructed from user
input, and input bytes sent to a session are forwarded only to that fixed
process.

Also covers "install" sessions (harness install one-liners and the `ollama`
provider's install one-liner, ADR-0037's amendment on 2026-08-09) and
`ollama pull`/`ollama rm` model-management sessions. Pull accepts a validated
Ollama model name as one argv element (never shell input); remove remains
restricted to the provider's configured `models:` list.

Distinct from the `interactive-pty` *protocol* name declared in
harnesses.py (ADR-0022) — that protocol has no implementation anywhere in
the codebase today. This module is the first real PTY spawn/stream code in
Squid and does not share machinery with it.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
import time
import uuid
from typing import AsyncGenerator, Optional

from .config import CLAUDE_PATH, CODEX_PATH, CURSOR_PATH, OLLAMA_PATH, OPENCODE_PATH
from .runners import _register_proc, _deregister_proc, _signal_process_group

# Sentinel topic under the existing process registry (agent/runners.py) so
# /processes, /cmd stopall, and shutdown/restart's kill_all_procs() all reach
# auth sessions the same way they reach normal turn subprocesses — this is
# the "registered in the normal process registry" requirement from
# ADR-0035, not a parallel bookkeeping system.
_AUTH_SESSION_TOPIC = "__auth_session__"

# Codex's device code expires in 15 minutes (observed in its own login output);
# cursor's browser-deep-link flow has no printed expiry. 20 minutes covers
# codex's window with margin while still reaping an abandoned browser tab in
# a bounded time — a dedicated value rather than reusing the 1h
# DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS, which is sized for long-lived
# agent turns, not a one-shot login prompt.
AUTH_SESSION_IDLE_TIMEOUT_SECONDS = 1200

# Rolling output buffer per session so a client that opens the SSE stream
# slightly after spawn still sees the banner/URL/code instead of missing it.
_REPLAY_BUFFER_CAP = 64 * 1024


class AuthSessionError(RuntimeError):
    pass


class NoLoginCommand(AuthSessionError):
    """Raised for harnesses with no CLI login command (pi)."""


def _login_argv(harness_id: str) -> list[str]:
    """Fixed, allowlisted command per harness. Never built from user input."""
    if harness_id == "claudecode":
        if not CLAUDE_PATH:
            raise AuthSessionError("claude CLI not found in PATH")
        return [CLAUDE_PATH, "auth", "login", "--claudeai"]
    if harness_id == "codex":
        if not CODEX_PATH:
            raise AuthSessionError("codex CLI not found in PATH")
        return [CODEX_PATH, "login", "--device-auth"]
    if harness_id == "cursor":
        if not CURSOR_PATH:
            raise AuthSessionError("cursor-agent CLI not found in PATH")
        return [CURSOR_PATH, "login"]
    if harness_id == "opencode":
        if not OPENCODE_PATH:
            raise AuthSessionError("opencode CLI not found in PATH")
        return [OPENCODE_PATH, "auth", "login"]
    if harness_id == "pi":
        raise NoLoginCommand(
            "pi has no CLI login command — set ANTHROPIC_API_KEY or "
            "ANTHROPIC_OAUTH_TOKEN in its environment instead."
        )
    raise AuthSessionError(f"Unknown harness {harness_id!r}")


def _login_env(harness_id: str) -> dict:
    env = os.environ.copy()
    if harness_id == "cursor":
        # Non-interactive-friendly: print the deep link instead of trying to
        # exec a browser from inside Squid's PTY (verified empirically —
        # honored, no browser launch attempted).
        env["NO_OPEN_BROWSER"] = "1"
    return env


def _install_argv(target_id: str) -> list[str]:
    """Fixed, allowlisted install command for a harness or the `ollama`
    provider. Never built from user input — same invariant as _login_argv.
    Wrapped in `sh -c` because install one-liners are themselves fixed
    strings (curl-pipe-sh), not user-supplied — identical trust model to the
    settings catalog's existing copy-to-clipboard command, just executed
    instead of pasted."""
    if target_id == "ollama":
        if OLLAMA_PATH:
            raise AuthSessionError("ollama is already installed")
        from .providers import provider_install_cmd
        return ["sh", "-c", provider_install_cmd("ollama")]

    from .harnesses import SUPPORTED_HARNESSES, harness_install_cmd, is_installed
    if target_id not in SUPPORTED_HARNESSES:
        raise AuthSessionError(f"Unknown install target {target_id!r}")
    if is_installed(target_id):
        raise AuthSessionError(f"{target_id} is already installed")
    return ["sh", "-c", harness_install_cmd(target_id)]


def _model_argv(action: str, model: str) -> list[str]:
    """Build a fixed-shape Ollama command without invoking a shell.

    Pull permits a validated registry model name; remove is deliberately
    limited to configured models so free text cannot delete local data.
    """
    if not OLLAMA_PATH:
        raise AuthSessionError("ollama CLI not found in PATH")
    if len(model) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?", model):
        raise AuthSessionError(f"{model!r} is not a valid ollama model name")
    from .providers import get_provider
    provider = get_provider("ollama")
    if action == "rm" and (not provider or model not in provider.models):
        raise AuthSessionError(f"{model!r} is not a configured ollama model")
    return [OLLAMA_PATH, action, model]


class AuthSession:
    def __init__(self, session_id: str, harness_id: str, pid: int, master_fd: int, display_command: str):
        self.id = session_id
        self.harness_id = harness_id
        self.pid = pid
        self.master_fd = master_fd
        self.display_command = display_command
        self.state = "running"  # running -> exited
        self.returncode: Optional[int] = None
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        self.buffer = bytearray()
        self.listeners: list[asyncio.Queue] = []
        self.reader_task: Optional[asyncio.Task] = None
        self.idle_task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def broadcast(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        if len(self.buffer) > _REPLAY_BUFFER_CAP:
            del self.buffer[: len(self.buffer) - _REPLAY_BUFFER_CAP]
        for q in list(self.listeners):
            q.put_nowait(chunk)

    def mark_exited(self, returncode: int) -> None:
        if self.state == "exited":
            return
        self.state = "exited"
        self.returncode = returncode
        for q in list(self.listeners):
            q.put_nowait(None)
        self._closed.set()


_sessions: dict[str, AuthSession] = {}
# asyncio only keeps a task alive via a strong reference; a bare
# create_task() result that's dropped can be garbage-collected before it
# runs. _expire_session is fire-and-forget by design, so it's tracked here
# instead (discarded via the task's own done-callback) rather than skipped —
# losing it silently would quietly reintroduce the leak this exists to fix.
_expiry_tasks: set[asyncio.Task] = set()


def get_session(session_id: str) -> Optional[AuthSession]:
    return _sessions.get(session_id)


def _spawn_pty(argv: list[str], env: dict, cols: int, rows: int) -> tuple[int, int]:
    """Fork+exec argv[0] attached to a fresh PTY. Runs in a worker thread —
    the child calls execvpe immediately with no other Python code in
    between, same fork+exec shape asyncio.create_subprocess_exec already
    uses elsewhere in this codebase, just with a real terminal instead of
    pipes so CLIs that check isatty() behave as they do in a real shell.
    pty.fork() makes the child a session leader with the PTY slave as its
    controlling terminal (setsid + TIOCSCTTY), so os.killpg(pid, ...) works
    without any extra preexec_fn, consistent with ADR-0018's process-group
    isolation.

    The winsize is set here, in the parent, immediately after fork and
    before returning — not via a follow-up ioctl once the client's resize
    request round-trips back. That earlier request used to arrive after the
    child had already exec'd and often already rendered its first frame at
    the PTY's default (unset) size; a provider-picker TUI redrawing itself
    against a SIGWINCH after its first paint has already computed on-screen
    row offsets from the old size, which is what produced the up-arrow
    rendering corruption in the opencode login list.
    """
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        except Exception:
            os._exit(127)
    else:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    return pid, master_fd


async def create_session(
    target_id: str, cols: int, rows: int, mode: str = "login", model: Optional[str] = None,
) -> AuthSession:
    """`target_id` is a harness id for mode="login"/"install", or "ollama"
    for mode="install"/"pull"/"remove". `model` is required for pull/remove
    and is validated by _model_argv. Raises AuthSessionError /
    NoLoginCommand."""
    if mode == "login":
        argv = _login_argv(target_id)
        env = _login_env(target_id)
    elif mode == "install":
        argv = _install_argv(target_id)
        env = os.environ.copy()
    elif mode in ("pull", "remove"):
        if not model:
            raise AuthSessionError(f"mode={mode!r} requires a model")
        argv = _model_argv("pull" if mode == "pull" else "rm", model)
        env = os.environ.copy()
    else:
        raise AuthSessionError(f"Unknown auth-session mode {mode!r}")

    pid, master_fd = await asyncio.to_thread(_spawn_pty, argv, env, cols, rows)
    os.set_blocking(master_fd, False)

    session_id = uuid.uuid4().hex
    session = AuthSession(session_id, target_id, pid, master_fd, shlex.join(argv))
    _sessions[session_id] = session

    prompt = f"{mode}: {target_id}" + (f" {model}" if model else "")
    _register_proc(
        pid, backend=target_id, topic=_AUTH_SESSION_TOPIC, agent=target_id,
        adhoc=True, prompt=prompt,
    )

    loop = asyncio.get_event_loop()

    def _on_readable() -> None:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            loop.remove_reader(master_fd)
            asyncio.create_task(_finalize(session))
            return
        session.touch()
        session.broadcast(chunk)

    loop.add_reader(master_fd, _on_readable)
    session.idle_task = asyncio.create_task(_idle_reaper(session))
    return session



# How long an exited session stays in _sessions before being dropped.
# cancel_session() (explicit user Cancel/Close) still removes it immediately —
# this is only for a client that never calls back after the process exits on
# its own (tab closed, network dropped, login finished and the exit event was
# missed), so a still-open tab can reconnect and see the final output/exit
# code instead of a 404 for a session that technically finished a moment ago.
_POST_EXIT_RETENTION_SECONDS = 120


async def _finalize(session: AuthSession) -> None:
    """Reap the child once its PTY slave closes (process exited)."""
    try:
        _, status = await asyncio.to_thread(os.waitpid, session.pid, 0)
        returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    except ChildProcessError:
        returncode = -1
    session.mark_exited(returncode)
    _deregister_proc(session.pid)
    try:
        os.close(session.master_fd)
    except OSError:
        pass
    if session.idle_task:
        session.idle_task.cancel()
    expiry_task = asyncio.create_task(_expire_session(session.id))
    _expiry_tasks.add(expiry_task)
    expiry_task.add_done_callback(_expiry_tasks.discard)


async def _expire_session(session_id: str) -> None:
    await asyncio.sleep(_POST_EXIT_RETENTION_SECONDS)
    _sessions.pop(session_id, None)


async def _idle_reaper(session: AuthSession) -> None:
    try:
        while session.state == "running":
            await asyncio.sleep(30)
            if session.state != "running":
                return
            if time.monotonic() - session.last_activity > AUTH_SESSION_IDLE_TIMEOUT_SECONDS:
                await cancel_session(session.id)
                return
    except asyncio.CancelledError:
        pass


async def stream_events(session: AuthSession) -> AsyncGenerator[bytes, None]:
    """Replay the buffer, then yield live chunks until the session ends.

    The queue is registered before the buffer is snapshotted, and nothing
    awaits between those two lines — broadcast() runs synchronously on the
    event loop (it's called from a non-async reader callback), so there is
    no point where it can interleave between them. That ordering matters:
    doing it the other way (snapshot, *then* yield, *then* register) leaves
    a real gap at the `yield` — the generator can stay suspended there for
    however long the SSE response takes to flush that chunk to the network,
    during which any PTY output (e.g. the OAuth URL/device code the buffer
    exists to protect) would be broadcast to no one and lost.
    """
    q: asyncio.Queue = asyncio.Queue()
    session.listeners.append(q)
    try:
        snapshot = bytes(session.buffer)
        if snapshot:
            yield snapshot
        if session.state == "exited":
            return
        while True:
            chunk = await q.get()
            if chunk is None:
                return
            yield chunk
    finally:
        try:
            session.listeners.remove(q)
        except ValueError:
            pass


def write_input(session: AuthSession, data: bytes) -> None:
    if session.state != "running":
        raise AuthSessionError("session is not running")
    session.touch()
    os.write(session.master_fd, data)


def resize(session: AuthSession, cols: int, rows: int) -> None:
    if session.state != "running":
        return
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, winsize)


async def cancel_session(session_id: str) -> bool:
    session = _sessions.get(session_id)
    if not session:
        return False
    if session.state == "running":
        _signal_process_group(session.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(session._closed.wait(), timeout=3)
        except asyncio.TimeoutError:
            _signal_process_group(session.pid, signal.SIGKILL)
    _sessions.pop(session_id, None)
    return True
