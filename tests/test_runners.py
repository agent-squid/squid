"""
Unit tests for runners.py process registry and kill functions.
"""
import asyncio
import json
import signal
import time
from types import SimpleNamespace
from unittest.mock import patch, call, AsyncMock
import pytest

from agent.runners import (
    _claude_interactive_sessions,
    _child_env,
    _proc_registry,
    _register_proc,
    _stream_lines,
    _deregister_proc,
    kill_all_procs,
    kill_procs_by_topic,
    kill_proc_by_msg_id,
    list_active_procs,
    runner_for_backend,
    runner_for_harness,
    run_claude,
    run_claude_interactive_cli,
    run_codex,
    run_cursor,
    run_echo,
    run_opencode,
    run_pi,
)


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


def test_runner_for_harness_uses_shared_supported_harness_map():
    assert runner_for_harness("claudecode") is run_claude
    assert runner_for_harness("codex") is run_codex
    assert runner_for_harness("opencode") is run_opencode
    assert runner_for_harness("pi") is run_pi
    assert runner_for_harness("missing") is None


def test_runner_for_backend_selects_protocol_and_forces_adhoc_oneshot():
    live = SimpleNamespace(harness="claudecode", protocol="interactive-cli")
    codex = SimpleNamespace(harness="codex", protocol="oneshot-cli")
    cursor = SimpleNamespace(harness="cursor", protocol="oneshot-cli")
    opencode = SimpleNamespace(harness="opencode", protocol="oneshot-cli")
    pi = SimpleNamespace(harness="pi", protocol="oneshot-cli")

    assert runner_for_backend(live) is run_claude_interactive_cli
    assert runner_for_backend(live, adhoc=True) is run_claude
    assert runner_for_backend(codex) is run_codex
    assert runner_for_backend(cursor) is runner_for_harness("cursor")
    assert runner_for_backend(opencode) is run_opencode
    assert runner_for_backend(pi) is run_pi


@pytest.mark.parametrize("harness,oneshot_runner", [
    ("claudecode", run_claude),
    ("codex", run_codex),
    ("cursor", run_cursor),
    ("opencode", run_opencode),
    ("pi", run_pi),
])
def test_runner_for_backend_selects_oneshot_cli(harness, oneshot_runner):
    oneshot = SimpleNamespace(harness=harness, protocol="oneshot-cli")

    assert runner_for_backend(oneshot) is oneshot_runner


def test_runner_for_backend_selects_claude_interactive_cli():
    interactive = SimpleNamespace(harness="claudecode", protocol="interactive-cli")

    assert runner_for_backend(interactive) is run_claude_interactive_cli


@pytest.mark.parametrize("harness", ["codex", "cursor", "opencode", "pi"])
def test_runner_for_backend_does_not_route_non_persistent_interactive_cli(harness):
    backend = SimpleNamespace(harness=harness, protocol="interactive-cli")

    assert runner_for_backend(backend) is None


def test_run_echo_echoes_prompt_and_yields_stats():
    # run_echo itself is always importable/callable regardless of
    # SQUID_TEST_HARNESS — only its *registration* (SUPPORTED_HARNESSES,
    # PROVIDERS) is gated behind that flag (see agent/config.py). No
    # subprocess, no network call, but it does wait a real random 5-10s
    # before replying (deliberately — see run_echo's docstring), so tests
    # patch that sleep away to stay fast.
    async def collect():
        with patch("agent.runners.asyncio.sleep", new=AsyncMock()):
            return [chunk async for chunk in run_echo("hello world", msg_id=42, topic="t", agent="a")]

    chunks = asyncio.run(collect())
    assert chunks[0] == "echo: hello world"
    assert len(chunks) == 2
    stats = chunks[1]["_stats"]
    assert stats["session_id"] == "echo-42"
    assert stats["output_tokens"] > 0


def test_run_echo_resumes_the_given_session_id():
    async def collect():
        with patch("agent.runners.asyncio.sleep", new=AsyncMock()):
            return [chunk async for chunk in run_echo("hi", resume_session_id="prior-session", msg_id=99)]

    chunks = asyncio.run(collect())
    assert chunks[1]["_stats"]["session_id"] == "prior-session"


def test_child_env_applies_backend_env(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("REMOVE_ME", "1")

    env = _child_env({"PATH": "/custom/bin", "TOKEN": "kept", "REMOVE_ME": None})

    assert env["PATH"] == "/custom/bin"
    assert env["TOKEN"] == "kept"
    assert "REMOVE_ME" not in env


def test_stream_lines_sets_squid_turn_env(monkeypatch):
    captured = {}
    fake_proc = _FakeProcess(9100, ["ok\n"])

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return fake_proc

    async def collect():
        return [line async for line in _stream_lines(
            ["agent"],
            topic="squid",
            agent="codex",
            msg_id=7120,
        )]

    with patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        assert asyncio.run(collect()) == ["ok"]

    assert captured["env"]["SQUID_TOPIC"] == "squid"
    assert captured["env"]["SQUID_AGENT"] == "codex"
    assert captured["env"]["SQUID_MSG_ID"] == "7120"


def test_claude_oneshot_cli_passes_prompt_as_process_argument():
    captured = {}

    async def fake_stream_lines(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        yield json.dumps({"type": "result", "result": "answer", "usage": {}})

    async def collect():
        return [chunk async for chunk in run_claude("fresh prompt", cwd="/tmp/project", msg_id=123)]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert captured["cmd"][:2] == ["claude", "--print"]
    assert "--mcp-config" not in captured["cmd"]
    assert "--append-system-prompt" not in captured["cmd"]
    assert captured["cmd"][-2] == "--"
    assert captured["cmd"][-1] == "fresh prompt"
    assert captured["kwargs"]["prompt"] == "fresh prompt"
    assert chunks[0] == "answer"


def test_claude_init_event_does_not_yield_mcp_diagnostics():
    async def fake_stream_lines(_cmd, **_kwargs):
        yield json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "sess-1",
            "tools": ["Read", "Edit"],
        })
        yield json.dumps({"type": "result", "result": "answer", "usage": {}})

    async def collect():
        return [chunk async for chunk in run_claude("fresh prompt", cwd="/tmp/project", msg_id=123)]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "answer"
    assert chunks[1]["_stats"]["session_id"] == "sess-1"


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
        ("claudecode", "work", "claude", "/tmp/project", None, ()),
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


def test_claude_interactive_handles_assistant_message_events():
    _clear()
    fake_proc = _FakeProcess(9008, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "current prompt"}}),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Checking the code."}],
                "stop_reason": "tool_use",
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_bash_1",
                    "name": "Bash",
                    "input": {"command": "git status", "description": "Check status"},
                }],
                "stop_reason": "tool_use",
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Final answer."}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
            },
            "total_cost_usd": 0.001,
            "duration_ms": 1234,
        }),
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

    assert {"_status": "Checking the code."} in chunks
    assert {"_tool": {"name": "Bash", "tool_use_id": "toolu_bash_1", "command": "git status"}} in chunks
    assert "Final answer." in chunks
    stats = next(chunk["_stats"] for chunk in chunks if isinstance(chunk, dict) and "_stats" in chunk)
    assert stats["input_tokens"] == 1
    assert stats["output_tokens"] == 2
    assert stats["cache_read_tokens"] == 3
    assert stats["cache_write_tokens"] == 4
    assert stats["cost_usd"] == 0.001
    assert stats["duration_ms"] == 1234
    _clear()


def test_claude_interactive_skips_thinking_only_assistant_end_turn():
    _clear()
    fake_proc = _FakeProcess(9009, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "current prompt"}}),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "internal only"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 9, "output_tokens": 9},
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Visible final."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }),
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

    assert "Visible final." in chunks
    assert not any(isinstance(chunk, dict) and chunk.get("_stats", {}).get("input_tokens") == 9 for chunk in chunks)
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
    assert {"_status": "[Agent: scan panels] Scanning all panels now.\n"} in chunks
    assert "Use Esc for dismissible panels, not confirmation modals." in chunks
    _clear()


def test_claude_interactive_accepts_queue_operation_agent_task_notification():
    _clear()
    fake_proc = _FakeProcess(9006, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "go"}}),
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
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n"
                "<tool-use-id>toolu_agent_1</tool-use-id>\n"
                "<status>completed</status>\n"
                "</task-notification>"
            ),
        }),
        json.dumps({"type": "result", "result": "Final answer after agent completion.", "usage": {}}),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "go", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Agent", "tool_use_id": "toolu_agent_1", "description": "scan panels"}}
    assert {"_status": "[Agent: scan panels] Scanning all panels now.\n"} in chunks
    assert "Final answer after agent completion." in chunks
    _clear()


def test_claude_interactive_skips_empty_result_after_agent_task_notification():
    _clear()
    task_notification = (
        "<task-notification>\n"
        "<tool-use-id>toolu_agent_1</tool-use-id>\n"
        "<status>completed</status>\n"
        "</task-notification>"
    )
    fake_proc = _FakeProcess(9007, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "go"}}),
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
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": task_notification}),
        json.dumps({"type": "queue-operation", "operation": "dequeue", "content": task_notification}),
        json.dumps({
            "type": "user",
            "isReplay": True,
            "message": {"role": "user", "content": task_notification},
        }),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        json.dumps({"type": "result", "result": "Final answer after agent completion.", "usage": {}}),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "go", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Agent", "tool_use_id": "toolu_agent_1", "description": "scan panels"}}
    assert {"_status": "[Agent: scan panels] Scanning all panels now.\n"} in chunks
    assert "Final answer after agent completion." in chunks
    assert not any(isinstance(chunk, dict) and chunk.get("_stats", {}).get("input_tokens") == 1 for chunk in chunks)
    _clear()


def test_claude_interactive_final_assistant_text_after_agent_is_response_not_status():
    _clear()
    task_notification = (
        "<task-notification>\n"
        "<tool-use-id>toolu_agent_1</tool-use-id>\n"
        "<status>completed</status>\n"
        "</task-notification>"
    )
    fake_proc = _FakeProcess(9010, [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "go"}}),
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
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": task_notification}}),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "internal only"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 9, "output_tokens": 9},
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Both fixes applied."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "total_cost_usd": 0.001,
            "duration_ms": 123,
        }),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "go", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert {"_status": "[Agent: scan panels] Scanning all panels now.\n"} in chunks
    assert "Both fixes applied." in chunks
    assert {"_status": "[Agent: scan panels] Both fixes applied.\n"} not in chunks
    stats = next(chunk["_stats"] for chunk in chunks if isinstance(chunk, dict) and "_stats" in chunk)
    assert stats["input_tokens"] == 1
    assert stats["output_tokens"] == 2
    assert stats["cost_usd"] == 0.001
    assert stats["duration_ms"] == 123
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
    session = _claude_interactive_sessions[("claudecode", "work", "claude", "/tmp/project", None, ())]
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


# ── ask_followup_question handling ────────────────────────────────────────────

class _HangingStdout:
    """Stdout that serves lines then blocks forever (simulates Claude Code waiting for tool result)."""
    def __init__(self, lines):
        self.lines = [l.encode() for l in lines]

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        await asyncio.sleep(1000)
        return b""


def _ask_followup_stream_events(tool_use_id: str, question: str, options=None) -> list[str]:
    inp: dict = {"question": question}
    if options is not None:
        inp["options"] = options
    return [
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": tool_use_id, "name": "ask_followup_question"},
            },
        }),
        json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)},
            },
        }),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
    ]


def test_claude_interactive_soft_completes_on_ask_followup_question():
    """When Claude emits ask_followup_question then blocks, Squid soft-completes:
    surfaces the question as response text and stores pending_followup."""
    _clear()
    fake_proc = _FakeProcess(9010, [])
    fake_proc.stdout = _HangingStdout([
        json.dumps({"type": "system", "session_id": "sess-ask"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "do the thing"}}),
        *_ask_followup_stream_events("toolu_ask_1", "Which branch should I use?"),
        # no result event — Claude Code blocks here
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "do the thing", cwd="/tmp/project", topic="work", agent="claude",
            interactive_idle_timeout_s=3600,
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners._ASK_FOLLOWUP_RESULT_WAIT", 0.05), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks == ["Which branch should I use?"]
    session = _claude_interactive_sessions[("claudecode", "work", "claude", "/tmp/project", None, ())]
    assert session.pending_followup == {"tool_use_id": "toolu_ask_1"}
    assert session.proc is not None and session.proc.returncode is None  # process kept alive
    _clear()


def test_claude_interactive_sends_parent_tool_use_id_on_reply_to_followup():
    """After a soft-complete with pending_followup, the next query sends
    parent_tool_use_id so Claude Code receives the answer as a tool result."""
    _clear()

    class _TwoTurnStdout:
        def __init__(self, turn1_lines, turn2_lines):
            self._lines = [l.encode() for l in turn1_lines]
            self._turn2 = [l.encode() for l in turn2_lines]
            self.advanced = False

        def advance(self):
            self.advanced = True

        async def readline(self):
            if self._lines:
                return self._lines.pop(0)
            if not self.advanced:
                await asyncio.sleep(1000)  # block until soft-complete fires
            if self._turn2:
                return self._turn2.pop(0)
            return b""

    stdout = _TwoTurnStdout(
        turn1_lines=[
            json.dumps({"type": "system", "session_id": "sess-ask"}),
            json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "do the thing"}}),
            *_ask_followup_stream_events("toolu_ask_1", "Which branch?"),
        ],
        turn2_lines=[
            json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "main branch"}}),
            json.dumps({"type": "result", "result": "Got it, using main.", "usage": {}}),
        ],
    )

    fake_proc = _FakeProcess(9011, [])
    fake_proc.stdout = stdout

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def run_two_turns():
        chunks1 = [chunk async for chunk in run_claude_interactive_cli(
            "do the thing", cwd="/tmp/project", topic="work", agent="claude",
            interactive_idle_timeout_s=3600,
        )]
        stdout.advance()
        chunks2 = [chunk async for chunk in run_claude_interactive_cli(
            "main branch", cwd="/tmp/project", topic="work", agent="claude",
            resume_session_id="sess-ask", interactive_idle_timeout_s=3600,
        )]
        return chunks1, chunks2

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners._ASK_FOLLOWUP_RESULT_WAIT", 0.05), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks1, chunks2 = asyncio.run(run_two_turns())

    assert chunks1[0] == "Which branch?"
    assert any(c == "Got it, using main." for c in chunks2)

    payloads = [json.loads(data.decode()) for data in fake_proc.stdin.writes]
    assert payloads[0]["message"]["content"] == "do the thing"
    assert "parent_tool_use_id" not in payloads[0]  # omitted when there is no pending followup
    assert payloads[1]["message"]["content"] == "main branch"
    assert payloads[1]["parent_tool_use_id"] == "toolu_ask_1"
    _clear()


def test_claude_interactive_surfaces_question_when_result_fires_empty_after_followup():
    """When Claude Code auto-handles ask_followup_question and result fires with
    empty text, Squid inserts the question text as the response."""
    _clear()
    fake_proc = _FakeProcess(9012, [
        json.dumps({"type": "system", "session_id": "sess-ask"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "do the thing"}}),
        *_ask_followup_stream_events("toolu_ask_2", "What directory?"),
        json.dumps({"type": "result", "result": "", "usage": {}}),  # auto-handled, empty result
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "do the thing", cwd="/tmp/project", topic="work", agent="claude",
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert any(c == "What directory?" for c in chunks)
    _clear()


def test_claude_interactive_surfaces_followup_options():
    """When ask_followup_question offers options, they are surfaced alongside the
    question as a bulleted list in the soft-completed response text."""
    _clear()
    fake_proc = _FakeProcess(9013, [])
    fake_proc.stdout = _HangingStdout([
        json.dumps({"type": "system", "session_id": "sess-ask"}),
        json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "do the thing"}}),
        *_ask_followup_stream_events("toolu_ask_3", "Which branch?", options=["main", "develop"]),
    ])

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        return fake_proc

    async def collect():
        return [chunk async for chunk in run_claude_interactive_cli(
            "do the thing", cwd="/tmp/project", topic="work", agent="claude",
            interactive_idle_timeout_s=3600,
        )]

    with patch("agent.runners.CLAUDE_PATH", "claude"), \
         patch("agent.runners._ASK_FOLLOWUP_RESULT_WAIT", 0.05), \
         patch("agent.runners.asyncio.create_subprocess_exec", fake_create_subprocess_exec):
        chunks = asyncio.run(collect())

    assert chunks == ["Which branch?\n\n- main\n- develop"]
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


def test_codex_routes_jsonl_commentary_to_status_and_dedupes_response_items():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"session_meta","payload":{"id":"thread-jsonl"}}'
        yield '{"type":"event_msg","payload":{"type":"agent_message","phase":"commentary","message":"Checking the code..."}}'
        yield '{"type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"Checking the code..."}]}}'
        yield '{"type":"event_msg","payload":{"type":"agent_message","phase":"final_answer","message":"Fixed and tested."}}'
        yield '{"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"Fixed and tested."}]}}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex("fix it", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_status": "Checking the code..."}
    assert chunks[1] == "Fixed and tested."
    assert chunks[2]["_stats"]["session_id"] == "thread-jsonl"


def test_codex_persists_jsonl_image_generation_results(tmp_path):
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAFgwJ/lxb8WQAAAABJRU5ErkJggg=="

    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"session_meta","payload":{"id":"thread-images"}}'
        yield json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "image_generation_end",
                "call_id": "ig_123",
                "status": "generating",
                "result": png_b64,
            },
        })
        yield json.dumps({
            "type": "response_item",
            "payload": {
                "type": "image_generation_call",
                "id": "ig_123",
                "status": "generating",
                "result": png_b64,
            },
        })
        yield '{"type":"event_msg","payload":{"type":"agent_message","phase":"final_answer","message":"Done."}}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex("draw", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ), patch("agent.runners.Path.home", return_value=tmp_path):
        chunks = asyncio.run(collect())

    assert chunks[0].startswith("![Generated image 1](")
    assert chunks[0].endswith("01-ig_123.png)\n\n")
    assert chunks[1] == "Done."
    image_path = tmp_path / ".squid" / "artifacts" / "codex-images" / "thread-images" / "01-ig_123.png"
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_codex_routes_phase_less_progress_before_tool_to_status():
    async def fake_stream_lines(*args, **kwargs):
        yield '{"type":"thread.started","thread_id":"thread-legacy"}'
        yield '{"type":"turn.started"}'
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"Checking the code..."}}'
        yield '{"type":"item.completed","item":{"type":"command_execution","command":"/bin/zsh -lc pwd","aggregated_output":"/tmp","exit_code":0,"status":"completed"}}'
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"Fixed and tested."}}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_codex("fix it", cwd="/tmp")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_status": "Checking the code..."}
    assert chunks[1] == {"_tool": {"name": "Bash", "command": "/bin/zsh -lc pwd"}}
    assert chunks[2] == "Fixed and tested."
    assert chunks[3]["_stats"]["session_id"] == "thread-legacy"


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


def test_codex_oneshot_resume_uses_structured_exec_resume_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:5] == ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        assert cmd[-2:] == ["thread-1", "next"]
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"next answer"}}'
        yield '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":2}}'

    async def collect():
        return [chunk async for chunk in run_codex("next", cwd="/tmp", resume_session_id="thread-1")]

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next answer"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_codex_oneshot_fresh_vs_resume_command_shape():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"thread.started","thread_id":"thread-1"}'
        yield '{"type":"turn.completed","usage":{}}'

    async def collect():
        oneshot = [chunk async for chunk in run_codex("fresh", cwd="/tmp")]
        resumed = [chunk async for chunk in run_codex("next", cwd="/tmp", resume_session_id="thread-1")]
        return oneshot, resumed

    with patch("agent.runners.CODEX_PATH", "codex"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert captured[0][:3] == ["codex", "exec", "--json"]
    assert "resume" not in captured[0]
    assert not any("mcp_servers.squid" in arg for arg in captured[0])
    assert captured[0][-1] == "fresh"
    assert captured[1][:4] == ["codex", "exec", "resume", "--json"]
    assert captured[1][-2:] == ["thread-1", "next"]


def test_cursor_oneshot_resume_uses_structured_print_resume_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:4] == ["cursor-agent", "--print", "--output-format", "stream-json"]
        assert "--workspace" in cmd
        assert cmd[cmd.index("--workspace") + 1] == "/tmp"
        assert "--resume" in cmd
        assert cmd[-2:] == ["thread-1", "next"]
        yield '{"type":"system","session_id":"thread-1"}'
        yield '{"type":"assistant","message":{"content":[{"type":"text","text":"next"}]},"timestamp_ms":1}'
        yield '{"type":"result","session_id":"thread-1","usage":{"inputTokens":10,"outputTokens":2,"cacheReadTokens":4,"cacheWriteTokens":0}}'

    async def collect():
        return [chunk async for chunk in run_cursor("next", cwd="/tmp", resume_session_id="thread-1")]

    with patch("agent.runners.CURSOR_PATH", "cursor-agent"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_cursor_routes_thinking_to_status_and_final_assistant_to_response():
    async def fake_stream_lines(cmd, **kwargs):
        yield '{"type":"system","session_id":"thread-1"}'
        yield '{"type":"thinking","subtype":"delta","text":"Checking files","session_id":"thread-1","timestamp_ms":1}'
        yield '{"type":"assistant","message":{"content":[{"type":"text","text":"Final answer"}]},"session_id":"thread-1"}'
        yield '{"type":"result","session_id":"thread-1","result":"Final answer","usage":{"inputTokens":10,"outputTokens":2}}'

    async def collect():
        return [chunk async for chunk in run_cursor("next", cwd="/tmp")]

    with patch("agent.runners.CURSOR_PATH", "cursor-agent"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_status": "Checking files"}
    assert chunks[1] == "Final answer"
    assert chunks[2]["_stats"]["session_id"] == "thread-1"


def test_cursor_uses_result_text_when_no_assistant_text_streamed():
    async def fake_stream_lines(cmd, **kwargs):
        yield '{"type":"system","session_id":"thread-1"}'
        yield '{"type":"result","session_id":"thread-1","result":"Recovered final","usage":{}}'

    async def collect():
        return [chunk async for chunk in run_cursor("next", cwd="/tmp")]

    with patch("agent.runners.CURSOR_PATH", "cursor-agent"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "Recovered final"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_cursor_oneshot_fresh_vs_resume_command_shape():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"system","session_id":"thread-1"}'
        yield '{"type":"result","session_id":"thread-1","usage":{}}'

    async def collect():
        oneshot = [chunk async for chunk in run_cursor("fresh", cwd="/tmp")]
        resumed = [chunk async for chunk in run_cursor("next", cwd="/tmp", resume_session_id="thread-1")]
        return oneshot, resumed

    with patch("agent.runners.CURSOR_PATH", "cursor-agent"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert captured[0][:4] == ["cursor-agent", "--print", "--output-format", "stream-json"]
    assert captured[0][captured[0].index("--workspace") + 1] == "/tmp"
    assert "--resume" not in captured[0]
    assert captured[0][-1] == "fresh"
    assert captured[1][:4] == ["cursor-agent", "--print", "--output-format", "stream-json"]
    assert captured[1][captured[1].index("--workspace") + 1] == "/tmp"
    assert "--resume" in captured[1]
    assert captured[1][-2:] == ["thread-1", "next"]


def test_opencode_oneshot_resume_uses_structured_run_session_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:4] == ["opencode", "run", "--format", "json"]
        assert cmd[4:7] == ["--dangerously-skip-permissions", "--dir", "/tmp"]
        assert cmd[-3:] == ["--session", "thread-1", "next"]
        yield '{"type":"text","sessionID":"thread-1","part":{"text":"next"}}'
        yield '{"type":"step_finish","sessionID":"thread-1","part":{"tokens":{"input":10,"output":2,"cache":{"read":4,"write":0}},"cost":0}}'

    async def collect():
        return [chunk async for chunk in run_opencode("next", cwd="/tmp", resume_session_id="thread-1")]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next"
    assert chunks[1]["_stats"]["session_id"] == "thread-1"


def test_pi_oneshot_resume_uses_session_id_and_json_events():
    async def fake_stream_lines(cmd, **kwargs):
        assert cmd[:4] == ["pi", "-p", "--mode", "json"]
        assert cmd[-3:] == ["--session-id", "thread-1", "next"]
        yield json.dumps({"type": "session", "id": "thread-1"})
        yield json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "next"}],
                "model": "openai/gpt-5",
                "stopReason": "stop",
                "usage": {
                    "input": 10,
                    "output": 2,
                    "cacheRead": 4,
                    "cacheWrite": 1,
                    "cost": {"total": 0.01},
                },
            },
        })
        yield json.dumps({"type": "agent_end"})

    async def collect():
        return [chunk async for chunk in run_pi("next", cwd="/tmp", resume_session_id="thread-1")]

    with patch("agent.runners.PI_PATH", "pi"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "next"
    stats = chunks[1]["_stats"]
    assert stats["session_id"] == "thread-1"
    assert stats["model"] == "openai/gpt-5"
    assert stats["input_tokens"] == 10
    assert stats["output_tokens"] == 2
    assert stats["cache_read_tokens"] == 4
    assert stats["cache_write_tokens"] == 1
    assert stats["cost_usd"] == 0.01


def test_pi_oneshot_fresh_vs_resume_command_shape():
    captured = []
    captured_cwd = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        captured_cwd.append(kwargs["cwd"])
        yield '{"type":"session","id":"thread-1"}'
        yield '{"type":"message_end","message":{"role":"assistant","content":[],"usage":{}}}'
        yield '{"type":"agent_end"}'

    async def collect():
        oneshot = [chunk async for chunk in run_pi("fresh", cwd="/tmp")]
        resumed = [chunk async for chunk in run_pi("next", cwd="/tmp", resume_session_id="thread-1")]
        return oneshot, resumed

    with patch("agent.runners.PI_PATH", "pi"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert captured[0][:4] == ["pi", "-p", "--mode", "json"]
    assert captured_cwd[0] == "/tmp"
    assert "--session-id" not in captured[0]
    assert captured[0][-1] == "fresh"
    assert captured[1][:4] == ["pi", "-p", "--mode", "json"]
    assert captured_cwd[1] == "/tmp"
    assert captured[1][-3:] == ["--session-id", "thread-1", "next"]


def test_pi_passes_provider_and_model():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"session","id":"thread-1"}'
        yield '{"type":"agent_end"}'

    async def collect():
        return [chunk async for chunk in run_pi(
            "fresh", cwd="/tmp", model="gpt-5.5",
            backend_settings={"provider": "openai"},
        )]

    with patch("agent.runners.PI_PATH", "pi"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert "--provider" not in captured[0]
    assert "--model" in captured[0]
    assert captured[0][captured[0].index("--model") + 1] == "openai/gpt-5.5"


def test_pi_does_not_double_prefix_provider_model():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"session","id":"thread-1"}'
        yield '{"type":"agent_end"}'

    async def collect():
        return [chunk async for chunk in run_pi(
            "fresh", cwd="/tmp", model="nvidia/nemotron-3-super-120b-a12b",
            backend_settings={"provider": "nvidia"},
        )]

    with patch("agent.runners.PI_PATH", "pi"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert "--model" in captured[0]
    assert captured[0][captured[0].index("--model") + 1] == "nvidia/nemotron-3-super-120b-a12b"


def test_pi_maps_tool_calls_and_waits_for_final_stats():
    async def fake_stream_lines(cmd, **kwargs):
        yield json.dumps({"type": "session", "id": "thread-1"})
        yield json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_end",
                "toolCall": {
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "pwd"},
                },
            },
        })
        yield json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "pwd"}}],
                "usage": {"input": 5, "output": 6, "cost": {"total": 0.1}},
                "stopReason": "toolUse",
            },
        })
        yield json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "model": "gpt-5.5",
                "usage": {"input": 7, "output": 8, "cost": {"total": 0.2}},
                "stopReason": "stop",
            },
        })
        yield json.dumps({"type": "agent_end"})

    async def collect():
        return [chunk async for chunk in run_pi("next", cwd="/tmp")]

    with patch("agent.runners.PI_PATH", "pi"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Bash", "tool_use_id": "call-1", "command": "pwd"}}
    assert chunks[1] == "done"
    stats = chunks[2]["_stats"]
    assert stats["model"] == "gpt-5.5"
    assert stats["input_tokens"] == 12
    assert stats["output_tokens"] == 14
    assert stats["cost_usd"] == 0.30000000000000004


def test_opencode_routes_tool_call_step_text_to_status_and_stop_text_to_final():
    async def fake_stream_lines(cmd, **kwargs):
        yield json.dumps({
            "type": "tool_use",
            "sessionID": "thread-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "pwd"},
                    "output": "/tmp\n",
                },
            },
        })
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "Checking the directory."},
        })
        yield json.dumps({
            "type": "step_finish",
            "sessionID": "thread-1",
            "part": {"reason": "tool-calls", "tokens": {"input": 10, "output": 2}},
        })
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "The current directory is `/tmp`."},
        })
        yield json.dumps({
            "type": "step_finish",
            "sessionID": "thread-1",
            "part": {"reason": "stop", "tokens": {"input": 3, "output": 8}},
        })

    async def collect():
        return [chunk async for chunk in run_opencode("pwd", cwd="/tmp")]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Bash", "command": "pwd"}}
    assert chunks[1] == {"_status": "Checking the directory."}
    assert chunks[2] == "The current directory is `/tmp`."
    assert chunks[3]["_stats"]["session_id"] == "thread-1"
    assert chunks[3]["_stats"]["input_tokens"] == 13
    assert chunks[3]["_stats"]["output_tokens"] == 10


def test_opencode_preserves_answer_text_before_tool_use():
    async def fake_stream_lines(cmd, **kwargs):
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "## Review\n\n**1. First issue.**"},
        })
        yield json.dumps({
            "type": "tool_use",
            "sessionID": "thread-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git diff"},
                    "output": "",
                },
            },
        })
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "\n\n**2. Second issue.**"},
        })
        yield json.dumps({
            "type": "step_finish",
            "sessionID": "thread-1",
            "part": {"reason": "stop", "tokens": {"input": 4, "output": 5}},
        })

    async def collect():
        return [chunk async for chunk in run_opencode("review", cwd="/tmp")]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == "## Review\n\n**1. First issue.**"
    assert chunks[1] == {"_tool": {"name": "Bash", "command": "git diff"}}
    assert chunks[2] == "\n\n**2. Second issue.**"
    assert chunks[3]["_stats"]["session_id"] == "thread-1"


def test_opencode_preserves_markdown_answer_text_from_tool_call_step():
    async def fake_stream_lines(cmd, **kwargs):
        yield json.dumps({
            "type": "tool_use",
            "sessionID": "thread-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "git diff"},
                    "output": "",
                },
            },
        })
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "## Summary\n\nThe change is sound.\n\n## Critical Issues\n\n**1. First issue.**"},
        })
        yield json.dumps({
            "type": "step_finish",
            "sessionID": "thread-1",
            "part": {"reason": "tool-calls", "tokens": {"input": 8, "output": 10}},
        })
        yield json.dumps({
            "type": "text",
            "sessionID": "thread-1",
            "part": {"text": "\n\n**2. Second issue.**"},
        })
        yield json.dumps({
            "type": "step_finish",
            "sessionID": "thread-1",
            "part": {"reason": "stop", "tokens": {"input": 3, "output": 4}},
        })

    async def collect():
        return [chunk async for chunk in run_opencode("review", cwd="/tmp")]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    assert chunks[0] == {"_tool": {"name": "Bash", "command": "git diff"}}
    assert chunks[1] == "## Summary\n\nThe change is sound.\n\n## Critical Issues\n\n**1. First issue.**"
    assert chunks[2] == "\n\n**2. Second issue.**"
    assert chunks[3]["_stats"]["session_id"] == "thread-1"


def test_opencode_oneshot_fresh_vs_resume_command_shape():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"step_finish","sessionID":"thread-1","part":{"tokens":{}}}'

    async def collect():
        oneshot = [chunk async for chunk in run_opencode("fresh", cwd="/tmp")]
        resumed = [chunk async for chunk in run_opencode("next", cwd="/tmp", resume_session_id="thread-1")]
        return oneshot, resumed

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert captured[0][:4] == ["opencode", "run", "--format", "json"]
    assert captured[0][4:7] == ["--dangerously-skip-permissions", "--dir", "/tmp"]
    assert "--session" not in captured[0]
    assert captured[0][-1] == "fresh"
    assert captured[1][:4] == ["opencode", "run", "--format", "json"]
    assert captured[1][4:7] == ["--dangerously-skip-permissions", "--dir", "/tmp"]
    assert captured[1][-3:] == ["--session", "thread-1", "next"]


def test_opencode_composes_provider_prefixed_model():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"step_finish","sessionID":"thread-1","part":{"tokens":{}}}'

    async def collect():
        return [chunk async for chunk in run_opencode(
            "fresh", cwd="/tmp", model="deepseek-ai/deepseek-v4-pro",
            backend_settings={"provider": "nvidia"},
        )]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert "-m" in captured[0]
    assert captured[0][captured[0].index("-m") + 1] == "nvidia/deepseek-ai/deepseek-v4-pro"


def test_opencode_does_not_double_prefix_provider_qualified_model():
    captured = []

    async def fake_stream_lines(cmd, **kwargs):
        captured.append(cmd)
        yield '{"type":"step_finish","sessionID":"thread-1","part":{"tokens":{}}}'

    async def collect():
        return [chunk async for chunk in run_opencode(
            "fresh", cwd="/tmp", model="opencode/deepseek-v4-flash-free",
            backend_settings={"provider": "opencode"},
        )]

    with patch("agent.runners.OPENCODE_PATH", "opencode"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        asyncio.run(collect())

    assert "-m" in captured[0]
    assert captured[0][captured[0].index("-m") + 1] == "opencode/deepseek-v4-flash-free"


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
            # backend_id is the harness ("claudecode"), not the provider — the
            # deepseek provider also runs on the claudecode harness, so this
            # must match what topic_queue.py actually passes in production.
            "hello", cwd="/tmp", backend_id="claudecode",
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
        "ANTHROPIC_API_KEY": None,
        "SQUID_NATIVE_CLAUDE_TOKEN": None,
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


def test_claude_does_not_duplicate_tool_use_between_stream_event_and_assistant():
    # With --include-partial-messages, Claude Code emits a tool_use both via
    # the granular stream_event content_block_stop *and* again inside the
    # consolidated "assistant" event for the same turn. Both share the same
    # tool_use id, so the second occurrence must be suppressed.
    tool_use_id = "toolu_dup123"

    async def fake_stream_lines(*args, **kwargs):
        yield json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": tool_use_id, "name": "Edit"},
            },
        })
        yield json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"file":"a.py"}'},
            },
        })
        yield json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
        })
        yield json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Edit", "input": {"file": "a.py"}},
                ],
                "stop_reason": "tool_use",
            },
        })
        yield json.dumps({"type": "result", "result": "", "usage": {}})

    async def collect():
        return [chunk async for chunk in run_claude("hello", cwd="/tmp")]

    with patch("agent.runners.CLAUDE_PATH", "claude"), patch(
        "agent.runners._stream_lines", fake_stream_lines
    ):
        chunks = asyncio.run(collect())

    tool_chunks = [c for c in chunks if isinstance(c, dict) and "_tool" in c]
    assert len(tool_chunks) == 1
    assert tool_chunks[0]["_tool"]["tool_use_id"] == tool_use_id


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

    assert 'model_provider="vllm_mlx"' in captured["cmd"]
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
