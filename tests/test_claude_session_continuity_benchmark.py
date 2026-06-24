import json
from datetime import datetime, timezone
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
    default_output_path,
    evaluate_submissions,
    git_worktree_diff,
    log_path_for,
    main,
    native_claude_env,
    normalize_reset_time,
    quota_window_changed,
    run_benchmark,
    run_prompts,
    summarize_debug_logs,
    state_path_for,
    sum_usage,
    sync_benchmark_outputs,
    usage_difference,
    usage_snapshot,
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
        "model": "claude-sonnet-mock",
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


def test_process_kills_non_sonnet_model(tmp_path: Path):
    executable = tmp_path / "mock-opus"
    executable.write_text(MOCK_CLAUDE.replace("claude-sonnet-mock", "claude-opus-mock"))
    executable.chmod(0o755)
    session_id = "11111111-1111-4111-8111-111111111111"

    with pytest.raises(BenchmarkError, match="non-sonnet model: claude-opus-mock"):
        with ClaudeStreamProcess(
            _mock_command(executable),
            tmp_path,
            {},
            5,
            required_model_family="sonnet",
        ) as process:
            process.ask("inspect", session_id, 1)

    assert process.proc.returncode is not None


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


def test_usage_normalization_subtracts_persistent_cumulative_counters():
    first_event = {
        "total_cost_usd": 0.10,
        "modelUsage": {
            "claude-sonnet": {
                "inputTokens": 5,
                "outputTokens": 100,
                "cacheReadInputTokens": 1000,
                "cacheCreationInputTokens": 800,
                "costUSD": 0.10,
            }
        },
        "usage": {"iterations": [{"type": "message"}]},
    }
    second_event = {
        "total_cost_usd": 0.16,
        "modelUsage": {
            "claude-sonnet": {
                "inputTokens": 8,
                "outputTokens": 160,
                "cacheReadInputTokens": 2100,
                "cacheCreationInputTokens": 950,
                "costUSD": 0.16,
            }
        },
        "usage": {"iterations": [{"type": "message"}, {"type": "tool"}]},
    }

    first = usage_snapshot(first_event)
    second = usage_snapshot(second_event)
    delta = usage_difference(second, first)

    assert delta["input_tokens"] == 3
    assert delta["output_tokens"] == 60
    assert delta["cache_read_input_tokens"] == 1100
    assert delta["cache_creation_input_tokens"] == 150
    assert delta["cost_usd"] == 0.06
    assert delta["inference_iterations"] == 2
    assert delta["models"]["claude-sonnet"]["output_tokens"] == 60


def test_sum_usage_uses_normalized_prompt_deltas():
    prompts = [
        PromptResult(1, "one", "done", 1.0, [], usage_delta={
            "input_tokens": 1, "output_tokens": 10,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20,
            "cost_usd": 0.01, "inference_iterations": 1, "models": {},
        }),
        PromptResult(2, "two", "done", 1.0, [], usage_delta={
            "input_tokens": 2, "output_tokens": 20,
            "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30,
            "cost_usd": 0.02, "inference_iterations": 2, "models": {},
        }),
    ]

    total = sum_usage(prompts)

    assert total["input_tokens"] == 3
    assert total["output_tokens"] == 30
    assert total["cache_read_input_tokens"] == 300
    assert total["cache_creation_input_tokens"] == 50
    assert total["cost_usd"] == 0.03
    assert total["inference_iterations"] == 3


def test_debug_summary_counts_requests_sources_and_models(tmp_path: Path):
    first = tmp_path / "process-01.log"
    second = tmp_path / "process-02.log"
    first.write_text(
        "[API:timing] dispatching to firstParty model=claude-haiku\n"
        "[API REQUEST] /v1/messages id=one source=generate_session_title\n"
        "[API:timing] dispatching to firstParty model=claude-sonnet\n"
        "[API REQUEST] /v1/messages id=two source=sdk\n"
    )
    second.write_text(
        "[API:timing] dispatching to firstParty model=claude-sonnet\n"
        "[API REQUEST] /v1/messages id=three source=sdk\n"
    )

    summary = summarize_debug_logs([str(first), str(second)])

    assert summary["file_count"] == 2
    assert summary["bytes"] == first.stat().st_size + second.stat().st_size
    assert summary["api_requests"] == 3
    assert summary["request_sources"] == {"generate_session_title": 1, "sdk": 2}
    assert summary["dispatched_models"] == {"claude-haiku": 1, "claude-sonnet": 2}


def test_controller_evaluations_check_saved_submissions(tmp_path: Path):
    lru = tmp_path / "benchmark_outputs" / "01_lru_cache" / "solution.py"
    merge = tmp_path / "benchmark_outputs" / "02_merge_intervals" / "solution.py"
    lru.parent.mkdir(parents=True)
    merge.parent.mkdir(parents=True)
    lru.write_text(
        "from collections import OrderedDict\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity): self.capacity, self.data = capacity, OrderedDict()\n"
        "    def get(self, key):\n"
        "        if key not in self.data: return -1\n"
        "        self.data.move_to_end(key); return self.data[key]\n"
        "    def put(self, key, value):\n"
        "        if key in self.data: self.data.move_to_end(key)\n"
        "        self.data[key] = value\n"
        "        if len(self.data) > self.capacity: self.data.popitem(last=False)\n"
    )
    merge.write_text(
        "def merge_intervals(intervals):\n"
        "    result = []\n"
        "    for start, end in sorted(intervals):\n"
        "        if result and start <= result[-1][1]: result[-1][1] = max(result[-1][1], end)\n"
        "        else: result.append([start, end])\n"
        "    return result\n"
    )

    evaluations = evaluate_submissions(tmp_path)

    assert [item["status"] for item in evaluations] == ["passed", "passed"]


def test_controller_evaluations_report_missing_and_failed_submissions(tmp_path: Path):
    lru = tmp_path / "benchmark_outputs" / "01_lru_cache" / "solution.py"
    lru.parent.mkdir(parents=True)
    lru.write_text("class LRUCache:\n    pass\n")

    evaluations = evaluate_submissions(tmp_path)

    assert evaluations[0]["status"] == "failed"
    assert evaluations[1]["status"] == "missing"


def test_native_claude_env_clears_inherited_and_overridden_auth():
    env = native_claude_env(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "inherited-key",
            "ANTHROPIC_BASE_URL": "https://inherited.example",
        },
        {
            "BENCHMARK_MARKER": "kept",
            "ANTHROPIC_AUTH_TOKEN": "configured-token",
            "SQUID_NATIVE_CLAUDE_TOKEN": "cached-token",
        },
    )

    assert env == {"PATH": "/bin", "BENCHMARK_MARKER": "kept"}


def test_git_worktree_diff_includes_untracked_files(tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "benchmark@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Benchmark Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    tracked.write_text("after\n")
    (tmp_path / "submission.py").write_text("def answer():\n    return 42\n")

    diff = git_worktree_diff(tmp_path)

    assert "+after" in diff
    assert "submission.py" in diff
    assert "+def answer():" in diff


def test_run_directory_paths_and_submission_materialization(tmp_path: Path):
    output = tmp_path / "run-20260623T120000Z" / "report.json"
    workspace = tmp_path / "workspace"
    submission = workspace / "benchmark_outputs" / "01_task" / "solution.py"
    submission.parent.mkdir(parents=True)
    submission.write_text("def answer():\n    return 42\n")

    sync_benchmark_outputs(workspace, output.parent / "persistent")

    assert state_path_for(output) == output.parent / "state.json"
    assert log_path_for(output) == output.parent / "run.log"
    stored = output.parent / "persistent" / "benchmark_outputs" / "01_task" / "solution.py"
    assert stored.read_text() == submission.read_text()


def test_default_output_uses_unique_timestamped_run_directory():
    first = default_output_path(datetime(2026, 6, 23, 12, 0, 0, 1, timezone.utc))
    second = default_output_path(datetime(2026, 6, 23, 12, 0, 0, 2, timezone.utc))

    assert first.name == "report.json"
    assert first.parent.name.startswith("run-")
    assert first != second


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


def test_successful_run_reports_normalized_usage_processes_and_evaluations(
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
            return GaugeSnapshot(float(self.calls), "reset-1", f"t{self.calls}")

    monkeypatch.setattr(
        "benchmarks.claude_session_continuity.benchmark.GitWorktrees", FakeWorktrees
    )
    monkeypatch.setattr(
        "benchmarks.claude_session_continuity.benchmark.ClaudeGauge", FakeGauge
    )
    output = tmp_path / "run" / "report.json"
    config = {
        "workspace": {"source": str(tmp_path)},
        "execution": {
            "claude_bin": str(mock_claude),
            "order": ["persistent", "resumed"],
            "capture_debug": True,
        },
        "prompts": ["one"],
    }

    report = run_benchmark(config, output, skip_cooldown=True)

    assert [arm["process_count"] for arm in report["arms"]] == [1, 1]
    assert all(arm["usage_total"]["cost_usd"] == 0 for arm in report["arms"])
    assert all(arm["debug_summary"]["file_count"] == 1 for arm in report["arms"])
    assert all(
        [evaluation["status"] for evaluation in arm["evaluations"]]
        == ["missing", "missing"]
        for arm in report["arms"]
    )
