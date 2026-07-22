import os
import subprocess

from agent import tool_scripts


def test_ensure_squid_tool_scripts_creates_executable_publish_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_scripts, "SQUID_TOOL_BIN", tmp_path / "bin")

    bin_dir = tool_scripts.ensure_squid_tool_scripts()
    script = bin_dir / "squid-publish"

    assert os.access(script, os.X_OK)
    result = subprocess.run(
        [str(script), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "--topic" in result.stdout
    assert "--tag" in result.stdout
