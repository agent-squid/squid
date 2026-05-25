"""
server.py — Chat relay server.

Endpoints
---------
POST /chat
    Body: { message, topic?, alias?, lookback? }
    Response: text/event-stream

GET /history?topic=X&offset=0&limit=10
GET /topics
GET /config/aliases
POST /config/aliases
DELETE /config/aliases/{name}
GET /stats?period=daily|hourly  or  ?group=topic
POST /stats/quota-delta
POST /config/creds
GET /quota
GET /health
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional, Union

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, GROK_PATH, AGY_PATH
from .runners import run_auto, run_claude, run_codex, run_copilot, run_cursor, run_grok, run_antigravity, CLINotFoundError, CLIError, list_active_procs
from .history import list_history
from .topic_queue import TopicDispatcher
from .stats_db import (
    init_db, save_stats, get_aggregated_stats, save_quota_delta, get_stats_by_topic, get_stats_by_model,
    get_topics_summary,
    pin_message, reset_pins,
    get_alias, upsert_alias, delete_alias, list_aliases,
    get_topic, upsert_topic, list_topics,
    insert_user_message, insert_assistant_message, update_assistant_message,
    get_context_history, mark_orphaned_pending, get_message,
)
from . import creds

init_db()
orphaned = mark_orphaned_pending()
if orphaned:
    import logging as _log
    _log.getLogger(__name__).warning("Marked %d orphaned pending messages as error", orphaned)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

dispatcher = TopicDispatcher()

# ---------------------------------------------------------------------------
# App + health check helpers
# ---------------------------------------------------------------------------

def _claude_logged_in() -> bool:
    if not CLAUDE_PATH:
        return False
    try:
        import subprocess
        result = subprocess.run(
            [CLAUDE_PATH, "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn"))
    except Exception:
        return False


def _check_deps():
    missing, warnings = [], []
    if not CLAUDE_PATH:
        missing.append("claude   →  npm install -g @anthropic-ai/claude-code")
    elif not _claude_logged_in():
        warnings.append("claude is installed but not logged in  →  run: claude login")
    if not CODEX_PATH:
        missing.append("codex         →  npm install -g @openai/codex")
    if not COPILOT_PATH:
        missing.append("copilot       →  brew install gh-copilot")
    if not CURSOR_PATH:
        missing.append("cursor-agent  →  install from cursor.com")
    if not GROK_PATH:
        missing.append("grok          →  install from https://x.ai")
    if not AGY_PATH:
        missing.append("agy           →  install from https://antigravity.google")
    if missing:
        log.warning("Missing CLI tools:\n  " + "\n  ".join(missing))
    if warnings:
        log.warning("Auth issues:\n  " + "\n  ".join(warnings))
    if not missing and not warnings:
        log.info("claude=%s  codex=%s  copilot=%s  cursor=%s  grok=%s  agy=%s", CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, GROK_PATH, AGY_PATH)

_check_deps()

app = FastAPI(title="Squid", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UI_DIR = Path(__file__).parent.parent / "ui"

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    topic: str = Field("default")
    alias: Optional[str] = None
    lookback: Union[int, Literal["all"]] = Field(5)


class AliasRequest(BaseModel):
    name: str = Field(..., min_length=1)
    backend: Literal["auto", "claude", "cursor", "grok", "antigravity", "codex", "copilot"] = "auto"
    model: Optional[str] = None
    cwd: Optional[str] = None   # abs path; None = /tmp/squid (bare default)
    timeout: Optional[int] = None  # seconds; None = use global default


class CredsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    session_key: str = Field(..., min_length=1)


class QuotaDeltaRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    before: float
    after: float

class CmdRequest(BaseModel):
    command: Literal["stop", "stopall", "deq", "list"]
    topic: str = "default"
    pos: Optional[int] = None  # deq only: None=all, 1=first, -1=last, 2=second, …

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def sse_chunk(data: str) -> str:
    return "data:" + data.replace("\n", "\ndata:") + "\n\n"

def sse_event(event: str, data: str = "") -> str:
    return f"event: {event}\ndata: {data}\n\n"

# ---------------------------------------------------------------------------
# Streaming response generator
# ---------------------------------------------------------------------------

async def stream_response(
    message: str,
    topic: str,
    alias: Optional[str],
    backend: str,
    model: Optional[str],
    cwd: Optional[str],
    context_history: list[dict],
    asst_msg_id: int,
    response_timeout: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    yield sse_event("meta", json.dumps({"alias": alias, "backend": backend, "msg_id": asst_msg_id}))

    out_q, seq, worker = await dispatcher.dispatch(
        topic=topic, prompt=message, context_history=context_history,
        backend=backend, model=model, alias=alias, cwd=cwd,
        response_timeout=response_timeout,
    )

    raw = ""
    status_raw = ""
    session_id: Optional[str] = None
    last_partial_save = time.monotonic()

    try:
        while True:
            position = worker.position_of(seq)
            if position > 0:
                yield sse_event("queued", json.dumps({"topic": topic, "position": position}))

            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if chunk is None:  # sentinel — worker finished
                break

            if isinstance(chunk, dict) and "_error" in chunk:
                err_text = chunk["_error"]
                yield sse_event("error", err_text)
                update_assistant_message(asst_msg_id, raw or err_text, session_id, "error")
                return

            if isinstance(chunk, dict) and "_stats" in chunk:
                stats = chunk["_stats"]
                session_id = stats.pop("session_id", None)
                if session_id:
                    save_stats(session_id, stats, topic=topic, alias=alias, backend=backend, model=model, cwd=cwd)
                    stats["session_id"] = session_id
                yield sse_event("stats", json.dumps(stats))

            elif isinstance(chunk, dict) and "_status" in chunk:
                status_raw += chunk["_status"]
                text = chunk["_status"].replace("\n", " ")
                if text:
                    yield sse_event("status", text)

            else:
                raw += chunk
                yield sse_chunk(chunk)
                now = time.monotonic()
                if raw and now - last_partial_save >= 3.0:
                    update_assistant_message(asst_msg_id, raw, session_id, "pending")
                    last_partial_save = now

            await asyncio.sleep(0)

        if not raw and status_raw:
            raw = status_raw
            yield sse_chunk(raw)
        update_assistant_message(asst_msg_id, raw, session_id, "done")
        yield sse_event("done")

    except Exception as exc:
        log.exception("Unexpected error in stream_response")
        err_text = f"Internal error: {exc}"
        yield sse_event("error", err_text)
        update_assistant_message(asst_msg_id, raw or err_text, session_id, "error")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    # Resolve alias config: explicit alias wins, else use topic's stored alias
    resolved_alias: Optional[str] = req.alias
    alias_config: dict = {}

    if req.alias:
        alias_config = get_alias(req.alias) or {}
        upsert_topic(req.topic, req.alias)
    else:
        topic_row = get_topic(req.topic)
        if topic_row:
            resolved_alias = topic_row.get("alias")
            if resolved_alias:
                alias_config = get_alias(resolved_alias) or {}
        upsert_topic(req.topic)

    backend = alias_config.get("backend") or "auto"
    model: Optional[str] = alias_config.get("model") or None
    cwd: Optional[str] = alias_config.get("cwd") or None
    response_timeout: Optional[int] = alias_config.get("timeout")

    lookback = 10_000 if req.lookback == "all" else int(req.lookback)
    context_history = get_context_history(req.topic, lookback)

    user_msg_id = insert_user_message(req.topic, resolved_alias, backend, model, req.message)
    asst_msg_id = insert_assistant_message(req.topic, resolved_alias, backend, model, user_msg_id)

    log.info(
        "chat  topic=%s  alias=%s  backend=%s  model=%s  lookback=%d  msg=%.80r",
        req.topic, resolved_alias, backend, model, lookback, req.message,
    )
    return StreamingResponse(
        stream_response(req.message, req.topic, resolved_alias, backend, model, cwd, context_history, asst_msg_id, response_timeout),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/cmd")
async def run_cmd(req: CmdRequest):
    if req.command == "stop":
        killed = dispatcher.stop_topic(req.topic)
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stopall":
        result = dispatcher.stopall_topic(req.topic)
        return JSONResponse({"ok": True, **result})
    if req.command == "deq":
        drained = dispatcher.drain_topic(req.topic, req.pos)
        return JSONResponse({"ok": True, "drained": drained})
    if req.command == "list":
        return JSONResponse({"ok": True, "topics": get_topics_summary()})
    return JSONResponse({"ok": False, "error": "unknown command"}, status_code=400)


@app.get("/processes")
async def processes():
    return JSONResponse(list_active_procs())


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "backends": {
            "claude":   {"available": bool(CLAUDE_PATH),   "path": CLAUDE_PATH},
            "cursor":   {"available": bool(CURSOR_PATH),   "path": CURSOR_PATH},
            "grok":     {"available": bool(GROK_PATH),     "path": GROK_PATH},
            "antigravity": {"available": bool(AGY_PATH),  "path": AGY_PATH},
            "codex":    {"available": bool(CODEX_PATH),    "path": CODEX_PATH},
            "copilot":  {"available": bool(COPILOT_PATH),  "path": COPILOT_PATH},
        },
    })


@app.get("/history")
async def history(offset: int = 0, limit: int = 5, topic: Optional[str] = None):
    return JSONResponse(list_history(topic=topic, offset=offset, limit=limit))


class PinRequest(BaseModel):
    pinned: int  # 1 = pinned, 0 = default, -1 = excluded

@app.get("/chat/{msg_id}/status")
async def message_status(msg_id: int):
    row = get_message(msg_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.post("/chat/reset-pins")
async def reset_all_pins():
    reset_pins()
    return JSONResponse({"ok": True})

@app.post("/chat/{msg_id}/pin")
async def toggle_pin(msg_id: int, req: PinRequest):
    pin_message(msg_id, req.pinned)
    return JSONResponse({"ok": True})


@app.get("/topics")
async def topics_list():
    db_topics = get_topics_summary()  # ordered by recency, includes last_prompt
    queue_map = {t["name"]: t for t in dispatcher.topics_info()}
    for t in db_topics:
        info = queue_map.get(t["name"], {})
        t["queue_depth"] = info.get("queue_depth", 0)
        t["active"] = info.get("active", False)
    return JSONResponse(db_topics)


@app.get("/config/aliases")
async def get_aliases():
    return JSONResponse(list_aliases())


@app.post("/config/aliases")
async def create_alias(req: AliasRequest):
    upsert_alias(req.name, req.backend, req.model, req.cwd, req.timeout)
    return JSONResponse({"ok": True})


@app.delete("/config/aliases/{name}")
async def remove_alias(name: str):
    deleted = delete_alias(name)
    return JSONResponse({"ok": deleted})


@app.get("/stats")
async def usage_stats(period: str = "daily", group: str = "time"):
    if group == "topic":
        return JSONResponse(get_stats_by_topic())
    if group == "model":
        return JSONResponse(get_stats_by_model())
    return JSONResponse(get_aggregated_stats(period))


@app.post("/stats/quota-delta")
async def record_quota_delta(req: QuotaDeltaRequest):
    save_quota_delta(req.session_id, req.before, req.after)
    return JSONResponse({"ok": True})


@app.post("/config/creds")
async def save_creds(req: CredsRequest):
    creds.save(req.org_id.strip(), req.session_key.strip())
    return JSONResponse({"ok": True})


@app.get("/quota")
async def quota():
    org_id = creds.get_org_id()
    session_key = creds.get_session_key()
    if not org_id or not session_key:
        return JSONResponse({"error": "credentials not configured"}, status_code=400)
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            r = await session.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers={"Cookie": f"sessionKey={session_key}"},
                impersonate="chrome",
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"claude.ai returned {r.status_code}"}, status_code=502)
        return JSONResponse(r.json())
    except Exception as exc:
        log.error("quota fetch failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
