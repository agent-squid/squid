import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import yaml

_USER_CONFIG = Path.home() / ".squid" / "squid.yaml"

def _load_config() -> dict:
    return yaml.safe_load(_USER_CONFIG.read_text())


def config_text() -> str:
    return _USER_CONFIG.read_text(encoding="utf-8")


def config_revision(content: Optional[str] = None) -> str:
    raw = config_text() if content is None else content
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def write_config_text(content: str, expected_revision: Optional[str] = None) -> str:
    """Atomically replace squid.yaml, retaining the previous version as .bak."""
    if expected_revision is not None and config_revision() != expected_revision:
        raise RuntimeError("configuration changed since it was loaded")

    mode = _USER_CONFIG.stat().st_mode & 0o777
    backup = _USER_CONFIG.with_suffix(_USER_CONFIG.suffix + ".bak")
    shutil.copy2(_USER_CONFIG, backup)
    fd, tmp_name = tempfile.mkstemp(prefix=".squid.yaml.", dir=_USER_CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, _USER_CONFIG)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return config_revision(content)

_cfg = _load_config()

# CLI executable names — override via env if installed elsewhere
CLAUDE_CLI    = "claude"
CODEX_CLI     = "codex"
CURSOR_CLI    = "cursor-agent"
OPENCODE_CLI  = "opencode"
PI_CLI        = "pi"

# Experimental runners exist for these CLIs but they are not configurable harnesses.
COPILOT_CLI  = "copilot"
AGY_CLI      = "agy"

# Squid Echo: a no-op harness (agent/runners.py's run_echo) that echoes the
# prompt straight back with no subprocess and no network call — for fast,
# free previews of how a Squid Flow route actually dispatches/executes
# (joins, round-trips, broadcasts, ...) without a real coding-agent CLI in
# the loop. Opt-in only (absent/false by default), so it never shows up for
# a user who hasn't asked for it; harnesses.py and providers.py both gate on
# this flag. Settable either way — `test_harness_enabled: true` in
# squid.yaml (picked up on every restart, since /restart re-execs and
# re-reads the file) or the SQUID_TEST_HARNESS env var (for CI/one-offs).
TEST_HARNESS_ENABLED: bool = bool(_cfg.get("test_harness_enabled")) or bool(os.environ.get("SQUID_TEST_HARNESS"))

# How long to wait for a CLI to produce its first byte before giving up
FIRST_BYTE_TIMEOUT: int = _cfg["agent"]["first_byte_timeout"]

# Hard cap on total response time per request
RESPONSE_TIMEOUT: int = _cfg["agent"]["response_timeout"]

def find_cli(name: str) -> Optional[str]:
    return shutil.which(name)

CLAUDE_PATH    = find_cli(CLAUDE_CLI)
CODEX_PATH     = find_cli(CODEX_CLI)
COPILOT_PATH   = find_cli(COPILOT_CLI)
CURSOR_PATH    = find_cli(CURSOR_CLI)
OPENCODE_PATH  = find_cli(OPENCODE_CLI)
PI_PATH        = find_cli(PI_CLI)
AGY_PATH       = find_cli(AGY_CLI)

# OpenCode free provider — no API key required
OPENCODE_DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

# Per-user tmp dir for context sync — avoids cross-user permission conflicts
SQUID_HOME = f"/tmp/{os.getlogin()}/squid"

# Per-turn Git worktree isolation (see ADR-0025). On by default; set
# `worktree.enabled: false` in squid.yaml to use direct working-tree writes.
_worktree_cfg = _cfg.get("worktree", {})
WORKTREE_ISOLATION_ENABLED: bool = bool(_worktree_cfg.get("enabled", True))

# Dependency/cache directory names to symlink from a code root into each fresh
# per-turn worktree. auto_link_ignored_dirs only links ignored dirs whose
# basename appears in this allowlist.
WORKTREE_AUTO_LINK_IGNORED_DIRS: bool = bool(_worktree_cfg.get("auto_link_ignored_dirs", True))
DEPENDENCY_DIRS: list[str] = _worktree_cfg.get("dependency_dirs", [
    "node_modules", ".venv", "venv", "env", ".tox", "__pypackages__",
    "vendor", "target", ".bundle", "Pods", ".cargo", ".stack-work", "elm-stuff",
])

# Proxy environment to inject into every CLI subprocess, or None if disabled.
_proxy_cfg = _cfg.get("proxy", {})
PROXY_ENV: Optional[dict] = None
if _proxy_cfg.get("enabled"):
    _proxy_url = _proxy_cfg.get("url", "http://127.0.0.1:8080")
    _cert = os.path.expanduser(_proxy_cfg.get("ssl_cert_file", "~/.mitmproxy/mitmproxy-ca-cert.pem"))
    PROXY_ENV = {
        "HTTP_PROXY": _proxy_url,
        "HTTPS_PROXY": _proxy_url,
        "SSL_CERT_FILE": _cert,
    }
