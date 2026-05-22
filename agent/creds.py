"""
creds.py — Store and retrieve claude.ai credentials (org_id + session_key).
Saved to squid-creds.json next to squid.db — add to .gitignore.
"""

import json
from pathlib import Path
from typing import Optional

_CREDS_PATH = Path(__file__).parent.parent / "squid-creds.json"


def load() -> dict:
    try:
        return json.loads(_CREDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(org_id: str, session_key: str) -> None:
    _CREDS_PATH.write_text(json.dumps({"org_id": org_id, "session_key": session_key}, indent=2))


def get_org_id() -> Optional[str]:
    return load().get("org_id")


def get_session_key() -> Optional[str]:
    return load().get("session_key")
