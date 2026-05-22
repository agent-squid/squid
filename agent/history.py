"""
history.py — Chat history from the central chat_messages DB table.
"""
from typing import Optional
from .stats_db import get_messages, get_stats


def list_history(topic: Optional[str] = None, offset: int = 0, limit: int = 5) -> dict:
    data = get_messages(topic=topic, offset=offset, limit=limit)
    for item in data["items"]:
        if item.get("session_id"):
            item["stats"] = get_stats(item["session_id"])
    return data
