"""
history.py — Chat history from the central chat_messages DB table.
"""
from typing import Optional
from .stats_db import get_messages_flat


def list_history(topic: Optional[str] = None, agent: Optional[str] = None, offset: int = 0, limit: int = 20) -> dict:
    return get_messages_flat(topic=topic, agent=agent, offset=offset, limit=limit)
