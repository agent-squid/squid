"""
server.py — Chat relay server.

Endpoints
---------
POST /chat
    Body: { message, topic?, agent?, lookback?, adhoc? }
    Response: text/event-stream

GET /history?topic=X&agent=Y&offset=0&limit=10
GET /topics
GET /config/agents
POST /config/agents
DELETE /config/agents/{name}
GET /stats?period=daily|hourly  or  ?group=topic|agent
POST /stats/quota-delta
POST /config/creds
GET /quota
GET /quota/claude
GET /quota/codex
GET /health
"""

import asyncio
import base64
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

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH, SQUID_HOME, _cfg
from .runners import run_claude, run_codex, run_copilot, run_cursor, run_antigravity, CLINotFoundError, CLIError, list_active_procs, kill_all_procs, kill_procs_by_topic, kill_proc_by_msg_id, get_active_agent_for_topic
from .history import list_history
from .topic_queue import TopicDispatcher
from .context_sync import sync_now, maybe_sync
from .topics import normalize_topic_slug
from .memory import (
    code_roots_prompt_block,
    read_topic_memory,
    topic_memory_path,
    topic_memory_prompt_block,
    topic_memory_squid_config,
    write_topic_memory_squid_code_roots,
    write_topic_memory,
)
from .stats_db import (
    init_db, get_aggregated_stats, save_quota_delta, get_stats_by_topic, get_stats_by_agent,
    get_topics_summary, get_topics_management_summary,
    get_agent, upsert_agent, delete_agent, list_agents, get_default_agent,
    get_topic, upsert_topic, list_topics,
    insert_user_message, insert_assistant_message, update_assistant_message,
    update_message_quota_snapshot,
    get_context_history, get_messages_by_ids, mark_orphaned_pending, get_message,
    get_session_injected_ids,
    get_topic_session, clear_topic_session,
    delete_topic, hide_topic, set_topic_hidden, get_topic_agents, get_topic_agent_history,
    clear_agent_sessions, get_agent_sessions,
    get_diff_revert_eligibility, record_git_diff_revert, get_message_gitdiff,
)
from .journal import _generate_journal, _current_week, list_topic_journals, read_journal
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

# Static file extensions served by the UI — exempt from bearer-token auth so
# the page loads before the browser has a token in localStorage.
_STATIC_EXTS = {".js", ".css", ".html", ".ico", ".png", ".svg",
                ".woff", ".woff2", ".ttf", ".map", ".json", ".webmanifest"}

_AUTH_TOKEN: str = (_cfg.get("server") or {}).get("token") or ""

@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    if not _AUTH_TOKEN:
        return await call_next(request)
    path = request.url.path
    # Let the UI shell and its static assets through unauthenticated so the
    # page can load and read the token from localStorage / URL param.
    if path == "/" or Path(path).suffix in _STATIC_EXTS:
        return await call_next(request)
    if path == "/localfile" and request.query_params.get("token") == _AUTH_TOKEN:
        return await call_next(request)
    if request.headers.get("Authorization") == f"Bearer {_AUTH_TOKEN}":
        return await call_next(request)
    return JSONResponse({"error": "unauthorized"}, status_code=401)

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    topic: str = Field("default")
    agent: Optional[str] = None
    lookback: int = Field(0)
    lookback_via_pins: bool = Field(False)
    adhoc: bool = Field(False)
    pinned_ids: Optional[list[int]] = None
    include_topic_memory: bool = Field(False)


class TopicMemoryRequest(BaseModel):
    content: str = ""


class TopicMemoryCodeRootsRequest(BaseModel):
    code_roots: Optional[list[str]] = None
    code_roots_skipped: bool = False


class TopicHiddenRequest(BaseModel):
    hidden: bool = Field(False)


class AgentRequest(BaseModel):
    name: str = Field(..., min_length=1)
    backend: Literal["auto", "claude", "cursor", "antigravity", "codex", "copilot"] = "auto"
    model: Optional[str] = None
    cwd: Optional[str] = None
    timeout: Optional[int] = None


class CredsRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    session_key: str = Field(..., min_length=1)


class CodexCredsRequest(BaseModel):
    token: str = Field(..., min_length=1)


class QuotaDeltaRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    before: float
    after: float


def _decode_jwt_payload(token: str) -> Optional[dict]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _codex_bearer_header(token: str) -> Optional[str]:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token

    payload = _decode_jwt_payload(token)
    if not payload:
        return None

    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    scopes = payload.get("scp") or []
    if (
        "https://api.openai.com/v1" in audiences
        or "model.request" in scopes
        or "https://api.openai.com/auth" in payload
    ):
        return f"Bearer {token}"
    return None


class CmdRequest(BaseModel):
    command: Literal["stop", "stopall", "deq", "list", "restart", "clear", "compact", "stop_msg", "journal"]
    topic: str = "default"
    agent: Optional[str] = None
    adhoc: Optional[bool] = None
    pos: Optional[int] = None
    msg_id: Optional[int] = None


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def sse_chunk(data: str) -> str:
    return "data:" + data.replace("\n", "\ndata:") + "\n\n"

def sse_event(event: str, data: str = "") -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _normalize_topic_response(topic: str) -> Union[str, JSONResponse]:
    try:
        return normalize_topic_slug(topic)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

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
) -> None:
    """Drain the worker queue after client disconnect; save final content to DB."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 300.0
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
            if chunk is None:
                break
            if isinstance(chunk, dict):
                if "_tool" in chunk:
                    tool_events.append(chunk["_tool"])
                if "_status" in chunk:
                    status_raw += chunk["_status"]
                if "_error" in chunk:
                    break
                if "_stats" in chunk and not session_id:
                    stats = dict(chunk["_stats"])
                    session_id = stats.get("session_id")
            else:
                raw += chunk
    except Exception:
        log.exception("drain error msg_id=%s", msg_id)

    content = raw or status_raw or ""
    context_json = json.dumps(tool_events) if tool_events else None
    try:
        update_assistant_message(msg_id, content, session_id, "done" if content else "error", context=context_json, only_if_pending=True)
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
    adhoc: bool = False,
    lookback: int = 0,
    code_roots: Optional[list[str]] = None,
) -> AsyncGenerator[str, None]:
    yield sse_event("meta", json.dumps({"agent": agent, "backend": backend, "msg_id": asst_msg_id, "adhoc": adhoc}))

    effective_cwd = cwd or SQUID_HOME
    out_q, seq, worker = await dispatcher.dispatch(
        topic=topic, prompt=message, context_history=context_history,
        backend=backend, model=model, agent=agent, cwd=effective_cwd,
        response_timeout=response_timeout,
        resume_session_id=resume_session_id,
        adhoc=adhoc, lookback=lookback, msg_id=asst_msg_id,
        code_roots=code_roots,
    )

    raw = ""
    status_raw = ""
    tool_events: list[dict] = []
    session_id: Optional[str] = None
    last_partial_save = time.monotonic()
    _completed = False

    try:
        while True:
            position = worker.position_of(seq)
            if position > 0:
                yield sse_event("queued", json.dumps({"topic": topic, "position": position}))

            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                break

            if isinstance(chunk, dict) and "_error" in chunk:
                err_text = chunk["_error"]
                if raw:
                    context_json = json.dumps(tool_events) if tool_events else None
                    update_assistant_message(asst_msg_id, raw, session_id, "done", context=context_json, only_if_pending=True)
                    yield sse_event("done")
                else:
                    yield sse_event("error", err_text)
                    update_assistant_message(asst_msg_id, err_text, session_id, "error", only_if_pending=True)
                _completed = True
                return

            if isinstance(chunk, dict) and "_stats" in chunk:
                stats = dict(chunk["_stats"])
                session_id = stats.get("session_id")
                yield sse_event("stats", json.dumps(stats))

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
        context_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, raw, session_id, "done", context=context_json, only_if_pending=True)
        yield sse_event("done")
        _completed = True

    except Exception as exc:
        log.exception("Unexpected error in stream_response")
        err_text = f"Internal error: {exc}"
        yield sse_event("error", err_text)
        context_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, err_text, session_id, "error", context=context_json)
        _completed = True

    finally:
        if not _completed:
            try:
                update_assistant_message(asst_msg_id, raw or status_raw or "", session_id, "pending", only_if_pending=True)
            except Exception:
                pass
            asyncio.create_task(
                _drain_to_completion(
                    out_q, asst_msg_id, raw, status_raw, session_id, tool_events,
                    topic=topic, agent=agent, backend=backend, model=model,
                    cwd=effective_cwd, adhoc=adhoc, lookback=lookback,
                ),
                name=f"squid-drain-{asst_msg_id}",
            )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic

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
    else:
        topic_row = get_topic(topic)
        if topic_row:
            resolved_agent = topic_row.get("agent")
            if resolved_agent:
                agent_config = get_agent(resolved_agent) or {}
        if not agent_config:
            default = get_default_agent()
            if default:
                resolved_agent = default["name"]
                agent_config = default

    backend = agent_config.get("backend") or "claude"
    model: Optional[str] = agent_config.get("model") or None

    upsert_topic(topic, resolved_agent, last_prompt=req.message,
                 last_backend=backend, last_model=model, adhoc=req.adhoc)
    agent_cwd: Optional[str] = agent_config.get("cwd") or None
    response_timeout: Optional[int] = agent_config.get("timeout")

    # 2. Resumable session lookup (skipped for adhoc turns)
    resume_session_id: Optional[str] = None
    cwd: Optional[str] = agent_cwd

    if not req.adhoc and resolved_agent:
        stored = get_topic_session(topic, resolved_agent)
        if stored:
            resume_session_id = stored["session_id"]
            cwd = stored["cwd"]

    # 3. Context history for adhoc turns
    lookback = int(req.lookback) if req.lookback else 0
    context_history: list[dict] = []
    context_ids: Optional[list[int]] = None

    if req.adhoc and lookback > 0 and not req.lookback_via_pins:
        context_history, context_ids = get_context_history(
            topic, lookback, agent=resolved_agent
        )

    # Inject pinned messages — works for both adhoc and session turns
    effective_message = req.message
    prefix_blocks: list[str] = []
    memory_config = topic_memory_squid_config(topic)
    code_roots = memory_config.get("code_roots") or []
    if code_roots:
        code_roots_block = code_roots_prompt_block(code_roots)
        if code_roots_block:
            prefix_blocks.append(code_roots_block)
    tracking_roots: list[str] = code_roots
    if req.include_topic_memory:
        memory_block = topic_memory_prompt_block(topic)
        if memory_block:
            prefix_blocks.append(memory_block)

    if req.pinned_ids:
        lookback_id_set = set(context_ids or [])
        filtered = [pid for pid in req.pinned_ids if pid not in lookback_id_set]
        if filtered:
            pinned_context = get_messages_by_ids(filtered)
            if pinned_context:
                if req.adhoc:
                    # Adhoc: prepend to context_history for _build_prompt
                    context_history = pinned_context + context_history
                else:
                    # Session: prepend as referenced_context block in the prompt
                    lines = ["Relevant context from other sessions:\n<referenced_context>"]
                    for msg in pinned_context:
                        role = "User" if msg["role"] == "user" else "Assistant"
                        lines.append(f"{role}: {msg['content'].strip()}")
                    lines.append("</referenced_context>\n")
                    prefix_blocks.append("\n".join(lines))

    if prefix_blocks:
        effective_message = "\n\n".join(prefix_blocks + [req.message])

    stored_context_ids = list({*(context_ids or []), *(req.pinned_ids or [])}) or None
    user_msg_id = insert_user_message(topic, resolved_agent, req.message,
                                      context_ids=stored_context_ids, mem=req.include_topic_memory)
    asst_msg_id = insert_assistant_message(topic, resolved_agent, user_msg_id, adhoc=req.adhoc)

    log.info(
        "chat  topic=%s  agent=%s  backend=%s  model=%s  adhoc=%s  resume=%s  ctx=%d  pinned=%d  memory=%s  msg=%.80r",
        topic, resolved_agent, backend, model, req.adhoc,
        bool(resume_session_id), len(context_history) // 2,
        len(req.pinned_ids) if req.pinned_ids else 0, req.include_topic_memory, req.message,
    )
    await maybe_sync()
    return StreamingResponse(
        stream_response(
            effective_message, topic, resolved_agent, backend, model, cwd,
            context_history, asst_msg_id, response_timeout,
            resume_session_id=resume_session_id,
            adhoc=req.adhoc,
            lookback=lookback,
            code_roots=tracking_roots,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/cmd")
async def run_cmd(req: CmdRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic

    if req.command == "stop_msg":
        killed = kill_proc_by_msg_id(req.msg_id) if req.msg_id else 0
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stop":
        killed = dispatcher.stop_topic(topic, agent=req.agent, adhoc=req.adhoc)
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stopall":
        result = dispatcher.stopall_topic(topic, agent=req.agent, adhoc=req.adhoc)
        return JSONResponse({"ok": True, **result})
    if req.command == "deq":
        drained = dispatcher.drain_topic(topic, req.pos)
        return JSONResponse({"ok": True, "drained": drained})
    if req.command == "list":
        return JSONResponse({"ok": True, "topics": get_topics_summary()})
    if req.command == "restart":
        async def _restart():
            await asyncio.sleep(0.4)
            kill_all_procs()
            if "--reload" in sys.argv:
                Path(__file__).touch()
            else:
                os.execv(sys.executable, [sys.executable, "-m", "agent.server"])
        asyncio.create_task(_restart())
        return JSONResponse({"ok": True})

    if req.command in ("clear", "compact"):
        agent = req.agent or get_active_agent_for_topic(topic)
        if not agent:
            topic_row = get_topic(topic)
            agent = topic_row.get("agent") if topic_row else None
        if not agent:
            return JSONResponse({"ok": False, "error": "no active session"}, status_code=400)
        kill_procs_by_topic(topic, agent=agent, adhoc=False)
        clear_topic_session(topic, agent)
        return JSONResponse({"ok": True, "agent": agent})

    if req.command == "journal":
        week_key, week_start, week_end = _current_week()
        path = await _generate_journal(topic, req.agent, week_key, week_start, week_end)
        if path:
            return JSONResponse({"ok": True, "file": str(path)})
        return JSONResponse({"ok": False, "error": "generation failed or no turns"}, status_code=500)

    return JSONResponse({"ok": False, "error": "unknown command"}, status_code=400)


@app.get("/processes")
async def processes():
    return JSONResponse(list_active_procs())


@app.get("/queue")
async def queued():
    return JSONResponse(dispatcher.all_queued_items())


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "boot_time": BOOT_TIME,
        "backends": {
            "claude":      {"available": bool(CLAUDE_PATH),   "path": CLAUDE_PATH},
            "cursor":      {"available": bool(CURSOR_PATH),   "path": CURSOR_PATH},
            "antigravity": {"available": bool(AGY_PATH),      "path": AGY_PATH},
            "codex":       {"available": bool(CODEX_PATH),    "path": CODEX_PATH},
            "copilot":     {"available": bool(COPILOT_PATH),  "path": COPILOT_PATH},
        },
    })


@app.get("/history")
async def history(offset: int = 0, limit: int = 5, topic: Optional[str] = None,
                  agent: Optional[str] = None, adhoc: Optional[bool] = None):
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    return JSONResponse(list_history(topic=topic, agent=agent, adhoc=adhoc, offset=offset, limit=limit))


@app.get("/chat/{msg_id}/status")
async def message_status(msg_id: int):
    row = get_message(msg_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.get("/topics")
async def topics_list():
    db_topics = get_topics_summary()
    queue_map = {t["name"]: t for t in dispatcher.topics_info()}
    for t in db_topics:
        info = queue_map.get(t["name"], {})
        t["queue_depth"] = info.get("queue_depth", 0)
        t["active"] = info.get("active", False)
    return JSONResponse(db_topics)


@app.get("/topics/manage")
async def topics_manage(include_hidden: bool = True):
    db_topics = get_topics_management_summary(include_hidden=include_hidden)
    queue_map = {t["name"]: t for t in dispatcher.topics_info()}
    for t in db_topics:
        info = queue_map.get(t["name"], {})
        t["queue_depth"] = info.get("queue_depth", 0)
        t["active"] = info.get("active", False)
        mem_path = topic_memory_path(t["name"])
        try:
            mem_display = str(mem_path.relative_to(mem_path.parent.parent.parent.parent))
        except ValueError:
            mem_display = str(mem_path)
        t["memory"] = {
            "exists": mem_path.exists(),
            "path": mem_display,
        }
    return JSONResponse(db_topics)


@app.get("/topics/{topic}/agents/history")
async def topic_agent_history(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(get_topic_agent_history(topic))


@app.get("/topics/{topic}/memory")
async def get_topic_memory_route(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(read_topic_memory(topic))


@app.put("/topics/{topic}/memory")
async def put_topic_memory_route(topic: str, req: TopicMemoryRequest):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(write_topic_memory(topic, req.content))


@app.put("/topics/{topic}/memory/squid/code-roots")
async def put_topic_memory_code_roots_route(topic: str, req: TopicMemoryCodeRootsRequest):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(write_topic_memory_squid_code_roots(
        topic,
        code_roots=req.code_roots,
        code_roots_skipped=req.code_roots_skipped,
    ))


@app.post("/topics/{topic}/hide")
async def hide_topic_route(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse({"ok": hide_topic(topic)})


@app.put("/topics/{topic}/hidden")
async def set_topic_hidden_route(topic: str, req: TopicHiddenRequest):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse({"ok": set_topic_hidden(topic, req.hidden), "hidden": req.hidden})


@app.delete("/topics/{topic}")
async def remove_topic(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    deleted = delete_topic(topic)
    return JSONResponse({"ok": deleted})


@app.get("/topics/{topic}/sessions")
async def list_topic_sessions(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    agents = get_topic_agents(topic)
    return JSONResponse({"agents": agents})


@app.get("/topics/{topic}/session")
async def get_session(topic: str, agent: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    stored = get_topic_session(topic, agent)
    if not stored:
        return JSONResponse({"session_id": None, "cwd": None})
    injected_ids = get_session_injected_ids(stored["session_id"])
    return JSONResponse({"session_id": stored["session_id"], "cwd": stored["cwd"],
                         "injected_ids": injected_ids})


@app.delete("/topics/{topic}/session")
async def clear_session(topic: str, agent: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    clear_topic_session(topic, agent)
    return JSONResponse({"ok": True})


@app.get("/context/{topic}")
async def context_view(topic: str, agent: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    stored = get_topic_session(topic, agent)
    return JSONResponse({
        "session_id": stored["session_id"] if stored else None,
        "cwd": stored["cwd"] if stored else None,
    })


@app.get("/config/agents/{name}/sessions")
async def agent_sessions(name: str):
    """Return all active topic sessions for a named agent."""
    return JSONResponse({"topics": get_agent_sessions(name)})


@app.get("/config/agents")
async def get_agents():
    return JSONResponse(list_agents())


@app.post("/config/agents")
async def create_agent(req: AgentRequest):
    key_changed = upsert_agent(req.name, req.backend, req.model, req.cwd, req.timeout)
    sessions_cleared = clear_agent_sessions(req.name) if key_changed else []
    return JSONResponse({"ok": True, "sessions_cleared": sessions_cleared})


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


class MsgQuotaSnapshotRequest(BaseModel):
    before: float
    after: float


class RevertRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    file_path: Optional[str] = None


@app.post("/chat/{msg_id}/quota-delta")
async def record_msg_quota_delta(msg_id: int, req: MsgQuotaSnapshotRequest):
    update_message_quota_snapshot(msg_id, req.before, req.after)
    return JSONResponse({"ok": True})


@app.get("/chat/{msg_id}/diff-revert-status")
async def diff_revert_status(msg_id: int, repo: str):
    eligibility = await asyncio.to_thread(get_diff_revert_eligibility, msg_id, repo)
    if not eligibility:
        return JSONResponse({"error": "diff not found"}, status_code=404)
    return JSONResponse(eligibility)


@app.post("/chat/{msg_id}/revert")
async def revert_diff(msg_id: int, req: RevertRequest):
    from .git_changes import extract_file_diff, apply_reverse_patch

    eligibility = await asyncio.to_thread(get_diff_revert_eligibility, msg_id, req.repo)
    if not eligibility:
        return JSONResponse({"error": "diff not found"}, status_code=404)

    if req.file_path:
        status = eligibility.get(req.file_path)
        if status != 'revertable':
            return JSONResponse(
                {"error": f"{req.file_path!r} is {status or 'not in diff'}"},
                status_code=400,
            )
        files_to_revert = [req.file_path]
    else:
        files_to_revert = [f for f, s in eligibility.items() if s == 'revertable']

    if not files_to_revert:
        return JSONResponse({"error": "no revertable files"}, status_code=400)

    this_diff = await asyncio.to_thread(get_message_gitdiff, msg_id, req.repo)
    if not this_diff:
        return JSONResponse({"error": "GitDiff not found"}, status_code=404)

    full_diff = this_diff.get('diff', '')
    repo_root = Path(req.repo)

    reverted: list[str] = []
    failed: list[dict] = []

    for fpath in files_to_revert:
        file_diff = extract_file_diff(full_diff, fpath)
        if not file_diff:
            failed.append({'file': fpath, 'error': 'no diff text found'})
            continue
        ok, err = await asyncio.to_thread(apply_reverse_patch, repo_root, file_diff)
        if ok:
            reverted.append(fpath)
        else:
            failed.append({'file': fpath, 'error': err})

    if reverted:
        await asyncio.to_thread(record_git_diff_revert, msg_id, req.repo, reverted)

    return JSONResponse({"ok": True, "reverted": reverted, "failed": failed})


@app.post("/config/creds")
async def save_creds(req: CredsRequest):
    creds.save(req.org_id.strip(), req.session_key.strip())
    return JSONResponse({"ok": True})


@app.post("/config/creds/codex")
async def save_codex_creds(req: CodexCredsRequest):
    creds.save_codex(req.token.strip())
    return JSONResponse({"ok": True})


@app.get("/quota")
@app.get("/quota/claude")
async def quota_claude():
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


@app.get("/quota/codex")
async def quota_codex():
    token = creds.get_codex_token()
    if not token:
        return JSONResponse({"error": "credentials not configured"}, status_code=400)
    authorization = _codex_bearer_header(token)
    if not authorization:
        return JSONResponse({"error": "Codex bearer token required"}, status_code=400)
    try:
        from curl_cffi.requests import AsyncSession
        common_headers = {
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        async with AsyncSession() as session:
            r = await session.get(
                "https://chatgpt.com/backend-api/wham/usage",
                headers={**common_headers, "Authorization": authorization},
                impersonate="chrome",
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"wham/usage returned {r.status_code}"}, status_code=502)
        return JSONResponse(r.json())
    except Exception as exc:
        log.error("codex quota fetch failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/journals/{topic}")
async def list_journals(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(list_topic_journals(topic))


@app.get("/journals/{topic}/{week}")
async def get_journal(topic: str, week: str, agent: Optional[str] = None):
    from fastapi.responses import PlainTextResponse
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    content = read_journal(topic, week, agent=agent)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return PlainTextResponse(content, media_type="text/markdown")


@app.get("/remote")
async def get_remote_url():
    """Return the Tailscale HTTPS URL for remote access QR generation."""
    import subprocess
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return JSONResponse({"url": None, "reason": "not_running"})
        data = json.loads(result.stdout)
        dns = data.get("Self", {}).get("DNSName", "").rstrip(".")
        if dns:
            return JSONResponse({"url": f"https://{dns}/"})
        return JSONResponse({"url": None, "reason": "no_dns"})
    except FileNotFoundError:
        return JSONResponse({"url": None, "reason": "not_installed"})
    except Exception:
        return JSONResponse({"url": None, "reason": "error"})


_LOCALFILE_ROOTS: list[Path] = [
    Path(r).expanduser().resolve()
    for r in ((_cfg.get("server") or {}).get("localfile_roots") or [])
]

@app.get("/localfile")
async def serve_local_file(path: str):
    """Serve a local file — only paths under server.localfile_roots are allowed."""
    import mimetypes
    from fastapi.responses import FileResponse, PlainTextResponse
    if not _LOCALFILE_ROOTS:
        return JSONResponse({"error": "localfile not enabled (set server.localfile_roots in squid.yaml)"}, status_code=403)
    p = Path(path).expanduser().resolve()
    if not any(p.is_relative_to(root) for root in _LOCALFILE_ROOTS):
        return JSONResponse({"error": "path outside allowed roots"}, status_code=403)
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not p.is_file():
        return JSONResponse({"error": "not a file"}, status_code=400)
    mime, _ = mimetypes.guess_type(str(p))
    if mime and mime.startswith("text/"):
        return PlainTextResponse(p.read_text(errors="replace"), media_type=mime)
    return FileResponse(str(p), media_type=mime or "application/octet-stream")


if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")


def main():
    import ipaddress
    import uvicorn

    host = _cfg["server"]["host"]
    port = _cfg["server"]["port"]

    # Only loopback (127.0.0.0/8) is permitted. squid must never bind directly
    # to a network interface — use `tailscale serve` to expose it on your mesh.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        sys.exit(f"ERROR: server.host must be an IP address, got: {host!r}")
    if ip not in ipaddress.ip_network("127.0.0.0/8"):
        sys.exit(
            f"ERROR: server.host {host!r} is not a loopback address.\n"
            "squid must bind to 127.0.0.1 (or another 127.x.x.x address).\n"
            "For remote access via Tailscale, use:\n"
            f"  tailscale serve --bg --http={port} 127.0.0.1:{port}"
        )

    print(f"Starting squid on http://{host}:{port}")
    uvicorn.run(
        "agent.server:app",
        host=host,
        port=port,
        reload="--reload" in sys.argv,
    )


if __name__ == "__main__":
    main()
