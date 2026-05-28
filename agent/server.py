"""
server.py — Chat relay server.

Endpoints
---------
POST /chat
    Body: { message, topic?, agent?, lookback? }
    Response: text/event-stream

GET /history?topic=X&offset=0&limit=10
GET /topics
GET /config/agents
POST /config/agents
DELETE /config/agents/{name}
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
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional, Union

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH, SQUID_HOME
from .runners import run_auto, run_claude, run_codex, run_copilot, run_cursor, run_antigravity, CLINotFoundError, CLIError, list_active_procs, kill_all_procs, kill_procs_by_topic
from .history import list_history
from .topic_queue import TopicDispatcher
from .context_sync import sync_now, maybe_sync
from .stats_db import (
    init_db, save_stats, get_aggregated_stats, save_quota_delta, get_stats_by_topic, get_stats_by_agent,
    get_topics_summary,
    pin_message, reset_pins, reset_topic_pins,
    get_agent, upsert_agent, delete_agent, list_agents,
    get_topic, upsert_topic, list_topics,
    insert_user_message, insert_assistant_message, update_assistant_message,
    get_context_history, mark_orphaned_pending, get_message,
    get_topic_session, set_topic_session, clear_topic_session,
    get_pending_injections, mark_injected, get_session_context_log,
    delete_topic, get_topic_agents,
)
from . import creds

init_db()
orphaned = mark_orphaned_pending()
if orphaned:
    import logging as _log
    _log.getLogger(__name__).warning("Marked %d orphaned pending messages as error", orphaned)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

BOOT_TIME = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
    if not AGY_PATH:
        missing.append("agy           →  install from https://antigravity.google")
    if missing:
        log.warning("Missing CLI tools:\n  " + "\n  ".join(missing))
    if warnings:
        log.warning("Auth issues:\n  " + "\n  ".join(warnings))
    if not missing and not warnings:
        log.info("claude=%s  codex=%s  copilot=%s  cursor=%s  agy=%s", CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH)

_check_deps()
sync_now()

app = FastAPI(title="Squid", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UI_DIR = Path(__file__).parent.parent / "ui"

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    topic: str = Field("default")
    agent: Optional[str] = None
    lookback: int = Field(0)
    adhoc: bool = Field(False)


class AgentRequest(BaseModel):
    name: str = Field(..., min_length=1)
    backend: Literal["auto", "claude", "cursor", "antigravity", "codex", "copilot"] = "auto"
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
    command: Literal["stop", "stopall", "deq", "list", "restart", "clear", "compact"]
    topic: str = "default"
    agent: Optional[str] = None  # clear/compact: target agent (falls back to sticky)
    pos: Optional[int] = None    # deq only: None=all, 1=first, -1=last, 2=second, …

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def sse_chunk(data: str) -> str:
    return "data:" + data.replace("\n", "\ndata:") + "\n\n"

def sse_event(event: str, data: str = "") -> str:
    return f"event: {event}\ndata: {data}\n\n"

# ---------------------------------------------------------------------------
# Background drain — runs after client disconnects mid-stream
# ---------------------------------------------------------------------------

async def _drain_to_completion(
    out_q: asyncio.Queue,
    msg_id: int,
    raw: str,
    status_raw: str,
    session_id: Optional[str],
    tool_events: Optional[list] = None,
    topic: Optional[str] = None,
    agent: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    adhoc: bool = False,
    lookback: int = 0,
    pin_count: int = 0,
) -> None:
    """Drain the worker queue after client disconnect; save final content to DB."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 300.0  # 5-minute hard cap
    tool_events = list(tool_events or [])
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                log.warning("drain timeout msg_id=%s, saving partial", msg_id)
                break
            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=min(left, 30.0))
            except asyncio.TimeoutError:
                break
            if chunk is None:  # sentinel — worker finished
                break
            if isinstance(chunk, dict):
                if "_tool" in chunk:
                    tool_events.append(chunk["_tool"])
                if "_status" in chunk:
                    status_raw += chunk["_status"]
                if "_error" in chunk:
                    break
                if "_stats" in chunk and not session_id:
                    # Client disconnected before stats arrived — capture now
                    stats = dict(chunk["_stats"])
                    session_id = stats.pop("session_id", None)
                    if session_id:
                        stats["adhoc"] = adhoc
                        stats["lookback"] = lookback
                        stats["pin_count"] = pin_count
                        save_stats(session_id, stats, topic=topic, agent=agent,
                                   backend=backend, model=model, cwd=cwd,
                                   lookback=lookback, pin_count=pin_count)
                        if agent and not adhoc:
                            set_topic_session(topic, agent, session_id, cwd or SQUID_HOME)
            else:
                raw += chunk
    except Exception:
        log.exception("drain error msg_id=%s", msg_id)

    content = raw or status_raw or ""
    tools_json = json.dumps(tool_events) if tool_events else None
    try:
        update_assistant_message(msg_id, content, session_id, "done" if content else "error", tools=tools_json)
        log.info("drain complete msg_id=%s len=%d tools=%d sid=%s", msg_id, len(content), len(tool_events), session_id)
    except Exception:
        log.exception("drain save failed msg_id=%s", msg_id)

# ---------------------------------------------------------------------------
# Streaming response generator
# ---------------------------------------------------------------------------

async def stream_response(
    message: str,
    topic: str,
    agent: Optional[str],
    backend: str,
    model: Optional[str],
    cwd: Optional[str],
    context_history: list[dict],
    asst_msg_id: int,
    response_timeout: Optional[int] = None,
    resume_session_id: Optional[str] = None,
    inject_history: Optional[list[dict]] = None,
    pending_inject_ids: Optional[list[int]] = None,
    adhoc: bool = False,
    lookback: int = 0,
    pin_count: int = 0,
) -> AsyncGenerator[str, None]:
    yield sse_event("meta", json.dumps({"agent": agent, "backend": backend, "msg_id": asst_msg_id, "adhoc": adhoc}))

    effective_cwd = cwd or SQUID_HOME  # matches what topic_queue._process uses
    out_q, seq, worker = await dispatcher.dispatch(
        topic=topic, prompt=message, context_history=context_history,
        backend=backend, model=model, agent=agent, cwd=effective_cwd,
        response_timeout=response_timeout,
        resume_session_id=resume_session_id,
        inject_history=inject_history or [],
        adhoc=adhoc,
    )

    raw = ""
    status_raw = ""
    tool_events: list[dict] = []
    session_id: Optional[str] = None
    last_partial_save = time.monotonic()
    _completed = False  # tracks whether we reached the normal done path

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
                if raw:
                    # Content already streamed — CLI exited non-zero after delivering the
                    # response (e.g. quota message). Treat as done so the content isn't
                    # replaced by a generic error on the client.
                    tools_json = json.dumps(tool_events) if tool_events else None
                    update_assistant_message(asst_msg_id, raw, session_id, "done", tools=tools_json)
                    yield sse_event("done")
                else:
                    yield sse_event("error", err_text)
                    update_assistant_message(asst_msg_id, err_text, session_id, "error")
                _completed = True
                return

            if isinstance(chunk, dict) and "_stats" in chunk:
                stats = chunk["_stats"]
                session_id = stats.pop("session_id", None)
                stats["adhoc"] = adhoc
                stats["lookback"] = lookback
                stats["pin_count"] = pin_count
                if session_id:
                    save_stats(session_id, stats, topic=topic, agent=agent, backend=backend, model=model, cwd=effective_cwd, lookback=lookback, pin_count=pin_count)
                    stats["session_id"] = session_id
                    stats["cwd"] = effective_cwd
                    if agent and not adhoc:
                        set_topic_session(topic, agent, session_id, effective_cwd)
                        if pending_inject_ids:
                            mark_injected(topic, agent, pending_inject_ids)
                yield sse_event("stats", json.dumps(stats))
                if adhoc and pin_count > 0:
                    reset_topic_pins(topic)

            elif isinstance(chunk, dict) and "_tool" in chunk:
                tool_events.append(chunk["_tool"])
                yield sse_event("tool", json.dumps(chunk["_tool"]))

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
        tools_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, raw, session_id, "done", tools=tools_json)
        yield sse_event("done")
        _completed = True

    except Exception as exc:
        log.exception("Unexpected error in stream_response")
        err_text = f"Internal error: {exc}"
        yield sse_event("error", err_text)
        tools_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, raw or err_text, session_id, "error", tools=tools_json)
        _completed = True

    finally:
        # Runs on client disconnect (CancelledError mid-stream)
        if not _completed:
            # Mark as pending immediately so a page reload shows in-progress state
            try:
                update_assistant_message(asst_msg_id, raw or status_raw or "", session_id, "pending")
            except Exception:
                pass
            # Background task drains the worker and saves final content when done
            asyncio.create_task(
                _drain_to_completion(
                    out_q, asst_msg_id, raw, status_raw, session_id, tool_events,
                    topic=topic, agent=agent, backend=backend, model=model,
                    cwd=effective_cwd, adhoc=adhoc, lookback=lookback, pin_count=pin_count,
                ),
                name=f"squid-drain-{asst_msg_id}",
            )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    # 1. Resolve agent — explicit wins, else use topic sticky
    resolved_agent: Optional[str] = req.agent
    agent_config: dict = {}

    if req.agent:
        agent_config = get_agent(req.agent) or {}
        if not agent_config:
            return JSONResponse(
                {"error": f"Agent '{req.agent}' not found. Create it first via /config/agents."},
                status_code=400,
            )
        upsert_topic(req.topic, req.agent)  # update sticky immediately
    else:
        topic_row = get_topic(req.topic)
        if topic_row:
            resolved_agent = topic_row.get("agent")
            if resolved_agent:
                agent_config = get_agent(resolved_agent) or {}
        upsert_topic(req.topic)

    backend = agent_config.get("backend") or "auto"
    model: Optional[str] = agent_config.get("model") or None
    agent_cwd: Optional[str] = agent_config.get("cwd") or None
    response_timeout: Optional[int] = agent_config.get("timeout")

    # 2. Resumable session lookup (skipped for adhoc turns)
    resume_session_id: Optional[str] = None
    cwd: Optional[str] = agent_cwd
    inject_history: list[dict] = []
    pending_inject_ids: list[int] = []

    if not req.adhoc and resolved_agent:
        stored = get_topic_session(req.topic, resolved_agent)
        if stored:
            resume_session_id = stored["session_id"]
            cwd = stored["cwd"]  # locked cwd takes precedence
        # For resumed sessions, exclude pins already in that session (--resume carries them).
        pending = get_pending_injections(req.topic, resolved_agent,
                                         exclude_session_id=resume_session_id)
        if pending:
            inject_history = [{"role": p["role"], "content": p["content"]} for p in pending]
            pending_inject_ids = [p["id"] for p in pending]

    # 3. Context history + pin counts
    lookback = int(req.lookback) if req.lookback else 0
    pin_count = 0
    if req.adhoc:
        # Pinned topic-wide. Recent-N scoped to resolved agent when active.
        context_history, pin_count = get_context_history(req.topic, lookback,
                                                          agent=resolved_agent if lookback > 0 else None)
    elif not resume_session_id and not resolved_agent:
        # No agent — inject_history won't run, so use context_history for pinned messages.
        context_history, pin_count = get_context_history(req.topic, 0)
    else:
        # Agent session: inject_history carries pins. Resumed: CLI owns all context.
        context_history = []
        pin_count = sum(1 for p in pending if p["role"] == "assistant") if pending_inject_ids else 0

    user_msg_id = insert_user_message(req.topic, resolved_agent, backend, model, req.message)
    asst_msg_id = insert_assistant_message(req.topic, resolved_agent, backend, model, user_msg_id, adhoc=req.adhoc)

    log.info(
        "chat  topic=%s  agent=%s  backend=%s  model=%s  adhoc=%s  resume=%s  injections=%d  msg=%.80r",
        req.topic, resolved_agent, backend, model, req.adhoc,
        bool(resume_session_id), len(pending_inject_ids), req.message,
    )
    await maybe_sync()
    return StreamingResponse(
        stream_response(
            req.message, req.topic, resolved_agent, backend, model, cwd,
            context_history, asst_msg_id, response_timeout,
            resume_session_id=resume_session_id,
            inject_history=inject_history,
            pending_inject_ids=pending_inject_ids,
            adhoc=req.adhoc,
            lookback=lookback,
            pin_count=pin_count,
        ),
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
    if req.command == "restart":
        async def _restart():
            await asyncio.sleep(0.4)
            kill_all_procs()
            if "--reload" in sys.argv:
                # In reload mode, touch a watched file — watchfiles detects
                # the change and gracefully cycles the worker. Sending SIGTERM
                # to the worker instead causes the reloader parent to exit too.
                Path(__file__).touch()
            else:
                os.execv(sys.argv[0], sys.argv)
        asyncio.create_task(_restart())
        return JSONResponse({"ok": True})

    if req.command in ("clear", "compact"):
        agent = req.agent
        if not agent:
            topic_row = get_topic(req.topic)
            agent = topic_row.get("agent") if topic_row else None
        if not agent:
            return JSONResponse({"ok": False, "error": "no active session"}, status_code=400)
        kill_procs_by_topic(req.topic)
        clear_topic_session(req.topic, agent)
        return JSONResponse({"ok": True, "agent": agent})

    return JSONResponse({"ok": False, "error": "unknown command"}, status_code=400)


@app.get("/processes")
async def processes():
    return JSONResponse(list_active_procs())


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "boot_time": BOOT_TIME,
        "backends": {
            "claude":   {"available": bool(CLAUDE_PATH),   "path": CLAUDE_PATH},
            "cursor":   {"available": bool(CURSOR_PATH),   "path": CURSOR_PATH},
            "antigravity": {"available": bool(AGY_PATH),  "path": AGY_PATH},
            "codex":    {"available": bool(CODEX_PATH),    "path": CODEX_PATH},
            "copilot":  {"available": bool(COPILOT_PATH),  "path": COPILOT_PATH},
        },
    })


@app.get("/history")
async def history(offset: int = 0, limit: int = 5, topic: Optional[str] = None, agent: Optional[str] = None):
    return JSONResponse(list_history(topic=topic, agent=agent, offset=offset, limit=limit))


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


@app.delete("/topics/{topic}")
async def remove_topic(topic: str):
    deleted = delete_topic(topic)
    return JSONResponse({"ok": deleted})


@app.get("/topics/{topic}/sessions")
async def list_topic_sessions(topic: str):
    agents = get_topic_agents(topic)
    return JSONResponse({"agents": agents})


@app.get("/topics/{topic}/session")
async def get_session(topic: str, agent: str):
    stored = get_topic_session(topic, agent)
    if not stored:
        return JSONResponse({"session_id": None, "cwd": None, "pending_injections": [], "already_injected": []})
    pending = get_pending_injections(topic, agent)
    absorbed = get_session_context_log(topic, agent)
    return JSONResponse({
        "session_id": stored["session_id"],
        "cwd": stored["cwd"],
        "pending_injections": pending,
        "already_injected": absorbed,
    })


@app.delete("/topics/{topic}/session")
async def clear_session(topic: str, agent: str):
    clear_topic_session(topic, agent)
    return JSONResponse({"ok": True})


@app.get("/context/{topic}")
async def context_view(topic: str, agent: str):
    stored = get_topic_session(topic, agent)
    pending = get_pending_injections(topic, agent)
    absorbed = get_session_context_log(topic, agent)
    return JSONResponse({
        "session_id": stored["session_id"] if stored else None,
        "cwd": stored["cwd"] if stored else None,
        "pending_injections": pending,
        "already_injected": absorbed,
    })


@app.get("/config/agents")
async def get_agents():
    return JSONResponse(list_agents())


@app.post("/config/agents")
async def create_agent(req: AgentRequest):
    upsert_agent(req.name, req.backend, req.model, req.cwd, req.timeout)
    return JSONResponse({"ok": True})


@app.delete("/config/agents/{name}")
async def remove_agent(name: str):
    deleted = delete_agent(name)
    return JSONResponse({"ok": deleted})


@app.get("/stats")
async def usage_stats(period: str = "daily", group: str = "time"):
    if group == "topic":
        return JSONResponse(get_stats_by_topic())
    if group == "agent":
        return JSONResponse(get_stats_by_agent())
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
