from pathlib import Path
import runpy
import subprocess
import sys


repair = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/repair-venv.py"))["repair"]


def test_moved_console_script_runs_and_repair_is_idempotent(tmp_path):
    venv = tmp_path / ".venv"
    scripts = venv / "bin"
    scripts.mkdir(parents=True)
    (scripts / "python").symlink_to(sys.executable)
    launcher = scripts / "agentsquid"
    launcher.write_text(f"#!{tmp_path}/deleted/.venv/bin/python3\nprint('ok')\n")
    launcher.chmod(0o755)
    unrelated = scripts / "other"
    unrelated.write_bytes(b"#!/usr/bin/env python3\nprint('other')\n")
    repair(venv)
    assert subprocess.check_output([str(launcher)], text=True) == "ok\n"
    before = launcher.read_bytes()
    repair(venv)
    assert launcher.read_bytes() == before
    assert unrelated.read_bytes() == b"#!/usr/bin/env python3\nprint('other')\n"


def test_missing_editable_source_repaired_but_existing_source_preserved(tmp_path):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/cli.py").touch()
    registration = venv / "lib/python3.12/site-packages/_editable_impl_agentsquid.pth"
    registration.parent.mkdir(parents=True)
    registration.write_text(str(tmp_path / "deleted") + "\n")
    repair(venv)
    assert registration.read_text() == str(tmp_path) + "\n"
    existing = tmp_path / "other-source"
    existing.mkdir()
    registration.write_text(str(existing) + "\n")
    repair(venv)
    assert registration.read_text() == str(existing) + "\n"
