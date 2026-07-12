import json
import os
import site
import subprocess
import sys
from pathlib import Path


def _write_user_config(home: Path, content: str) -> None:
    squid_dir = home / ".squid"
    squid_dir.mkdir(parents=True)
    (squid_dir / "squid.yaml").write_text(content, encoding="utf-8")


def test_harness_yaml_overrides_protocol_default(tmp_path):
    _write_user_config(tmp_path, """
server:
  host: "127.0.0.1"
  port: 8000
agent:
  first_byte_timeout: 300
  response_timeout: 1800
harnesses:
  claudecode:
    protocol:
      type: oneshot-cli
    default_provider: anthropic
    supported_apis: [/v1/messages]
providers:
  anthropic:
    label: Claude
    color: "#AE5332"
    auth: {type: subscription}
    gauge: claude
    supported_apis: [/v1/messages]
""")
    code = """
import json
from agent.harnesses import list_harnesses
from agent.resolve import resolve_agent
resolved = resolve_agent("claudecode", None)
print(json.dumps({
    "protocol": resolved.protocol,
    "idle_timeout_seconds": resolved.interactive.idle_timeout_seconds,
    "harnesses": list_harnesses(),
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHONPATH": os.pathsep.join(
                path for path in [site.getusersitepackages(), os.environ.get("PYTHONPATH", "")]
                if path
            ),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    claudecode = next(item for item in data["harnesses"] if item["id"] == "claudecode")

    assert data["protocol"] == "oneshot-cli"
    assert data["idle_timeout_seconds"] == 0
    assert claudecode["protocol"] == "oneshot-cli"
    assert claudecode["interactive"]["idle_timeout_seconds"] == 3600.0
    assert claudecode["supported_apis"] == ["/v1/messages"]
    assert claudecode["compatible_providers"] == ["anthropic"]


def test_interactive_cli_protocol_timeout_is_nested_under_timeout(tmp_path):
    _write_user_config(tmp_path, """
server:
  host: "127.0.0.1"
  port: 8000
agent:
  first_byte_timeout: 300
  response_timeout: 1800
harnesses:
  claudecode:
    protocol:
      type: interactive-cli
      timeout:
        idle_seconds: 42
    default_provider: anthropic
    supported_apis: [/v1/messages]
providers:
  anthropic:
    label: Claude
    color: "#AE5332"
    auth: {type: subscription}
    gauge: claude
    supported_apis: [/v1/messages]
""")
    code = """
import json
from agent.harnesses import list_harnesses
from agent.resolve import resolve_agent
resolved = resolve_agent("claudecode", None)
print(json.dumps({
    "protocol": resolved.protocol,
    "idle_timeout_seconds": resolved.interactive.idle_timeout_seconds,
    "harnesses": list_harnesses(),
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHONPATH": os.pathsep.join(
                path for path in [site.getusersitepackages(), os.environ.get("PYTHONPATH", "")]
                if path
            ),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    claudecode = next(item for item in data["harnesses"] if item["id"] == "claudecode")

    assert data["protocol"] == "interactive-cli"
    assert data["idle_timeout_seconds"] == 42.0
    assert claudecode["protocol"] == "interactive-cli"
    assert claudecode["interactive"]["idle_timeout_seconds"] == 42.0
    assert claudecode["supported_apis"] == ["/v1/messages"]
    assert claudecode["compatible_providers"] == ["anthropic"]


def test_harness_compatible_providers_are_derived_from_supported_apis(tmp_path):
    _write_user_config(tmp_path, """
server:
  host: "127.0.0.1"
  port: 8000
agent:
  first_byte_timeout: 300
  response_timeout: 1800
harnesses:
  claudecode:
    protocol:
      type: interactive-cli
    default_provider: anthropic
    supported_apis: [/v1/messages]
providers:
  anthropic:
    label: Claude
    color: "#AE5332"
    auth: {type: subscription}
    gauge: claude
    supported_apis: [/v1/messages]
  gateway:
    label: Gateway
    color: "#123456"
    base_url: "https://gateway.example/v1"
    auth:
      type: api_key
      api_key: token
    gauge: none
    supported_apis: [/v1/messages]
""")
    code = """
import json
from agent.harnesses import list_harnesses
print(json.dumps(list_harnesses(), sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PYTHONPATH": os.pathsep.join(
                path for path in [site.getusersitepackages(), os.environ.get("PYTHONPATH", "")]
                if path
            ),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    claudecode = next(item for item in data if item["id"] == "claudecode")

    assert claudecode["compatible_providers"] == ["anthropic", "gateway"]
