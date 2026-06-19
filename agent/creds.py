"""
creds.py — Store and retrieve service credentials.
Saved to ~/.squid/squid-creds.json alongside squid.db and squid.yaml.
"""

import json
from pathlib import Path
from typing import Optional

_CREDS_PATH = Path.home() / ".squid" / "squid-creds.json"


def load() -> dict:
    try:
        return json.loads(_CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    existing = load()
    existing.update(data)
    _CREDS_PATH.write_text(json.dumps(existing, indent=2))


def save(org_id: str, session_key: str) -> None:
    _save({"org_id": org_id, "session_key": session_key})


def save_codex(token: str) -> None:
    _save({"codex_token": token})


def get_org_id() -> Optional[str]:
    return load().get("org_id")


def get_session_key() -> Optional[str]:
    return load().get("session_key")


def get_codex_token() -> Optional[str]:
    return load().get("codex_token")


def read_chrome_claude_creds() -> dict:
    """Read sessionKey and lastActiveOrg from Chrome's cookie store (macOS only)."""
    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(domain_name="claude.ai")
        result = {}
        for cookie in jar:
            if cookie.name in ("sessionKey", "lastActiveOrg"):
                result[cookie.name] = cookie.value
        return result
    except Exception as e:
        raise RuntimeError(f"Could not read Chrome cookies: {e}")


def get_cursor_token() -> Optional[str]:
    """Read Cursor access token from macOS Keychain (where cursor-agent stores it)."""
    try:
        import subprocess
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "cursor-access-token", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        token = result.stdout.strip()
        return token or None
    except Exception:
        return None
