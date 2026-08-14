import getpass
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import yaml

_USER_CONFIG = Path.home() / ".squid" / "squid.yaml"
_EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "squid.yaml.example"


def _bootstrap_config() -> None:
    """Create ~/.squid/squid.yaml from the bundled example on first run.

    bin/install.sh and bin/start.sh do this same substitution for a source
    checkout, but a pip/pipx install never runs either script, so this has
    to also happen here — otherwise a fresh pipx install has no config file
    and _load_config() below crashes on the very first launch.
    """
    if _USER_CONFIG.exists() or not _EXAMPLE_CONFIG.exists():
        return
    _USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = _EXAMPLE_CONFIG.read_text(encoding="utf-8")
    text = text.replace("/tmp/squid", f"/tmp/{getpass.getuser()}/squid")
    _USER_CONFIG.write_text(text, encoding="utf-8")


def _load_config() -> dict:
    _bootstrap_config()
    return yaml.safe_load(_USER_CONFIG.read_text())


def realtime_transport(config: dict) -> str:
    realtime = config.get("realtime", {})
    if not isinstance(realtime, dict):
        raise ValueError("realtime must be a mapping")
    transport = realtime.get("transport", "auto")
    if transport not in {"auto", "websocket", "sse"}:
        raise ValueError("realtime.transport must be one of: auto, websocket, sse")
    return transport


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
REALTIME_TRANSPORT: str = realtime_transport(_cfg)

# CLI executable names — override via env if installed elsewhere
CLAUDE_CLI    = "claude"
CODEX_CLI     = "codex"
CURSOR_CLI    = "cursor-agent"
OPENCODE_CLI  = "opencode"
PI_CLI        = "pi"

# Ollama's own control binary (ADR-0037) — not a harness CLI, just what's
# checked to decide whether the `ollama` provider is actually usable / worth
# defaulting pi and opencode to (see bin/install.sh).
OLLAMA_CLI    = "ollama"

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

# How long a native `!` shell command may run before Squid stops it
# (ADR-0038). Deliberately separate from response_timeout: a shell command
# is a different kind of operation than an LLM turn and shouldn't inherit
# that value. Read with .get(), not required like the fields above, so an
# existing squid.yaml without this key still loads.
NATIVE_SHELL_TIMEOUT: int = int(_cfg["agent"].get("shell_timeout", 120))

_updates_cfg = _cfg.get("updates", {})
UPDATES_INSTALL_ON_RESTART: str = str(_updates_cfg.get("install_on_restart", "ask")).lower()
if UPDATES_INSTALL_ON_RESTART not in {"ask", "always", "never"}:
    UPDATES_INSTALL_ON_RESTART = "ask"

def find_cli(name: str) -> Optional[str]:
    return shutil.which(name)

CLAUDE_PATH    = find_cli(CLAUDE_CLI)
CODEX_PATH     = find_cli(CODEX_CLI)
COPILOT_PATH   = find_cli(COPILOT_CLI)
CURSOR_PATH    = find_cli(CURSOR_CLI)
OPENCODE_PATH  = find_cli(OPENCODE_CLI)
PI_PATH        = find_cli(PI_CLI)
AGY_PATH       = find_cli(AGY_CLI)
OLLAMA_PATH    = find_cli(OLLAMA_CLI)

# OpenCode free provider — no API key required
OPENCODE_DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

# Per-user tmp dir for context sync — avoids cross-user permission conflicts
SQUID_HOME = f"/tmp/{os.getlogin()}/squid"

# Per-user, per-agent tmp dir for sandboxed-$HOME agents (see ADR-0036).
# Deliberately a sibling of SQUID_HOME, not nested inside it -- context_sync's
# rsync runs with --delete against SQUID_HOME whenever ~/.squid/context/
# changes, and would silently wipe a credential symlink or installed
# plugins/skills sitting inside it. Under /tmp rather than ~/.squid/ for the
# same reason ADR-0012 moved context off a path under the real $HOME:
# discovery that walks up a directory tree looking for CLAUDE.md/config
# would otherwise eventually reach the real $HOME and pick up its personal
# CLAUDE.md/MCP config -- exactly the contamination sandboxing is meant to
# prevent. Each agent gets its own subfolder (SQUID_HOMES/<agent>) so
# Blank Home agents don't share plugins/skills/settings/history with
# each other, only the sibling-of-SQUID_HOME root is shared as a concept.
SQUID_HOMES = f"/tmp/{os.getlogin()}/squid-homes"

# Per-user tmp dir for native `!` shell command spool files (ADR-0038) — full
# stdout/stderr past the in-chat display cap. Sibling of SQUID_HOME for the
# same reason SQUID_HOMES is: SQUID_HOME is emptied by context_sync's
# `rsync --delete` whenever ~/.squid/context/ changes, and would silently
# delete spool files sitting inside it.
NATIVE_SHELL_SPOOL_DIR = f"/tmp/{os.getlogin()}/squid-shell-logs"

# Per-turn Git worktree isolation (see ADR-0025). On by default; set
# `worktree.enabled: false` in squid.yaml to use direct working-tree writes.
_worktree_cfg = _cfg.get("worktree", {})
WORKTREE_ISOLATION_ENABLED: bool = bool(_worktree_cfg.get("enabled", True))
WORKTREE_TRACK_DIRTY_CHANGES: bool = bool(_worktree_cfg.get("track_dirty_changes", True))

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
