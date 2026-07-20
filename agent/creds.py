"""
creds.py — Store and retrieve service credentials.
Saved to ~/.squid/squid-creds.json alongside ~/.squid/squid.db and
~/.squid/squid.yaml.
"""

import json
from pathlib import Path
from typing import Optional

_CREDS_PATH = Path.home() / ".squid" / "squid-creds.json"
_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


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
    _save({"claude_org_id": org_id, "claude_session_key": session_key})


def save_codex(token: str) -> None:
    _save({"codex_token": token})


def get_org_id() -> Optional[str]:
    d = load()
    return d.get("claude_org_id") or d.get("org_id")


def get_session_key() -> Optional[str]:
    d = load()
    return d.get("claude_session_key") or d.get("session_key")


def get_codex_token() -> Optional[str]:
    return load().get("codex_token")


def get_codex_cli_auth() -> dict:
    try:
        data = json.loads(_CODEX_AUTH_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def _extract_cookies(domain: str) -> dict:
    """Read all cookies for a domain from Chrome then Safari."""
    import browser_cookie3
    errors = []
    for loader, name in [(browser_cookie3.chrome, "Chrome"), (browser_cookie3.safari, "Safari")]:
        try:
            jar = loader(domain_name=domain)
            cookies = {c.name: c.value for c in jar}
            if cookies:
                return cookies
        except PermissionError:
            errors.append(
                f"{name}: permission denied — grant Full Disk Access to Terminal in "
                f"System Settings → Privacy & Security → Full Disk Access"
            )
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError(" | ".join(errors) if errors else
                       f"No cookies found for {domain} in Chrome or Safari. Make sure you are logged in.")


def read_chrome_claude_creds() -> dict:
    """Read sessionKey and lastActiveOrg from Chrome or Safari cookie store (macOS only)."""
    cookies = _extract_cookies("claude.ai")
    result = {k: v for k, v in cookies.items() if k in ("sessionKey", "lastActiveOrg")}
    if not result.get("sessionKey"):
        raise RuntimeError("sessionKey not found in claude.ai cookies. Make sure you are logged in.")
    return result


def save_max_budget(gauge: str, amount: float) -> None:
    data = load()
    budgets = data.get("max_budgets") or {}
    budgets[gauge] = amount
    data["max_budgets"] = budgets
    if gauge == "deepseek":
        data.pop("deepseek_max_budget", None)  # superseded legacy flat key
    _CREDS_PATH.write_text(json.dumps(data, indent=2))


def get_max_budget(gauge: str) -> Optional[float]:
    data = load()
    budgets = data.get("max_budgets") or {}
    if gauge in budgets:
        return budgets[gauge]
    if gauge == "deepseek":
        return data.get("deepseek_max_budget")  # legacy flat key
    return None


def clear_max_budget(gauge: str) -> None:
    data = load()
    budgets = data.get("max_budgets") or {}
    budgets.pop(gauge, None)
    data["max_budgets"] = budgets
    if gauge == "deepseek":
        data.pop("deepseek_max_budget", None)
    _CREDS_PATH.write_text(json.dumps(data, indent=2))


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
