"""
Unit tests for runners.py process registry and kill functions.
"""
import asyncio
import time
from unittest.mock import patch, call
import pytest

from agent.runners import (
    _proc_registry,
    _register_proc,
    _deregister_proc,
    kill_procs_by_topic,
    kill_proc_by_msg_id,
    run_codex,
)


def _clear():
    _proc_registry.clear()


# ── kill_procs_by_topic ────────────────────────────────────────────────────────

def test_lifo_kills_most_recent_first():
    """Consecutive LIFO stops walk back: stop→stop kills #2 then #1."""
    _clear()
    _register_proc(101, "claude", "work", "claude", adhoc=True,  msg_id=1)
    _register_proc(102, "claude", "work", "claude", adhoc=True,  msg_id=2)
    # Make pid=102 clearly newer
    _proc_registry[102]["started_at"] = _proc_registry[101]["started_at"] + 1.0

    with patch("agent.runners.os.kill") as mock_kill:
        # First stop — should kill pid=102 (most recent)
        killed = kill_procs_by_topic("work", agent="claude", adhoc=True, lifo=True)
        assert killed == 1
        mock_kill.assert_called_once_with(102, __import__("signal").SIGTERM)
        assert 102 not in _proc_registry  # deregistered immediately

        # Second stop — should kill pid=101 (now the most recent remaining)
        mock_kill.reset_mock()
        killed = kill_procs_by_topic("work", agent="claude", adhoc=True, lifo=True)
        assert killed == 1
        mock_kill.assert_called_once_with(101, __import__("signal").SIGTERM)
        assert 101 not in _proc_registry

    _clear()


def test_lifo_does_not_rekill_already_stopped():
    """After first LIFO stop, pid is deregistered — second call targets the next one."""
    _clear()
    _register_proc(201, "claude", "work", "claude", adhoc=True, msg_id=10)
    _register_proc(202, "claude", "work", "claude", adhoc=True, msg_id=11)
    _proc_registry[202]["started_at"] = _proc_registry[201]["started_at"] + 1.0

    with patch("agent.runners.os.kill"):
        kill_procs_by_topic("work", agent="claude", adhoc=True, lifo=True)
        # pid=202 is gone; registry has only pid=201
        assert 202 not in _proc_registry
        assert 201 in _proc_registry

    _clear()


def test_nuclear_stop_kills_all():
    """`#topic /stop` (no agent/adhoc filter) kills every process under topic."""
    _clear()
    _register_proc(301, "claude", "work", "claude", adhoc=False, msg_id=20)
    _register_proc(302, "claude", "work", "codex",  adhoc=True,  msg_id=21)

    with patch("agent.runners.os.kill") as mock_kill:
        killed = kill_procs_by_topic("work")
        assert killed == 2
        killed_pids = {c.args[0] for c in mock_kill.call_args_list}
        assert killed_pids == {301, 302}

    _clear()


def test_agent_scoped_stop_skips_other_agents():
    """`#topic@agent /stop` only kills processes for that agent."""
    _clear()
    _register_proc(401, "claude", "work", "claude", adhoc=False, msg_id=30)
    _register_proc(402, "codex",  "work", "codex",  adhoc=False, msg_id=31)

    with patch("agent.runners.os.kill") as mock_kill:
        killed = kill_procs_by_topic("work", agent="claude", adhoc=False)
        assert killed == 1
        mock_kill.assert_called_once_with(401, __import__("signal").SIGTERM)

    _clear()


# ── kill_proc_by_msg_id ────────────────────────────────────────────────────────

def test_kill_by_msg_id_targets_exact_process():
    """× button kill sends stop_msg with msg_id — server kills the exact process."""
    _clear()
    _register_proc(501, "claude", "work", "claude", adhoc=True, msg_id=99)
    _register_proc(502, "claude", "work", "claude", adhoc=True, msg_id=100)

    with patch("agent.runners.os.kill") as mock_kill:
        result = kill_proc_by_msg_id(99)
        assert result == 1
        mock_kill.assert_called_once_with(501, __import__("signal").SIGTERM)
        assert 501 not in _proc_registry  # deregistered
        assert 502 in _proc_registry      # untouched

    _clear()


def test_kill_by_msg_id_returns_zero_when_not_found():
    _clear()
    with patch("agent.runners.os.kill"):
        assert kill_proc_by_msg_id(9999) == 0


def test_codex_stats_keep_cached_tokens_as_breakdown_only():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"type":"turn.completed","usage":{"input_tokens":900000,"cached_input_tokens":300000,"output_tokens":1200,"reasoning_output_tokens":50}}'

    async def collect():
        return [chunk async for chunk in run_codex("hello", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch("agent.runners._stream_lines", fake_stream_lines):
        chunks = asyncio.run(collect())

    stats = chunks[-1]["_stats"]
    assert stats["session_id"] == "thread-1"
    assert stats["input_tokens"] == 900000
    assert stats["cache_read_tokens"] == 300000
    assert stats["output_tokens"] == 1200
