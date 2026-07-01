"""
Unit tests for runners.py process registry and kill functions.
"""
import asyncio
import json
import signal
import time
from unittest.mock import patch, call
import pytest

from agent.runners import (
    _claude_interactive_sessions,
    _proc_registry,
    _register_proc,
    _deregister_proc,
    kill_all_procs,
    kill_procs_by_topic,
    kill_proc_by_msg_id,
    list_active_procs,
    runner_for_backend,
    runner_for_driver,
    run_claude,
    run_claude_interactive_cli,
    run_codex,
    run_codex_interactive_cli,
    run_cursor_interactive_cli,
    run_opencode,
    run_opencode_interactive_cli,
)
from agent.backends import _validate_backend


def _clear():
    _proc_registry.clear()
    _claude_interactive_sessions.clear()


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self.lines = [line.encode() for line in lines]

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return b""


class _FakeStderr:
    async def read(self, _n):
        return b""


class _FakeProcess:
    def __init__(self, pid, lines):
        self.pid = pid
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminated = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_runner_for_driver_uses_shared_supported_driver_map():
    assert runner_for_driver("claude") is run_claude
    assert runner_for_driver("codex") is run_codex
    assert runner_for_driver("opencode") is run_opencode
    assert runner_for_driver("missing") is None


def test_runner_for_backend_selects_protocol_and_forces_adhoc_oneshot():
    live = _validate_backend("claude-live", {
        "driver": "claude",
        "protocol": "interactive-cli",
    })
    codex_live = _validate_backend("codex-live", {
        "driver": "codex",
        "protocol": "interactive-cli",
    })
    cursor_live = _validate_backend("cursor-live", {
        "driver": "cursor",
        "protocol": "interactive-cli",
    })
    opencode_live = _validate_backend("opencode-live", {
        "driver": "opencode",
        "protocol": "interactive-cli",
    })

    assert runner_for_backend(live) is run_claude_interactive_cli
    assert runner_for_backend(live, adhoc=True) is run_claude
    assert runner_for_backend(codex_live) is run_codex_interactive_cli
    assert runner_for_backend(codex_live, adhoc=True) is run_codex
    assert runner_for_backend(cursor_live) is run_cursor_interactive_cli
    assert runner_for_backend(cursor_live, adhoc=True) is runner_for_driver("cursor")
    assert runner_for_backend(opencode_live) is run_opencode_interactive_cli
    assert runner_for_backend(opencode_live, adhoc=True) is run_opencode


def test_claude_interactive_reuses_live_process_for_same_session_key():
    _clear()
    fake_proc = _FakeProcess(9001, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "first prompt"}}),
        json.dumps({"type": "result", "result": "first", "usage": {}}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "second prompt"}}),
        json.dumps({"type": "result", "result": "second", "usage": {}}),
    ])
    created_cmds = []

    async def fake_create_subprocess_exec(*cmd, **_kwargs):
        created_cmds.append(list(cmd))
        return fake_proc

    async def collect(prompt, msg_id, resume_session_id=None):
        return [chunk async for chunk in run_claude_interactive_cli(
            prompt, cwd="/tmp/project", topic="work", agent="claude", msg_id=msg_id,
            resume_session_id=resume_session_id,
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        first = asyncio.run(collect("first prompt", 1))
        # second turn passes the session_id from the first turn, mirroring real usage
        # (topic_queue calls set_topic_session after each turn; server passes it back)
        second = asyncio.run(collect("second prompt", 2, resume_session_id="sess-1"))

    assert created_cmds == [[
        "claude", "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--replay-user-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]]
    assert first[0] == "first"
    assert second[0] == "second"
    payloads = [json.loads(data.decode()) for data in fake_proc.stdin.writes]
    assert [payload["message"]["content"] for payload in payloads] == [
        "first prompt",
        "second prompt",
    ]
    assert list(_claude_interactive_sessions) == [
        ("claude", "work", "claude", "/tmp/project", None, ()),
    ]
    assert _proc_registry[9001]["msg_id"] is None
    assert _proc_registry[9001]["state"] == "idle"
    proc = list_active_procs()[0]
    assert proc["state"] == "idle"
    assert "state_duration_s" in proc
    _clear()


def test_claude_interactive_ignores_results_before_matching_prompt_replay():
    _clear()
    fake_proc = _FakeProcess(9004, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "result", "result": "stale task notification answer", "usage": {}}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "current prompt"}}),
        json.dumps({"type": "result", "result": "current answer", "usage": {}}),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "current prompt", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks[0] == "current answer"
    assert all(chunk != "stale task notification answer" for chunk in chunks)
    _clear()


def test_claude_interactive_waits_for_matching_agent_task_notification():
    _clear()
    fake_proc = _FakeProcess(9005, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "2."}}),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_agent_1", "name": "Agent"},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"description":"scan panels"}'},
            },
        }),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
        json.dumps({"type": "result", "result": "Scanning all panels now.", "usage": {}}),
        json.dumps({
            "type": "user",
            "isReplay": True,
            "message": {
                "role": "user",
                "content": "<task-notification><tool-use-id>toolu_agent_1</tool-use-id><status>completed</status></task-notification>",
            },
        }),
        json.dumps({"type": "result", "result": "Use Esc for dismissible panels, not confirmation modals.", "usage": {}}),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "2.", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Agent", "tool_use_id": "toolu_agent_1", "description": "scan panels"}}
    assert "Scanning all panels now." not in chunks
    assert "Use Esc for dismissible panels, not confirmation modals." in chunks
    _clear()


def test_claude_interactive_starts_with_resume_id_and_skips_history_injection():
    _clear()
    fake_proc = _FakeProcess(9002, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "new prompt"}}),
        json.dumps({"type": "result", "result": "resumed", "usage": {}}),
    ])
    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs["cwd"]
        return fake_proc

    history = [{"role": "user", "content": "old prompt"}]

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "new prompt",
            cwd="/tmp/project",
            history=history,
            topic="work",
            agent="claude",
            resume_session_id="sess-1",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks[0] == "resumed"
    assert captured["cwd"] == "/tmp/project"
    assert captured["cmd"][-2:] == ["--resume", "sess-1"]
    payload = json.loads(fake_proc.stdin.writes[0].decode())
    assert payload["message"]["content"] == "new prompt"
    assert "old prompt" not in payload["message"]["content"]
    _clear()


def test_claude_interactive_closes_process_after_idle_timeout():
    _clear()
    fake_proc = _FakeProcess(9003, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "prompt"}}),
        json.dumps({"type": "result", "result": "done", "usage": {}}),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect_and_wait():
        chunks = [chunk async for chunk in run_claude_interactive_cli(
            "prompt",
            cwd="/tmp/project",
            topic="work",
            agent="claude",
            interactive_idle_timeout_s=0.001,
        )]
        await asyncio.sleep(0.01)
        return chunks

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect_and_wait())

    assert chunks[0] == "done"
    assert fake_proc.terminated is True
    assert 9003 not in _proc_registry
    session = _claude_interactive_sessions[("claude", "work", "claude", "/tmp/project", None, ())]
    assert session.proc is None
    _clear()


# ── kill_procs_by_topic ────────────────────────────────────────────────────────

def test_lifo_kills_most_recent_first():
    """Consecutive LIFO stops walk back: stop→stop kills #2 then #1."""
    _clear()
    _register_proc(101, "claude", "work", "claude", adhoc=True,  msg_id=1)
    _register_proc(102, "claude", "work", "claude", adhoc=True,  msg_id=2)
    # Make pid=102 clearly newer
    _proc_registry[102]["started_at"] = _proc_registry[101]["started_at"] + 1.0

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg") as mock_killpg:
        # First stop — should kill pid=102 (most recent)
        killed = kill_procs_by_topic("work", agent="claude", adhoc=True, lifo=True)
        assert killed == 1
        mock_killpg.assert_called_once_with(102, signal.SIGTERM)
        assert 102 not in _proc_registry  # deregistered immediately

        # Second stop — should kill pid=101 (now the most recent remaining)
        mock_killpg.reset_mock()
        killed = kill_procs_by_topic("work", agent="claude", adhoc=True, lifo=True)
        assert killed == 1
        mock_killpg.assert_called_once_with(101, signal.SIGTERM)
        assert 101 not in _proc_registry

    _clear()


def test_lifo_does_not_rekill_already_stopped():
    """After first LIFO stop, pid is deregistered — second call targets the next one."""
    _clear()
    _register_proc(201, "claude", "work", "claude", adhoc=True, msg_id=10)
    _register_proc(202, "claude", "work", "claude", adhoc=True, msg_id=11)
    _proc_registry[202]["started_at"] = _proc_registry[201]["started_at"] + 1.0

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg"):
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

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg") as mock_killpg:
        killed = kill_procs_by_topic("work")
        assert killed == 2
        killed_pids = {c.args[0] for c in mock_killpg.call_args_list}
        assert killed_pids == {301, 302}

    _clear()


def test_agent_scoped_stop_skips_other_agents():
    """`#topic@agent /stop` only kills processes for that agent."""
    _clear()
    _register_proc(401, "claude", "work", "claude", adhoc=False, msg_id=30)
    _register_proc(402, "codex",  "work", "codex",  adhoc=False, msg_id=31)

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg") as mock_killpg:
        killed = kill_procs_by_topic("work", agent="claude", adhoc=False)
        assert killed == 1
        mock_killpg.assert_called_once_with(401, signal.SIGTERM)

    _clear()


# ── kill_proc_by_msg_id ────────────────────────────────────────────────────────

def test_kill_by_msg_id_targets_exact_process():
    """× button kill sends stop_msg with msg_id — server kills the exact process."""
    _clear()
    _register_proc(501, "claude", "work", "claude", adhoc=True, msg_id=99)
    _register_proc(502, "claude", "work", "claude", adhoc=True, msg_id=100)

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg") as mock_killpg:
        result = kill_proc_by_msg_id(99)
        assert result == 1
        mock_killpg.assert_called_once_with(501, signal.SIGTERM)
        assert 501 not in _proc_registry  # deregistered
        assert 502 in _proc_registry      # untouched

    _clear()


def test_kill_by_msg_id_returns_zero_when_not_found():
    _clear()
    with patch("agent.runners.os.kill"):
        assert kill_proc_by_msg_id(9999) == 0


def test_stop_falls_back_to_parent_pid_when_process_group_unavailable():
    _clear()
    _register_proc(601, "claude", "work", "claude", adhoc=True, msg_id=101)

    with patch("agent.runners.os.getpgid", side_effect=ProcessLookupError), \
         patch("agent.runners.os.kill") as mock_kill:
        result = kill_proc_by_msg_id(101)
        assert result == 1
        mock_kill.assert_called_once_with(601, signal.SIGTERM)

    _clear()


def test_stopall_signals_process_groups():
    _clear()
    _register_proc(701, "claude", "work", "claude", adhoc=False, msg_id=201)
    _register_proc(702, "codex", "work", "codex", adhoc=False, msg_id=202)

    with patch("agent.runners.os.getpgid", side_effect=lambda pid: pid), \
         patch("agent.runners.os.killpg") as mock_killpg:
        killed = kill_all_procs()
        assert killed == 2
        assert {c.args[0] for c in mock_killpg.call_args_list} == {701, 702}

    _clear()


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


def test_codex_routes_commentary_to_status_and_persists_only_final_answer():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"type":"item.completed","item":{"type":"agent_message","phase":"commentary","text":"Checking the code..."}}'
        yield '{"type":"item.completed","item":{"type":"agent_message","phase":"final_answer","text":"Fixed and tested."}}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex("fix it", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_status": "Checking the code..."}
    assert chunks[1] == "Fixed and tested."
    assert chunks[2]["_stats"]["session_id"] == "thread-1"


def test_codex_keeps_legacy_agent_messages_as_response_content():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"Legacy response"}}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex("hello", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "Legacy response"


def test_codex_interactive_cli_uses_structured_exec_resume_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:5] == ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        assert cmd[-2:] == ["thread-1", "next"]
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"next answer"}}'
        yield '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":2}}'

    async def collect():
        return [chunk async for chunk in run_codex_interactive_cli(
            "next", cwd="/tmp", resume_session_id="thread-1", interactive_idle_timeout_s=1,
        )]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next answer"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_cursor_interactive_cli_uses_structured_print_resume_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:4] == ["cursor-agent", "--print", "--output-format", "stream-json"]
        assert "--resume" in cmd
        assert cmd[-2:] == ["thread-1", "next"]
        yield '{"type":"system","session_id":"thread-1"}'
        yield '{"type":"assistant","message":{"content":[{"type":"text","text":"next"}]},"timestamp_ms":1}'
        yield '{"type":"result","session_id":"thread-1","usage":{"inputTokens":10,"outputTokens":2,"cacheReadTokens":4,"cacheWriteTokens":0}}'

    async def collect():
        return [chunk async for chunk in run_cursor_interactive_cli(
            "next", cwd="/tmp", resume_session_id="thread-1", interactive_idle_timeout_s=1,
        )]

    with patch("agent.runners.CURSOR_PATH", "cursor-agent"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_opencode_interactive_cli_uses_structured_run_session_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:4] == ["opencode", "run", "--format", "json"]
        assert cmd[-3:] == ["--session", "thread-1", "next"]
        yield '{"type":"text","sessionID":"thread-1","part":{"text":"next"}}'
        yield '{"type":"step_finish","sessionID":"thread-1","part":{"tokens":{"input":10,"output":2,"cache":{"read":4,"write":0}},"cost":0}}'

    async def collect():
        return [chunk async for chunk in run_opencode_interactive_cli(
            "next", cwd="/tmp", resume_session_id="thread-1", interactive_idle_timeout_s=1,
        )]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_native_claude_removes_inherited_anthropic_auth_environment():
    captured = {}

    async def fake_stream_lines(cmd, **kwargs):
        captured["kwargs"] = kwargs
        yield '{"type":"result","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_claude("hello", cwd="/tmp")]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch("agent.runners._stream_lines", fake_stream_lines):
        asyncio.run(collect())

    assert captured["kwargs"]["extra_env"] == {
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_BASE_URL": None,
        "SQUID_NATIVE_CLAUDE_TOKEN": None,
    }


def test_claude_gateway_backend_keeps_its_explicit_environment():
    captured = {}

    async def fake_stream_lines(cmd, **kwargs):
        captured["kwargs"] = kwargs
        yield '{"type":"result","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_claude(
            "hello", cwd="/tmp", backend_id="deepcla",
            backend_env={
                "ANTHROPIC_AUTH_TOKEN": "deepseek-token",
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            },
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch("agent.runners._stream_lines", fake_stream_lines):
        asyncio.run(collect())

    assert captured["kwargs"]["extra_env"] == {
        "ANTHROPIC_AUTH_TOKEN": "deepseek-token",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    }


def test_claude_routes_partial_text_to_status_and_result_to_response():
    async def fake_stream_lines(*args, **kwargs):
        yield json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Working on it..."},
            },
        })
        yield json.dumps({
            "type": "result",
            "result": "Finished response.",
            "usage": {},
        })

    async def collect():
        return [chunk async for chunk in run_claude("hello", cwd="/tmp")]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_status": "Working on it..."}
    assert chunks[1] == "Finished response."
    assert "_stats" in chunks[2]


def test_codex_backend_configuration_reaches_command_and_process_metadata():
    captured = {}

    async def fake_stream_lines(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex(
            "hello", cwd="/tmp", backend_id="local-codex",
            backend_env={"LOCAL_TOKEN": "token"},
            backend_settings={"model_provider": "vllm_mlx"},
        )]

    with patch("agent.runners.CODEX_PATH", "codex"), patch("agent.runners._stream_lines", fake_stream_lines):
        asyncio.run(collect())

    assert ["-c", 'model_provider="vllm_mlx"'] == captured["cmd"][5:7]
    assert captured["kwargs"]["backend"] == "local-codex"
    assert captured["kwargs"]["extra_env"] == {"LOCAL_TOKEN": "token"}


def test_codex_file_change_event_yields_diff_tool():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"method":"item/fileChange/patchUpdated","params":{"path":"ui/app.js","unified_diff":"@@ -1 +1 @@\\n-old\\n+new"}}'
        yield '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":1}}'

    async def collect():
        return [chunk async for chunk in run_codex("edit", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch("agent.runners._stream_lines", fake_stream_lines):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Diff", "file": "ui/app.js", "diff": "@@ -1 +1 @@\n-old\n+new"}}
