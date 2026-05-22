"""
history.py — Read paginated chat history from Claude CLI's JSONL session files.

Each one-shot claude invocation writes one .jsonl file to:
  ~/.claude/projects/<cwd-with-slashes-as-dashes>/

Files are sorted by mtime (newest first) and paginated.
"""

import json
import re
from pathlib import Path
from typing import Optional

_CTX_STRIP = re.compile(
    r".*</conversation_history>\s*(?:User:\s*)?",
    re.DOTALL,
)

from .stats_db import get_stats


def _project_dir(cwd: str) -> Path:
    slug = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _parse_session(path: Path) -> Optional[dict]:
    title = None
    prompt = None
    timestamp = None
    response_texts: list[str] = []

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                t = e.get("type")

                if t == "ai-title":
                    title = e.get("aiTitle")

                elif t == "last-prompt":
                    raw = (e.get("lastPrompt") or "").strip()
                    if raw and prompt is None:
                        prompt = _CTX_STRIP.sub("", raw).strip() or None

                elif t == "user" and not e.get("isSidechain"):
                    if timestamp is None:
                        timestamp = e.get("timestamp")
                    raw_prompt = _extract_text(e.get("message", {}).get("content", "")).strip()
                    stripped = _CTX_STRIP.sub("", raw_prompt).strip()
                    if stripped:
                        prompt = stripped  # user event always wins over last-prompt

                elif t == "assistant" and not e.get("isSidechain"):
                    text = _extract_text(e.get("message", {}).get("content", [])).strip()
                    if text:
                        response_texts.append(text)

    except OSError:
        return None

    if not prompt and not response_texts:
        return None

    session_id = path.stem
    return {
        "session_id": session_id,
        "timestamp": timestamp,
        "title": title,
        "prompt": prompt or "",
        "response": response_texts[-1] if response_texts else "",
        "stats": get_stats(session_id),
    }


def list_history(cwd: str, offset: int = 0, limit: int = 5) -> dict:
    project_dir = _project_dir(cwd)
    if not project_dir.exists():
        return {"items": [], "total": 0, "has_more": False}

    files = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    total = len(files)
    page = files[offset : offset + limit]

    items = [s for f in page if (s := _parse_session(f)) is not None]

    return {
        "items": items,
        "total": total,
        "has_more": (offset + limit) < total,
    }
