import os
import shutil
from typing import Optional

# CLI executable names — override via env if installed elsewhere
CLAUDE_CLI   = "claude"
CODEX_CLI    = "codex"
COPILOT_CLI  = "copilot"
CURSOR_CLI   = "cursor-agent"
GROK_CLI     = "grok"

# How long to wait for a CLI to produce its first byte before giving up
FIRST_BYTE_TIMEOUT = 30   # seconds

# Hard cap on total response time per request
RESPONSE_TIMEOUT = 1800   # seconds (30 min default; overridable per alias)

# Common install locations not always in subprocess PATH
_EXTRA_PATHS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/node_modules/.bin"),
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
GROK_PATH     = find_cli(GROK_CLI)

# Subprocess working directory — symlink created by start.sh pointing to ./context/
SQUID_HOME = "/tmp/squid"
