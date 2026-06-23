import json
from pathlib import Path

import pytest

from benchmarks.claude_session_continuity.benchmark import (
    BenchmarkError,
    ClaudeGauge,
    ClaudeStreamProcess,
    GaugeSnapshot,
    PromptResult,
    RunState,
    claude_command,
    main,
    normalize_reset_time,
    quota_window_changed,
    run_benchmark,
    run_prompts,
    state_path_for,
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


def test_prompt_callbacks_expose_live_progress(mock_claude: Path, tmp_path: Path):
    started = []
    completed = []
    results = run_prompts(
        arm="persistent",
        prompts=["one", "two"],
        cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111",
        command_factory=lambda _resume: _mock_command(mock_claude),
        env={},
        timeout=5,
        on_prompt_start=started.append,
        on_prompt_complete=lambda result: completed.append(result.prompt_index),
    )

    assert [result.result for result in results] == ["one", "two"]
    assert started == [1, 2]
    assert completed == [1, 2]


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


def test_gauge_stabilization_accepts_initial_window_activation(monkeypatch):
    samples = iter([
        GaugeSnapshot(0.0, None, "t1"),
        GaugeSnapshot(1.0, "reset-1", "t2"),
        GaugeSnapshot(1.0, "reset-1", "t3"),
    ])
    gauge = ClaudeGauge()
    monkeypatch.setattr(gauge, "fetch", lambda: next(samples))

    stable = gauge.stable(
        interval_seconds=0,
        stable_samples=2,
        epsilon=0.001,
        timeout_seconds=5,
        sleep=lambda _: None,
    )

    assert stable.resets_at == "reset-1"


def test_quota_window_change_distinguishes_activation_from_reset():
    unused = GaugeSnapshot(0.0, None, "t1")
    activated = GaugeSnapshot(29.0, "reset-1", "t2")
    reset = GaugeSnapshot(0.0, "reset-2", "t3")

    assert quota_window_changed(unused, activated) is False
    assert quota_window_changed(activated, reset) is True


def test_quota_window_change_ignores_small_reset_timestamp_jitter():
    first = GaugeSnapshot(6.0, "2026-06-23T20:29:59+00:00", "t1")
    jittered = GaugeSnapshot(6.0, "2026-06-23T20:30:00+00:00", "t2")
    changed = GaugeSnapshot(0.0, "2026-06-24T01:30:00+00:00", "t3")

    assert quota_window_changed(first, jittered) is False
    assert quota_window_changed(first, changed) is True


def test_reset_time_normalization_discards_fractional_api_jitter():
    first = normalize_reset_time("2026-06-23T06:10:00.126303+00:00")
    second = normalize_reset_time("2026-06-23T06:10:00.904061+00:00")

    assert first == second == "2026-06-23T06:10:00+00:00"


def test_run_state_updates_atomically_and_status_accepts_output_path(tmp_path: Path, capsys):
    output = tmp_path / "run.json"
    state_path = state_path_for(output)
    state = RunState(state_path, schema_version=1, status="starting")
    state.update(status="completed", phase="completed")

    assert not list(tmp_path.glob("*.tmp"))
    assert main(["--status", str(output)]) == 0
    displayed = capsys.readouterr().out
    assert '"status": "completed"' in displayed
    assert '"phase": "completed"' in displayed


def test_background_run_detaches_and_publishes_paths(tmp_path: Path, monkeypatch, capsys):
    launched = {}

    class FakeProcess:
        pid = 43210

        def __init__(self, command, **kwargs):
            launched["command"] = command
            launched["kwargs"] = kwargs

    monkeypatch.setattr("benchmarks.claude_session_continuity.benchmark.subprocess.Popen", FakeProcess)
    config = tmp_path / "config.yaml"
    output = tmp_path / "run.json"

    assert main(["--config", str(config), "--output", str(output), "--background"]) == 0
    displayed = capsys.readouterr().out
    state = RunState(state_path_for(output)).data

    assert '"pid": 43210' in displayed
    assert launched["kwargs"]["start_new_session"] is True
    assert launched["kwargs"]["stdin"] is not None
    assert state["status"] == "starting"
    assert state["pid"] == 43210


def test_run_checkpoint_preserves_prompts_when_post_gauge_fails(
    mock_claude: Path, tmp_path: Path, monkeypatch
):
    class FakeWorktrees:
        def __init__(self, *_args, **_kwargs):
            pass

        def create(self, _arm):
            return tmp_path

        def cleanup(self):
            pass

    class FakeGauge:
        calls = 0

        def stable(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return GaugeSnapshot(0.0, None, "before")
            raise BenchmarkError("post gauge failed")

    result = PromptResult(1, "inspect", "done", 1.25, [{"type": "result"}])

    def fake_run_prompts(**kwargs):
        kwargs["on_prompt_start"](1)
        kwargs["on_prompt_complete"](result)
        return [result]

    monkeypatch.setattr(
        "benchmarks.claude_session_continuity.benchmark.GitWorktrees", FakeWorktrees
    )
    monkeypatch.setattr(
        "benchmarks.claude_session_continuity.benchmark.ClaudeGauge", FakeGauge
    )
    monkeypatch.setattr(
        "benchmarks.claude_session_continuity.benchmark.run_prompts", fake_run_prompts
    )
    output = tmp_path / "report.json"
    config = {
        "workspace": {"source": str(tmp_path)},
        "execution": {
            "claude_bin": str(mock_claude),
            "order": ["persistent", "resumed"],
        },
        "prompts": ["inspect"],
    }

    with pytest.raises(BenchmarkError, match="post gauge failed"):
        run_benchmark(config, output, skip_cooldown=True)

    report = json.loads(output.read_text())
    assert report["error"] == "post gauge failed"
    assert report["active_arm"]["status"] == "failed"
    assert report["active_arm"]["prompts"][0]["result"] == "done"
    assert "git_status" in report["active_arm"]
    assert "git_diff" in report["active_arm"]
