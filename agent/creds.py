"""
creds.py — Store and retrieve service credentials.
Saved to ~/.squid/squid-creds.json alongside ~/.squid/squid.db and
~/.squid/squid.yaml.
"""

import json
import tempfile
from pathlib import Path
from typing import Optional

_CREDS_PATH = Path.home() / ".squid" / "squid-creds.json"
_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


def load() -> dict:
    try:
        return json.loads(_CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_credentials(data: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=_CREDS_PATH.parent,
        prefix=f".{_CREDS_PATH.name}.", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2)
            stream.close()
            temporary.replace(_CREDS_PATH)
        finally:
            temporary.unlink(missing_ok=True)


def _save(data: dict) -> None:
    existing = load()
    existing.update(data)
    _write_credentials(existing)


def save(org_id: str, session_key: str, email: str = "") -> None:
    _save({"claude_org_id": org_id, "claude_session_key": session_key, "claude_email": email})


def save_codex(token: str, account_id: str = "") -> None:
    _save({"codex_token": token, "codex_account_id": account_id})


def get_org_id() -> Optional[str]:
    d = load()
    return d.get("claude_org_id") or d.get("org_id")


def get_session_key() -> Optional[str]:
    d = load()
    return d.get("claude_session_key") or d.get("session_key")


def get_codex_token() -> Optional[str]:
    return load().get("codex_token")


def get_codex_account_id() -> Optional[str]:
    return load().get("codex_account_id")


def get_codex_cli_auth() -> dict:
    try:
        data = json.loads(_CODEX_AUTH_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def claude_browser_profiles() -> dict[str, tuple[str, Path | None]]:
    """List host profiles without opening their cookie databases."""
    chrome_root = Path.home() / "Library/Application Support/Google"
    profiles = {}
    for browser in ("Chrome", "Chrome Beta", "Chrome Dev"):
        root = chrome_root / browser
        try:
            info = json.loads((root / "Local State").read_text())["profile"]["info_cache"]
        except (OSError, ValueError, KeyError, TypeError):
            info = {}
        for directory in sorted(root.glob("*")):
            if directory.name != "Default" and not directory.name.startswith("Profile "):
                continue
            cookie_file = next(
                (directory / path for path in ("Network/Cookies", "Cookies")
                 if (directory / path).is_file()),
                None,
            )
            if cookie_file:
                metadata = info.get(directory.name, {}) if isinstance(info, dict) else {}
                name = metadata.get("name") if isinstance(metadata, dict) else None
                label = f"{browser} · {name} ({directory.name})" if name else f"{browser} · {directory.name}"
                profile_id = f"{browser}/{directory.name}" if browser != "Chrome" else directory.name
                profiles[profile_id] = (label, cookie_file)
    profiles["safari"] = ("Safari", None)
    return profiles


def read_claude_browser_creds(profile: str) -> dict:
    import browser_cookie3

    selected = claude_browser_profiles().get(profile)
    if selected is None:
        raise ValueError("Choose an available browser profile.")
    _, cookie_file = selected
    try:
        jar = (browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name="claude.ai")
               if cookie_file else browser_cookie3.safari(domain_name="claude.ai"))
        cookies = {}
        for cookie in jar:
            if (
                cookie.name in ("sessionKey", "lastActiveOrg")
                and cookie.domain.lstrip(".") == "claude.ai"
                and cookie.path == "/"
                and not cookie.is_expired()
            ):
                if cookie.name not in cookies or cookie.domain == "claude.ai":
                    cookies[cookie.name] = cookie.value
    except Exception as exc:
        raise ValueError("Could not read Claude cookies from the selected browser profile.") from exc
    return {key: cookies.get(key) for key in ("sessionKey", "lastActiveOrg")}


def save_max_budget(gauge: str, amount: float) -> None:
    data = load()
    budgets = data.get("max_budgets") or {}
    budgets[gauge] = amount
    data["max_budgets"] = budgets
    if gauge == "deepseek":
        data.pop("deepseek_max_budget", None)  # superseded legacy flat key
    _write_credentials(data)


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
    _write_credentials(data)


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
