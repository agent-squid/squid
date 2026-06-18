import glob
import os
import shutil
from pathlib import Path
from typing import Optional
import yaml

_ROOT = Path(__file__).parent.parent

def _load_config() -> dict:
    return yaml.safe_load((_ROOT / "config" / "squid.yaml").read_text())

_cfg = _load_config()

# CLI executable names — override via env if installed elsewhere
CLAUDE_CLI   = "claude"
CODEX_CLI    = "codex"
CURSOR_CLI   = "cursor-agent"

# Not yet supported — runners exist but are gated by ENABLED_BACKENDS
COPILOT_CLI  = "copilot"
AGY_CLI      = "agy"

# Only these backends are exposed via the API and UI
ENABLED_BACKENDS = frozenset({"claude", "codex", "cursor"})

# How long to wait for a CLI to produce its first byte before giving up
FIRST_BYTE_TIMEOUT: int = _cfg["agent"]["first_byte_timeout"]

# Hard cap on total response time per request
RESPONSE_TIMEOUT: int = _cfg["agent"]["response_timeout"]

# Common install locations not always in subprocess PATH
_EXTRA_PATHS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/node_modules/.bin"),
    # nvm-managed node versions (any installed version)
    *glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")),
]

def find_cli(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for base in _EXTRA_PATHS:
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Last resort: check if the binary exists at known path even if not executable by shutil
    known = f"/usr/local/bin/{name}"
    if os.path.exists(known):
        return known
    return None

CLAUDE_PATH   = find_cli(CLAUDE_CLI)
CODEX_PATH    = find_cli(CODEX_CLI)
COPILOT_PATH  = find_cli(COPILOT_CLI)
CURSOR_PATH   = find_cli(CURSOR_CLI)
AGY_PATH      = find_cli(AGY_CLI)

# Per-user tmp dir for context sync — avoids cross-user permission conflicts
SQUID_HOME = f"/tmp/{os.getlogin()}/squid"

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
