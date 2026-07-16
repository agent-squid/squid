"""
history.py — Chat history from the central chat_messages DB table.
"""
from typing import Optional
from .stats_db import get_messages_flat, get_history_items_by_ids


def list_history(topic: Optional[str] = None, agent: Optional[str] = None,
                 adhoc: Optional[bool] = None, offset: int = 0, limit: int = 20,
                 flow_route: Optional[str] = None) -> dict:
    return get_messages_flat(topic=topic, agent=agent, adhoc=adhoc, offset=offset, limit=limit, flow_route=flow_route)


def list_history_by_ids(ids: list[int]) -> dict:
    items = get_history_items_by_ids(ids)
    return {"items": items, "total": len(items)}
