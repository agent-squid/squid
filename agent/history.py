"""
history.py — Chat history from the central chat_messages DB table.
"""
from typing import Optional
from .stats_db import (
    get_messages_flat, get_history_items_by_ids,
    get_messages_around, get_messages_around_flow, get_messages_from_cursor,
)


def list_history(topic: Optional[str] = None, agent: Optional[str] = None,
                 adhoc: Optional[bool] = None, offset: int = 0, limit: int = 20,
                 flow_route: Optional[str] = None, bookmarked: bool = False,
                 marked_bad: bool = False) -> dict:
    return get_messages_flat(
        topic=topic,
        agent=agent,
        adhoc=adhoc,
        offset=offset,
        limit=limit,
        flow_route=flow_route,
        bookmarked=bookmarked,
        marked_bad=marked_bad,
    )


def list_history_by_ids(ids: list[int]) -> dict:
    items = get_history_items_by_ids(ids)
    return {"items": items, "total": len(items)}


def list_history_around(msg_id: Optional[int] = None, flow_run_id: Optional[str] = None,
                        before: int = 20, after: int = 20,
                        direction: Optional[str] = None, cursor_completed_at: Optional[str] = None,
                        cursor_id: Optional[int] = None, limit: int = 20,
                        topic: Optional[str] = None, agent: Optional[str] = None,
                        adhoc: Optional[bool] = None, flow_route: Optional[str] = None,
                        bookmarked: bool = False, marked_bad: bool = False) -> dict:
    if direction and cursor_completed_at and cursor_id is not None:
        return get_messages_from_cursor(
            direction=direction,
            cursor_completed_at=cursor_completed_at,
            cursor_id=cursor_id,
            limit=limit,
            topic=topic,
            agent=agent,
            adhoc=adhoc,
            flow_route=flow_route,
            bookmarked=bookmarked,
            marked_bad=marked_bad,
        )
    if msg_id is None:
        if flow_run_id:
            return get_messages_around_flow(
                flow_run_id,
                before=before,
                after=after,
                topic=topic,
                agent=agent,
                adhoc=adhoc,
                flow_route=flow_route,
                bookmarked=bookmarked,
                marked_bad=marked_bad,
            )
        return {"items": [], "found": False, "has_older": False, "has_newer": False}
    return get_messages_around(
        msg_id,
        before=before,
        after=after,
        topic=topic,
        agent=agent,
        adhoc=adhoc,
        flow_route=flow_route,
        bookmarked=bookmarked,
        marked_bad=marked_bad,
    )
