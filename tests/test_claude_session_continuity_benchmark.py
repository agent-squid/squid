from pathlib import Path

import pytest

from benchmarks.claude_session_continuity.benchmark import (
    BenchmarkError,
    ClaudeGauge,
    ClaudeStreamProcess,
    GaugeSnapshot,
    claude_command,
    run_prompts,
)


MOCK_CLAUDE = r'''#!/usr/bin/env python3
import json
import sys

remembered = None
for line in sys.stdin:
    incoming = json.loads(line)
    content = incoming["message"]["content"][0]["text"]
    session_id = incoming["session_id"]
    print(json.dumps({
        "type": "system", "subtype": "init", "session_id": session_id,
        "model": "mock-claude",
    }), flush=True)
    print(json.dumps(incoming), flush=True)
    if content.startswith("remember "):
        remembered = content.removeprefix("remember ")
        result = "remembered"
    elif content == "recall":
        result = remembered or "missing"
    else:
        result = content
    print(json.dumps({
        "type": "assistant",
        "session_id": session_id,
        "message": {"content": [{"type": "text", "text": result}]},
    }), flush=True)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": session_id, "result": result,
    }), flush=True)
'''


@pytest.fixture
def mock_claude(tmp_path: Path) -> Path:
    executable = tmp_path / "mock-claude"
    executable.write_text(MOCK_CLAUDE)
    executable.chmod(0o755)
    return executable


def _mock_command(executable: Path) -> list[str]:
    # The mock ignores CLI flags, but using the real command builder verifies
    # that Popen receives the production argument shape.
    return claude_command(
        claude_bin=str(executable),
        session_id="11111111-1111-4111-8111-111111111111",
        model="haiku",
        effort="low",
        permission_mode="plan",
        allowed_tools=[],
        resume=False,
        extra_args=[],
    )


def test_persistent_process_keeps_two_prompt_runtime_state(mock_claude: Path, tmp_path: Path):
    session_id = "11111111-1111-4111-8111-111111111111"
    results = run_prompts(
        arm="persistent",
        prompts=["remember SQUID_STREAM_7429", "recall"],
        cwd=tmp_path,
        session_id=session_id,
        command_factory=lambda _resume: _mock_command(mock_claude),
        env={},
        timeout=5,
    )

    assert [result.result for result in results] == ["remembered", "SQUID_STREAM_7429"]
    assert all(any(event.get("type") == "system" for event in result.events) for result in results)


def test_resumed_command_changes_only_session_selector():
    common = dict(
        claude_bin="/usr/local/bin/claude",
        session_id="11111111-1111-4111-8111-111111111111",
        model="sonnet",
        effort="high",
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit"],
        extra_args=["--no-chrome"],
    )
    first = claude_command(**common, resume=False)
    resumed = claude_command(**common, resume=True)

    assert "--session-id" in first
    assert "--resume" not in first
    assert "--resume" in resumed
    assert "--session-id" not in resumed
    assert first[: first.index("--session-id")] == resumed[: resumed.index("--resume")]
    assert first[first.index("--session-id") + 2 :] == resumed[resumed.index("--resume") + 2 :]


def test_gauge_stabilization_waits_for_consecutive_equal_samples(monkeypatch):
    samples = iter([
        GaugeSnapshot(12.0, "reset-1", "t1"),
        GaugeSnapshot(12.5, "reset-1", "t2"),
        GaugeSnapshot(12.5, "reset-1", "t3"),
        GaugeSnapshot(12.5, "reset-1", "t4"),
    ])
    gauge = ClaudeGauge()
    monkeypatch.setattr(gauge, "fetch", lambda: next(samples))

    stable = gauge.stable(
        interval_seconds=0,
        stable_samples=3,
        epsilon=0.001,
        timeout_seconds=5,
        sleep=lambda _: None,
    )

    assert stable.utilization == 12.5
    assert stable.observed_at == "t4"


def test_gauge_stabilization_rejects_window_reset(monkeypatch):
    samples = iter([
        GaugeSnapshot(99.0, "reset-1", "t1"),
        GaugeSnapshot(0.0, "reset-2", "t2"),
    ])
    gauge = ClaudeGauge()
    monkeypatch.setattr(gauge, "fetch", lambda: next(samples))

    with pytest.raises(BenchmarkError, match="reset during stabilization"):
        gauge.stable(
            interval_seconds=0,
            stable_samples=2,
            epsilon=0.001,
            timeout_seconds=5,
            sleep=lambda _: None,
        )
