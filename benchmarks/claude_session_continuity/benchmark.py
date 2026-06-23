"""Benchmark process continuity in native Claude Code sessions.

Both arms use the installed ``claude`` binary with stream-json input/output.
The persistent arm sends every prompt to one process.  The resumed arm starts a
new process per prompt and resumes the same native Claude session ID.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from agent import creds


class BenchmarkError(RuntimeError):
    """The benchmark could not produce a valid measurement."""


@dataclass
class PromptResult:
    prompt_index: int
    prompt: str
    result: str
    duration_seconds: float
    events: list[dict[str, Any]]


@dataclass
class GaugeSnapshot:
    utilization: float
    resets_at: Optional[str]
    observed_at: str


@dataclass
class ArmResult:
    arm: str
    session_id: str
    cwd: str
    quota_before: GaugeSnapshot
    quota_after: GaugeSnapshot
    quota_delta: float
    started_at: str
    completed_at: str
    prompts: list[PromptResult]
    git_status: str
    git_diff: str


def state_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(".state.json")


def log_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(".log")


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2))
    os.replace(temporary, path)


class RunState:
    """Atomically persist small, pollable progress snapshots for a benchmark run."""

    def __init__(self, path: Path, **initial: Any):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        self.update(**initial)

    def update(self, **changes: Any) -> None:
        self.data.update(changes, updated_at=utc_now())
        write_json_atomic(self.path, self.data)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_reset_time(value: Optional[str]) -> Optional[str]:
    """Discard API jitter below the reset timestamp's meaningful precision."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).replace(microsecond=0).isoformat()
    except ValueError:
        return value


def quota_window_changed(before: GaugeSnapshot, after: GaugeSnapshot) -> bool:
    """Return whether two samples belong to different established windows.

    At zero utilization Claude reports no reset timestamp. The first request
    activates a window and adds one, which is not a reset and must remain a
    valid before/after comparison.
    """
    if before.resets_at == after.resets_at:
        return False
    if before.resets_at is not None and after.resets_at is not None:
        try:
            before_reset = datetime.fromisoformat(before.resets_at)
            after_reset = datetime.fromisoformat(after.resets_at)
        except ValueError:
            pass
        else:
            # The API's estimate commonly oscillates by one second even while
            # utilization is stable. A real five-hour window change is much larger.
            if abs((after_reset - before_reset).total_seconds()) <= 5:
                return False
    return not (
        before.resets_at is None
        and before.utilization == 0.0
        and after.resets_at is not None
    )


def user_message(prompt: str, session_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        },
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


def claude_command(
    *,
    claude_bin: str,
    session_id: str,
    model: str,
    effort: Optional[str],
    permission_mode: str,
    allowed_tools: list[str],
    resume: bool,
    extra_args: list[str],
) -> list[str]:
    cmd = [
        claude_bin,
        "--verbose",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--replay-user-messages",
        "--model", model,
        "--permission-mode", permission_mode,
    ]
    if effort:
        cmd.extend(["--effort", effort])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    cmd.extend(["--resume", session_id] if resume else ["--session-id", session_id])
    cmd.extend(extra_args)
    # An empty prompt makes print mode accept a still-open stdin pipe.
    cmd.extend(["-p", ""])
    return cmd


class ClaudeStreamProcess:
    """One native Claude Code process driven through newline-delimited JSON."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], timeout: float):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.lines: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()

        def read_lines(stream: Any, name: str) -> None:
            for line in stream:
                self.lines.put((name, line.rstrip("\r\n")))
            self.lines.put((name, None))

        self.readers = [
            threading.Thread(target=read_lines, args=(self.proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=read_lines, args=(self.proc.stderr, "stderr"), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def ask(self, prompt: str, session_id: str, prompt_index: int) -> PromptResult:
        started = time.monotonic()
        payload = user_message(prompt, session_id)
        assert self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise BenchmarkError("Claude process exited before accepting the prompt") from exc

        events: list[dict[str, Any]] = []
        stderr: list[str] = []
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                source, line = self.lines.get(timeout=min(1.0, max(0, deadline - time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            if line is None:
                if self.proc.poll() is not None and self.lines.empty():
                    break
                continue
            if source == "stderr":
                stderr.append(line)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(f"Claude emitted non-JSON stdout: {line[:200]}") from exc
            events.append(event)
            if event.get("type") != "result":
                continue
            result = str(event.get("result") or "")
            subtype = str(event.get("subtype") or "")
            if subtype != "success" or event.get("is_error"):
                raise BenchmarkError(f"Claude result failed ({subtype}): {result}")
            return PromptResult(
                prompt_index=prompt_index,
                prompt=prompt,
                result=result,
                duration_seconds=round(time.monotonic() - started, 3),
                events=events,
            )
        detail = "\n".join(stderr[-10:])
        raise BenchmarkError(
            f"Claude exited or timed out without a result (exit={self.proc.poll()})"
            + (f"\nstderr:\n{detail}" if detail else "")
        )

    def close(self) -> int:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                return self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                return self.proc.wait(timeout=5)

    def __enter__(self) -> "ClaudeStreamProcess":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ClaudeGauge:
    """Read the same claude.ai five-hour gauge used by Squid."""

    def fetch(self) -> GaugeSnapshot:
        org_id = creds.get_org_id()
        session_key = creds.get_session_key()
        if not org_id or not session_key:
            raise BenchmarkError(
                "Claude gauge credentials are missing; configure them in Squid first"
            )
        try:
            from curl_cffi.requests import Session

            with Session(impersonate="chrome") as session:
                response = session.get(
                    f"https://claude.ai/api/organizations/{org_id}/usage",
                    headers={"Cookie": f"sessionKey={session_key}"},
                    timeout=15,
                )
        except Exception as exc:
            raise BenchmarkError(f"Claude gauge request failed: {exc}") from exc
        if response.status_code != 200:
            raise BenchmarkError(f"Claude gauge returned HTTP {response.status_code}")
        window = (response.json().get("five_hour") or {})
        utilization = window.get("utilization")
        if utilization is None:
            raise BenchmarkError("Claude gauge response has no five_hour.utilization")
        return GaugeSnapshot(
            float(utilization), normalize_reset_time(window.get("resets_at")), utc_now()
        )

    def stable(
        self,
        *,
        interval_seconds: float,
        stable_samples: int,
        epsilon: float,
        timeout_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        on_sample: Optional[Callable[[GaugeSnapshot, int], None]] = None,
    ) -> GaugeSnapshot:
        deadline = time.monotonic() + timeout_seconds
        streak = 0
        previous: Optional[GaugeSnapshot] = None
        while True:
            current = self.fetch()
            if previous and quota_window_changed(previous, current):
                raise BenchmarkError("Claude five-hour gauge reset during stabilization")
            if previous and abs(current.utilization - previous.utilization) <= epsilon:
                streak += 1
            else:
                streak = 1
            if on_sample:
                on_sample(current, streak)
            if streak >= stable_samples:
                return current
            if time.monotonic() >= deadline:
                raise BenchmarkError("Claude quota gauge did not stabilize before timeout")
            previous = current
            sleep(interval_seconds)


class GitWorktrees:
    def __init__(self, source: Path, baseline: str, keep: bool):
        self.source = source.resolve()
        self.baseline = baseline
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="claude-session-continuity-"))
        self.paths: list[Path] = []

    def create(self, arm: str) -> Path:
        path = self.root / arm
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), self.baseline],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        self.paths.append(path)
        return path

    def cleanup(self) -> None:
        if self.keep:
            return
        for path in reversed(self.paths):
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self.source,
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self.root, ignore_errors=True)


def git_capture(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    return completed.stdout + completed.stderr


def run_prompts(
    *,
    arm: str,
    prompts: list[str],
    cwd: Path,
    session_id: str,
    command_factory: Callable[[bool], list[str]],
    env: dict[str, str],
    timeout: float,
    on_prompt_start: Optional[Callable[[int], None]] = None,
    on_prompt_complete: Optional[Callable[[PromptResult], None]] = None,
) -> list[PromptResult]:
    def ask(process: ClaudeStreamProcess, prompt: str, index: int) -> PromptResult:
        if on_prompt_start:
            on_prompt_start(index)
        result = process.ask(prompt, session_id, index)
        if on_prompt_complete:
            on_prompt_complete(result)
        return result

    if arm == "persistent":
        with ClaudeStreamProcess(command_factory(False), cwd, env, timeout) as process:
            return [ask(process, prompt, index) for index, prompt in enumerate(prompts, 1)]

    results: list[PromptResult] = []
    for index, prompt in enumerate(prompts, 1):
        with ClaudeStreamProcess(command_factory(index > 1), cwd, env, timeout) as process:
            results.append(ask(process, prompt, index))
    return results


def load_config(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    prompts = data.get("prompts")
    if not isinstance(prompts, list) or not prompts or not all(isinstance(p, str) and p.strip() for p in prompts):
        raise BenchmarkError("config.prompts must be a non-empty list of strings")
    workspace = data.get("workspace") or {}
    if not workspace.get("source"):
        raise BenchmarkError("config.workspace.source is required")
    return data


def sleep_countdown(
    seconds: float,
    label: str,
    on_tick: Optional[Callable[[int], None]] = None,
) -> None:
    if seconds <= 0:
        return
    print(f"{label}: waiting {seconds:g}s with no Claude activity", flush=True)
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if on_tick:
                on_tick(0)
            return
        if on_tick:
            on_tick(max(0, round(remaining)))
        time.sleep(min(remaining, 30))
        print(f"  {max(0, round(deadline - time.monotonic()))}s remaining", flush=True)


def run_benchmark(
    config: dict[str, Any],
    output_path: Path,
    skip_cooldown: bool = False,
    state: Optional[RunState] = None,
) -> dict[str, Any]:
    workspace = config["workspace"]
    source = Path(workspace["source"]).expanduser()
    baseline = str(workspace.get("baseline", "HEAD"))
    keep_worktrees = bool(workspace.get("keep_worktrees", False))
    execution = config.get("execution") or {}
    measurement = config.get("measurement") or {}
    prompts = config["prompts"]
    order = execution.get("order", ["persistent", "resumed"])
    if sorted(order) != ["persistent", "resumed"]:
        raise BenchmarkError("execution.order must contain persistent and resumed exactly once")

    claude_bin = shutil.which(str(execution.get("claude_bin", "claude")))
    if not claude_bin:
        raise BenchmarkError("claude binary not found")
    model = str(execution.get("model", "sonnet"))
    effort = execution.get("effort")
    permission_mode = str(execution.get("permission_mode", "acceptEdits"))
    allowed_tools = list(execution.get("allowed_tools") or [])
    extra_args = [str(arg) for arg in execution.get("extra_args") or []]
    prompt_timeout = float(execution.get("prompt_timeout_seconds", 1800))
    cooldown = 0 if skip_cooldown else float(measurement.get("cold_cache_seconds", 600))
    gauge_options = {
        "interval_seconds": float(measurement.get("gauge_poll_seconds", 10)),
        "stable_samples": int(measurement.get("gauge_stable_samples", 3)),
        "epsilon": float(measurement.get("gauge_epsilon", 0.001)),
        "timeout_seconds": float(measurement.get("gauge_timeout_seconds", 180)),
    }
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (execution.get("env") or {}).items()})

    gauge = ClaudeGauge()
    worktrees = GitWorktrees(source, baseline, keep_worktrees)
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": utc_now(),
        "claude_bin": claude_bin,
        "claude_version": subprocess.run(
            [claude_bin, "--version"], text=True, capture_output=True, check=True
        ).stdout.strip(),
        "model": model,
        "baseline": baseline,
        "order": order,
        "arms": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if state:
        state.update(
            status="running",
            phase="initializing",
            pid=os.getpid(),
            started_at=report["started_at"],
            output_path=str(output_path.resolve()),
            total_arms=len(order),
            total_prompts=len(prompts),
            completed_arms=0,
        )
    try:
        for position, arm in enumerate(order):
            progress = {
                "arm": arm,
                "arm_index": position + 1,
                "prompt_index": None,
                "completed_prompts": 0,
            }
            if state:
                state.update(phase="cooldown", cooldown_remaining_seconds=round(cooldown), **progress)
            sleep_countdown(
                cooldown,
                f"Cold-cache boundary before {arm}",
                on_tick=(
                    lambda remaining: state.update(cooldown_remaining_seconds=remaining)
                    if state else None
                ),
            )
            cwd = worktrees.create(arm)
            session_id = str(uuid.uuid4())
            print(f"Starting {arm} arm in {cwd} (session {session_id})", flush=True)
            if state:
                state.update(phase="gauge_before", cooldown_remaining_seconds=0)
            before = gauge.stable(
                **gauge_options,
                on_sample=(
                    lambda sample, streak: state.update(
                        gauge_utilization=sample.utilization,
                        gauge_resets_at=sample.resets_at,
                        gauge_stable_streak=streak,
                    ) if state else None
                ),
            )
            started_at = utc_now()
            active_arm: dict[str, Any] = {
                "arm": arm,
                "session_id": session_id,
                "cwd": str(cwd),
                "quota_before": asdict(before),
                "started_at": started_at,
                "status": "running",
                "prompts": [],
            }
            report["active_arm"] = active_arm
            write_json_atomic(output_path, report)

            def command_factory(resume: bool) -> list[str]:
                return claude_command(
                    claude_bin=claude_bin,
                    session_id=session_id,
                    model=model,
                    effort=effort,
                    permission_mode=permission_mode,
                    allowed_tools=allowed_tools,
                    resume=resume,
                    extra_args=extra_args,
                )

            def prompt_started(index: int) -> None:
                active_arm["prompt_index"] = index
                write_json_atomic(output_path, report)
                if state:
                    state.update(phase="prompt", prompt_index=index)

            def prompt_completed(result: PromptResult) -> None:
                active_arm["prompts"].append(asdict(result))
                active_arm["completed_prompts"] = result.prompt_index
                active_arm["prompt_index"] = None
                active_arm["git_status"] = git_capture(cwd, "status", "--short")
                active_arm["git_diff"] = git_capture(cwd, "diff", "--binary", "HEAD")
                write_json_atomic(output_path, report)
                if state:
                    state.update(
                        completed_prompts=result.prompt_index,
                        last_prompt_duration_seconds=result.duration_seconds,
                    )

            prompt_results = run_prompts(
                arm=arm,
                prompts=prompts,
                cwd=cwd,
                session_id=session_id,
                command_factory=command_factory,
                env=env,
                timeout=prompt_timeout,
                on_prompt_start=prompt_started,
                on_prompt_complete=prompt_completed,
            )
            if state:
                state.update(phase="gauge_after", prompt_index=None)
            after = gauge.stable(
                **gauge_options,
                on_sample=(
                    lambda sample, streak: state.update(
                        gauge_utilization=sample.utilization,
                        gauge_resets_at=sample.resets_at,
                        gauge_stable_streak=streak,
                    ) if state else None
                ),
            )
            if quota_window_changed(before, after):
                raise BenchmarkError(f"Claude five-hour gauge reset during {arm} arm")
            arm_result = ArmResult(
                arm=arm,
                session_id=session_id,
                cwd=str(cwd),
                quota_before=before,
                quota_after=after,
                quota_delta=round(after.utilization - before.utilization, 6),
                started_at=started_at,
                completed_at=utc_now(),
                prompts=prompt_results,
                git_status=git_capture(cwd, "status", "--short"),
                git_diff=git_capture(cwd, "diff", "--binary", "HEAD"),
            )
            report["arms"].append(asdict(arm_result))
            report.pop("active_arm", None)
            write_json_atomic(output_path, report)
            if state:
                state.update(completed_arms=position + 1, quota_delta=arm_result.quota_delta)
            print(f"Completed {arm}: quota delta {arm_result.quota_delta:g}%", flush=True)
        report["completed_at"] = utc_now()
        write_json_atomic(output_path, report)
        if state:
            state.update(status="completed", phase="completed", completed_at=report["completed_at"])
        return report
    except BaseException as exc:
        report["failed_at"] = utc_now()
        report["error"] = str(exc)
        if "active_arm" in report:
            report["active_arm"]["status"] = "failed"
        write_json_atomic(output_path, report)
        if state:
            state.update(status="failed", phase="failed", error=str(exc), failed_at=utc_now())
        raise
    finally:
        worktrees.cleanup()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--background",
        action="store_true",
        help="Detach the run and return paths for polling its state and logs",
    )
    parser.add_argument(
        "--status",
        type=Path,
        metavar="OUTPUT_OR_STATE",
        help="Print the current state for an output or .state.json path",
    )
    parser.add_argument(
        "--skip-cooldown",
        action="store_true",
        help="Skip cold-cache waits (only for protocol/debug runs, not valid measurements)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.status:
        status_path = args.status if args.status.name.endswith(".state.json") else state_path_for(args.status)
        if not status_path.exists():
            print(f"benchmark state not found: {status_path}", file=sys.stderr)
            return 1
        data = json.loads(status_path.read_text())
        pid = data.get("pid")
        if data.get("status") == "running" and isinstance(pid, int):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                data["status"] = "stopped"
        print(json.dumps(data, indent=2))
        return 0
    if not args.config:
        parser.error("--config is required unless --status is used")
    output = args.output or Path(__file__).with_name("results") / f"run-{int(time.time())}.json"
    output = output.expanduser().resolve()
    if args.background:
        output.parent.mkdir(parents=True, exist_ok=True)
        state_path = state_path_for(output)
        log_path = log_path_for(output)
        RunState(
            state_path,
            schema_version=1,
            status="starting",
            phase="starting",
            config_path=str(args.config.expanduser().resolve()),
            output_path=str(output),
            log_path=str(log_path),
            requested_at=utc_now(),
        )
        command = [
            sys.executable,
            "-m", "benchmarks.claude_session_continuity.benchmark",
            "--config", str(args.config.expanduser().resolve()),
            "--output", str(output),
        ]
        if args.skip_cooldown:
            command.append("--skip-cooldown")
        with log_path.open("a") as log:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        RunState(state_path).update(pid=process.pid)
        print(json.dumps({
            "pid": process.pid,
            "output": str(output),
            "state": str(state_path),
            "log": str(log_path),
        }, indent=2))
        return 0
    state = RunState(
        state_path_for(output),
        schema_version=1,
        status="starting",
        phase="starting",
        config_path=str(args.config.expanduser().resolve()),
        log_path=str(log_path_for(output)),
    )
    try:
        config = load_config(args.config)
        run_benchmark(config, output, skip_cooldown=args.skip_cooldown, state=state)
    except (BenchmarkError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        state.update(status="failed", phase="failed", error=str(exc), failed_at=utc_now())
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(f"Results written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
