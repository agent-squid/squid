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
import logging.handlers
import os
import re
import subprocess
import sys
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional, Union

import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (
    CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH,
    OPENCODE_PATH, SQUID_HOME, RESPONSE_TIMEOUT, WORKTREE_ISOLATION_ENABLED,
    _USER_CONFIG, _cfg,
    config_revision, config_text, write_config_text,
)
from .harnesses import SUPPORTED_HARNESSES, _validate_harness_config, list_harnesses
from .providers import Provider, _validate_provider, get_provider, public_providers, require_provider
from .resolve import agent_ref_for_storage, resolve_agent, split_agent_ref
from .runners import list_active_procs, kill_all_procs, kill_procs_by_topic, kill_proc_by_msg_id, get_active_agent_for_topic
from .history import list_history, list_history_by_ids
from .stats_db import get_usage_stats
from .topic_queue import TopicDispatcher
from .context_sync import sync_now, maybe_sync
from .topics import normalize_topic_slug
from .memory import (
    _split_frontmatter,
    code_roots_prompt_block,
    read_topic_memory,
    topic_memory_path,
    topic_memory_squid_config,
    write_topic_memory_squid_code_roots,
    write_topic_memory,
)
from .stats_db import (
    init_db, get_aggregated_stats, save_quota_delta, get_stats_by_topic, get_stats_by_agent,
    get_stats_by_agent_breakdown, get_stats_by_breakdown, get_stats_by_turn, get_stats_filter_options,
    list_stats_filter_presets, create_stats_filter_preset, update_stats_filter_preset,
    delete_stats_filter_preset,
    get_topics_summary, get_topics_management_summary,
    get_agent, upsert_agent, delete_agent, list_agents, get_default_agent,
    get_topic, upsert_topic, list_topics,
    insert_user_message, insert_assistant_message, update_assistant_message,
    attach_assistant_session, mark_assistant_cancelled,
    update_message_quota_snapshot,
    get_context_history, get_messages_by_ids, mark_orphaned_pending, get_message,
    get_message_previews, get_flow_run_messages,
    get_completed_run_text, get_completed_run_status_raw, get_run_events,
    ensure_session_turn_index,
    get_session_injected_context,
    get_topic_session, clear_topic_session,
    delete_topic, delete_topic_agent, set_topic_hidden, get_topic_agents, get_topic_agent_history,
    clear_agent_sessions, get_agent_sessions,
    get_diff_revert_eligibility, record_git_diff_revert, get_message_gitdiff,
    search_messages, search_prompts,
    get_recent_prompts,
    save_file_edit, get_file_edit_history, get_file_edit_by_id,
    get_bookmarks, add_bookmark, remove_bookmark,
    save_worktree, get_worktrees, delete_all_worktrees,
    delete_all_topic_worktrees,
    allocate_id,
)
from .journal import _generate_journal, _current_week, list_topic_journals, read_journal
from . import creds

BOOT_TIME = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
SQUID_VERSION = _pkg_version("squid")

init_db()

# Rotate daily, keep a week of history — the log previously grew unbounded
# (shell-appended by bin/start.sh with no timestamps, no cap).
_LOG_DIR = _USER_CONFIG.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_handler = logging.handlers.TimedRotatingFileHandler(
    _LOG_DIR / "server.log", when="midnight", backupCount=7, encoding="utf-8", utc=True,
)
_log_handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler], force=True)
log = logging.getLogger(__name__)


class _AccessLogNoiseFilter(logging.Filter):
    """Drop routine polling GETs (health checks, /queue, /processes, static
    assets, etc.) from the access log — they fire every second or two per
    open browser tab and bury the entries that actually matter. POSTs and
    any non-2xx/3xx response still get through."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client_addr, method, _path, _http_version, status_code = args
        if method != "GET":
            return True
        try:
            return int(status_code) >= 400
        except (TypeError, ValueError):
            return True


logging.getLogger("uvicorn.access").addFilter(_AccessLogNoiseFilter())

dispatcher = TopicDispatcher()

# Discard the legacy cached OAuth access token if this process inherited one
# from an older Squid restart.
os.environ.pop("SQUID_NATIVE_CLAUDE_TOKEN", None)

_SQUID_CHAT_COMMANDS = frozenset({
    "clear", "deq", "f", "filter", "help", "remote", "restart",
    "s", "search", "status", "stop", "stopall",
})

# ---------------------------------------------------------------------------
# App + health check helpers
# ---------------------------------------------------------------------------

def _claude_logged_in() -> bool:
    if not CLAUDE_PATH:
        return False
    # Let Claude Code inspect its own credential store. Remove inherited gateway
    # credentials so the result reflects the native claude.ai login.
    try:
        env = os.environ.copy()
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            env.pop(name, None)
        result = subprocess.run(
            [CLAUDE_PATH, "auth", "status"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn"))
    except Exception:
        return False


def _check_deps():
    missing, warnings = [], []
    for harness in list_harnesses():
        if not harness["installed"]:
            missing.append(f"{harness['id']:<12}  →  {harness['install_cmd']}")
        elif harness["id"] == "claudecode" and not _claude_logged_in():
            warnings.append("claudecode is installed but not logged in  →  run: claude login")
    if missing:
        log.warning("Missing CLI tools:\n  " + "\n  ".join(missing))
    if warnings:
        log.warning("Auth issues:\n  " + "\n  ".join(warnings))
    if not missing and not warnings:
        log.info("all harnesses installed: %s", ", ".join(sorted(SUPPORTED_HARNESSES)))


def _backend_native_chat_command_name(message: str) -> str:
    text = message.strip()
    if not text.startswith("/"):
        return ""
    return text[1:].split(None, 1)[0].lower()


def _is_backend_native_chat_command(message: str) -> bool:
    command_name = _backend_native_chat_command_name(message)
    return bool(command_name and command_name not in _SQUID_CHAT_COMMANDS)


def _resolve_agent_runtime(agent_config: dict) -> tuple[str, Optional[str], str, object]:
    harness, provider = split_agent_ref(agent_config.get("harness"), agent_config.get("provider"))
    resolved = resolve_agent(harness, provider)
    return harness, provider, agent_ref_for_storage(harness, provider), resolved


def _public_agent_config(agent_config: dict) -> dict:
    item = dict(agent_config)
    try:
        harness, provider, backend_ref, resolved = _resolve_agent_runtime(item)
        item.update({
            "harness": harness,
            "provider": provider,
            "color": resolved.color,
            "provider_color": resolved.provider.color,
            "provider_label": resolved.provider.label,
        })
    except Exception:
        pass
    return item


def _public_agent_map() -> dict:
    return {agent["name"]: agent for agent in (_public_agent_config(a) for a in list_agents()) if agent.get("name")}


_check_deps()
sync_now()

app = FastAPI(title="Squid", version=SQUID_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _recover_orphaned_pending_on_startup():
    orphaned = mark_orphaned_pending(before_created_at=BOOT_TIME)
    if orphaned:
        log.warning("Marked %d orphaned pending messages as error", orphaned)


@app.on_event("startup")
async def _resume_stalled_flows_on_startup():
    from .flow import sweep_incomplete_flows
    resumed = await sweep_incomplete_flows()
    if resumed:
        log.warning("Resumed %d stalled Squid Flow chain(s) on startup", resumed)


UI_DIR = Path(__file__).parent.parent / "ui"

# Static file extensions served by the UI — exempt from bearer-token auth so
# the page loads before the browser has a token in localStorage.
_STATIC_EXTS = {".js", ".css", ".html", ".ico", ".png", ".svg",
                ".woff", ".woff2", ".ttf", ".map", ".json", ".webmanifest"}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    topic: str = Field("default")
    agent: Optional[str] = None
    route: Optional[str] = None
    source: str = Field("human")
    lookback: int = Field(0)
    lookback_via_pins: bool = Field(False)
    adhoc: bool = Field(False)
    pinned_ids: Optional[list[int]] = None
    include_topic_memory: bool = Field(False)
    flow_run_id: Optional[str] = None
    flow_route: Optional[str] = None


class TopicMemoryRequest(BaseModel):
    content: str = ""


class TopicMemoryCodeRootsRequest(BaseModel):
    code_roots: Optional[list[str]] = None
    code_roots_skipped: bool = False


class TopicHiddenRequest(BaseModel):
    hidden: bool = Field(False)


class AgentRequest(BaseModel):
    name: str = Field(..., min_length=1)
    harness: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None


class ConfigRequest(BaseModel):
    content: str = Field(..., min_length=1)
    revision: Optional[str] = None


class LocalfileWriteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str


class LocalfileCreateRequest(BaseModel):
    parent: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)


class LocalfileRenameRequest(BaseModel):
    path: str = Field(..., min_length=1)
    name: Optional[str] = Field(default=None, min_length=1)
    to_path: Optional[str] = Field(default=None, min_length=1)


class LocalfileDeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)


class LocalfileRevertEditRequest(BaseModel):
    edit_id: int


class CredsRequest(BaseModel):
    claude_org_id: str = Field(..., min_length=1)
    claude_session_key: str = Field(..., min_length=1)


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
    command: Literal["stop", "stopall", "deq", "list", "restart", "clear", "stop_msg", "journal"]
    topic: str = "default"
    agent: Optional[str] = None
    adhoc: Optional[bool] = None
    pos: Optional[int] = None
    msg_id: Optional[int] = None


def _validate_config_content(content: str) -> dict:
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("configuration must be a YAML mapping")

    server_cfg = parsed.get("server")
    if not isinstance(server_cfg, dict):
        raise ValueError("server must be a mapping")
    if server_cfg.get("host") != "127.0.0.1":
        raise ValueError("server.host must remain 127.0.0.1")
    port = server_cfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("server.port must be an integer from 1 to 65535")
    roots = server_cfg.get("localfile_roots") or []
    if not isinstance(roots, list) or not all(isinstance(root, str) and root for root in roots):
        raise ValueError("server.localfile_roots must be a list of paths")

    agent_cfg = parsed.get("agent")
    if not isinstance(agent_cfg, dict):
        raise ValueError("agent must be a mapping")
    for field in ("first_byte_timeout", "response_timeout"):
        value = agent_cfg.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"agent.{field} must be a positive integer")

    _validate_harness_config(parsed.get("harnesses"))

    providers = parsed.get("providers")
    if providers is not None:
        if not isinstance(providers, dict) or not providers:
            raise ValueError("providers must be a non-empty mapping")
        for provider_id, raw in providers.items():
            _validate_provider(provider_id, raw)
    return parsed


def _origin_candidates(request: Request) -> set[str]:
    candidates = {str(request.base_url).rstrip("/")}
    host = request.headers.get("host")
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if host:
        candidates.add(f"{request.url.scheme}://{host}".rstrip("/"))
    if forwarded_proto and host:
        candidates.add(f"{forwarded_proto}://{host}".rstrip("/"))
    if forwarded_host:
        candidates.add(f"{forwarded_proto or request.url.scheme}://{forwarded_host}".rstrip("/"))
    return candidates


def _same_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site == "cross-site":
        return False
    if fetch_site == "same-origin":
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin.rstrip("/") in _origin_candidates(request)


def _safe_browser_navigation(request: Request) -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    fetch_mode = request.headers.get("sec-fetch-mode")
    fetch_dest = request.headers.get("sec-fetch-dest")
    if fetch_site not in {"same-origin", "same-site", "cross-site", "none"}:
        return False
    if fetch_site != "cross-site":
        return fetch_mode in {None, "navigate"} and fetch_dest in {None, "document"}
    return fetch_mode == "navigate" and fetch_dest == "document"


_LOCALFILE_DOCUMENT_PREVIEW_SUFFIXES = {
    ".html", ".htm", ".svg", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
}


def _safe_document_preview_request(request: Request, *, render: bool, path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        if not render:
            return False
    elif suffix not in _LOCALFILE_DOCUMENT_PREVIEW_SUFFIXES:
        return False
    if request.method not in {"GET", "HEAD"}:
        return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    return "text/html" in request.headers.get("accept", "")


def _localfile_roots_from(config: dict) -> list[Path]:
    configured = [
        Path(root).expanduser().resolve()
        for root in ((config.get("server") or {}).get("localfile_roots") or [])
    ]
    # Squid's own state is intentionally visible to its single-user web UI.
    squid_state = _USER_CONFIG.parent.resolve()
    return list(dict.fromkeys([squid_state, *configured]))


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

# Idle handling for the /chat SSE loop. Poll out_q on a short timeout so we can
# emit periodic keepalive comments during long silent gaps (Opus extended
# thinking, long tool runs) — keeps proxies/tunnels/mobile links from dropping
# the stream and forcing the client into "recovering" polling.
_OUT_Q_POLL_TIMEOUT = 1.0  # seconds
_HEARTBEAT_TICKS = 10      # emit a heartbeat after this many idle poll timeouts (~10s)


def sse_chunk(data: str) -> str:
    return "data:" + data.replace("\n", "\ndata:") + "\n\n"

def sse_event(event: str, data: str = "") -> str:
    # SSE represents multiline payloads as repeated data fields. Preserve the
    # original line structure instead of emitting bare continuation lines.
    normalized = data.replace("\r\n", "\n").replace("\r", "\n")
    encoded = normalized.replace("\n", "\ndata: ")
    return f"event: {event}\ndata: {encoded}\n\n"


def _normalize_topic_response(topic: str) -> Union[str, JSONResponse]:
    try:
        return normalize_topic_slug(topic)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def canonical_flow_route(route: Optional[str]) -> Optional[str]:
    text = re.sub(r"\s+", "", (route or "").strip())
    if not text:
        return None
    parts = [part for part in text.split(",") if part]
    if len(parts) > 1:
        parts.sort()
    return ",".join(parts)


async def _prepare_chat_turn(
    *,
    message: str,
    topic: str,
    agent: Optional[str],
    adhoc: bool = False,
    lookback: int = 0,
    lookback_via_pins: bool = False,
    pinned_ids: Optional[list[int]] = None,
    include_topic_memory: bool = False,
    source: str = "human",
    flow_run_id: Optional[str] = None,
    flow_route: Optional[str] = None,
) -> Union[dict, JSONResponse]:
    # 1. Resolve agent — explicit wins, else use topic sticky
    resolved_agent: Optional[str] = agent
    agent_config: dict = {}

    if agent:
        agent_config = get_agent(agent) or {}
        if not agent_config:
            return JSONResponse(
                {"error": f"Agent '{agent}' not found. Create it first via /config/agents."},
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

    try:
        harness, provider, backend, resolved_runtime = _resolve_agent_runtime(agent_config)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    model: Optional[str] = agent_config.get("model") or None
    native_backend_command = _is_backend_native_chat_command(message)

    upsert_topic(topic, resolved_agent, last_prompt=message,
                 last_harness=harness, last_provider=provider, last_model=model, adhoc=adhoc)
    agent_cwd: Optional[str] = agent_config.get("cwd") or None
    response_timeout: int = RESPONSE_TIMEOUT

    # 2. Resumable session lookup (skipped for adhoc turns)
    resume_session_id: Optional[str] = None
    cwd: Optional[str] = agent_cwd

    if not adhoc and resolved_agent:
        stored = get_topic_session(topic, resolved_agent)
        if stored:
            stored_fingerprint = stored.get("runtime_fingerprint")
            if stored_fingerprint and stored_fingerprint != resolved_runtime.fingerprint:
                clear_topic_session(topic, resolved_agent)
                log.info("cleared session after agent runtime change: topic=%s agent=%s backend=%s", topic, resolved_agent, backend)
            else:
                resume_session_id = stored["session_id"]
                cwd = stored["cwd"]

    # 3. Context history for adhoc turns
    lookback = int(lookback) if lookback else 0
    context_history: list[dict] = []
    context_ids: Optional[list[int]] = None

    if not native_backend_command and adhoc and lookback > 0 and not lookback_via_pins:
        context_history, context_ids = get_context_history(
            topic, lookback, agent=resolved_agent
        )

    memory_config = topic_memory_squid_config(topic)
    code_roots = memory_config.get("code_roots") or []
    source_cwd = cwd

    memory_revision: Optional[str] = None
    memory_data_for_prompt: Optional[dict] = None
    if not native_backend_command and include_topic_memory:
        memory_data_for_prompt = read_topic_memory(topic)
        if memory_data_for_prompt["content"].strip():
            memory_revision = memory_data_for_prompt.get("revision")

    stored_context_ids = list({*(context_ids or []), *(pinned_ids or [])}) or None
    user_msg_id = insert_user_message(topic, resolved_agent, message,
                                      context_ids=stored_context_ids,
                                      mem=bool(memory_revision),
                                      mem_revision=memory_revision,
                                      lookback=lookback,
                                      source=source,
                                      flow_run_id=flow_run_id,
                                      flow_route=flow_route)
    asst_msg_id = insert_assistant_message(topic, resolved_agent, user_msg_id, adhoc=adhoc, flow_run_id=flow_run_id, flow_route=flow_route)
    attach_assistant_session(asst_msg_id, resume_session_id)

    effective_code_roots = code_roots
    effective_cwd = cwd
    worktree_isolated = False
    if WORKTREE_ISOLATION_ENABLED and resolved_agent and code_roots:
        effective_code_roots, effective_cwd, worktree_isolated = await _setup_worktrees(
            code_roots, cwd, topic, str(asst_msg_id)
        )

    effective_message = message
    prefix_blocks: list[str] = []
    if not native_backend_command and effective_code_roots:
        code_roots_block = code_roots_prompt_block(effective_code_roots, isolated=worktree_isolated)
        if code_roots_block:
            prefix_blocks.append(code_roots_block)
    tracking_roots: list[str] = [] if native_backend_command else effective_code_roots

    if memory_data_for_prompt:
        memory_content = memory_data_for_prompt["content"].strip()
        if memory_content:
            prefix_blocks.append("\n".join([
                "Persistent user-editable topic memory:",
                f'<topic_memory topic="{memory_data_for_prompt["topic"]}">',
                memory_content,
                "</topic_memory>",
            ]))

    if not native_backend_command and pinned_ids:
        lookback_id_set = set(context_ids or [])
        filtered = [pid for pid in pinned_ids if pid not in lookback_id_set]
        if filtered:
            pinned_context = get_messages_by_ids(filtered)
            if pinned_context:
                if adhoc:
                    context_history = pinned_context + context_history
                else:
                    lines = ["Relevant context from other sessions:\n<referenced_context>"]
                    for msg in pinned_context:
                        role = "User" if msg["role"] == "user" else "Assistant"
                        lines.append(f"{role}: {msg['content'].strip()}")
                    lines.append("</referenced_context>\n")
                    prefix_blocks.append("\n".join(lines))

    if prefix_blocks:
        effective_message = "\n\n".join(prefix_blocks + [message])

    log.info(
        "chat  topic=%s  agent=%s  harness=%s  provider=%s  backend=%s  model=%s  adhoc=%s  resume=%s  ctx=%d  pinned=%d  memory=%s  msg=%.80r",
        topic, resolved_agent, harness, provider, backend, model, adhoc,
        bool(resume_session_id), len(context_history) // 2,
        len(pinned_ids) if pinned_ids else 0, include_topic_memory, message,
    )
    return {
        "effective_message": effective_message,
        "topic": topic,
        "agent": resolved_agent,
        "backend": backend,
        "model": model,
        "cwd": effective_cwd,
        "context_history": context_history,
        "asst_msg_id": asst_msg_id,
        "response_timeout": response_timeout,
        "resume_session_id": resume_session_id,
        "adhoc": adhoc,
        "lookback": lookback,
        "code_roots": tracking_roots,
        "display_prompt": message,
        "source_cwd": source_cwd,
        "harness": harness,
        "provider": provider,
    }

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
    drain_timeout: Optional[int] = None,
) -> None:
    """Drain the worker queue after client disconnect; save final content to DB."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + float(drain_timeout if drain_timeout is not None else RESPONSE_TIMEOUT)
    tool_events = list(tool_events or [])
    timed_out = False
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                log.warning("drain timeout msg_id=%s, saving partial", msg_id)
                timed_out = True
                break
            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=min(left, 30.0))
            except asyncio.TimeoutError:
                continue
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

    content = raw
    context_json = json.dumps(tool_events) if tool_events else None
    try:
        status = "pending" if timed_out else ("done" if content else "error")
        update_assistant_message(msg_id, content, session_id, status, context=context_json, status_raw=status_raw, only_if_pending=True)
        log.info("drain complete msg_id=%s len=%d tools=%d sid=%s", msg_id, len(content), len(tool_events), session_id)
    except Exception:
        log.exception("drain save failed msg_id=%s", msg_id)

# ---------------------------------------------------------------------------
# Worktree helpers
# ---------------------------------------------------------------------------

async def _setup_worktrees(
    code_roots: list[str],
    cwd: Optional[str],
    topic: str,
    agent: str,
) -> tuple[list[str], Optional[str], bool]:
    """
    Create worktrees for each unique git repo in code_roots and remap paths.
    Returns (effective_code_roots, cwd, isolated). CWD is never remapped — it
    stays as the source repo path for session continuity (see ADR-0003). The
    model uses effective_code_roots (worktree-remapped absolute paths) for all
    file access. Falls back to original paths on any error, with isolated=False.
    """
    from .worktree import repo_root_for, ensure_worktree, map_to_worktree, branch_name

    worktree_map: dict[Path, Path] = {}
    for root in code_roots:
        try:
            repo_root = await asyncio.to_thread(repo_root_for, root)
            if repo_root and repo_root not in worktree_map:
                wt_path = await asyncio.to_thread(ensure_worktree, repo_root, topic, agent)
                worktree_map[repo_root] = wt_path
                await asyncio.to_thread(
                    save_worktree, topic, agent,
                    str(repo_root), str(wt_path), branch_name(topic, agent),
                )
        except Exception:
            log.exception("worktree setup failed for root=%s topic=%s agent=%s", root, topic, agent)

    if not worktree_map:
        return code_roots, cwd, False

    effective_roots = [map_to_worktree(r, worktree_map) or r for r in code_roots]
    return effective_roots, cwd, True


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
    display_prompt: Optional[str] = None,
    source_cwd: Optional[str] = None,
    harness: Optional[str] = None,
    provider: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    yield sse_event("meta", json.dumps({
        "agent": agent,
        "harness": harness,
        "provider": provider,
        "model": model,
        "msg_id": asst_msg_id,
        "adhoc": adhoc,
    }))

    effective_cwd = cwd or SQUID_HOME
    dispatch_harness, dispatch_provider = split_agent_ref(harness or backend, provider)
    out_q, seq, worker = await dispatcher.dispatch(
        topic=topic, prompt=message, context_history=context_history,
        harness=dispatch_harness, provider=dispatch_provider,
        model=model, agent=agent, cwd=effective_cwd,
        source_cwd=source_cwd,
        response_timeout=response_timeout,
        resume_session_id=resume_session_id,
        adhoc=adhoc, lookback=lookback, msg_id=asst_msg_id,
        code_roots=code_roots,
        display_prompt=display_prompt,
    )

    raw = ""
    status_raw = ""
    tool_events: list[dict] = []
    session_id: Optional[str] = None
    last_partial_save = time.monotonic()
    _completed = False
    idle_ticks = 0

    try:
        while True:
            position = worker.position_of(seq)
            if position > 0:
                yield sse_event("queued", json.dumps({"topic": topic, "position": position}))

            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=_OUT_Q_POLL_TIMEOUT)
            except asyncio.TimeoutError:
                idle_ticks += 1
                if idle_ticks >= _HEARTBEAT_TICKS:
                    idle_ticks = 0
                    yield ": ping\n\n"
                continue
            idle_ticks = 0

            if chunk is None:
                break

            if isinstance(chunk, dict) and "_error" in chunk:
                err_text = chunk["_error"]
                if raw:
                    context_json = json.dumps(tool_events) if tool_events else None
                    update_assistant_message(asst_msg_id, raw, session_id, "done", context=context_json, status_raw=status_raw, only_if_pending=True)
                    yield sse_event("done")
                else:
                    yield sse_event("error", err_text)
                    update_assistant_message(asst_msg_id, err_text, session_id, "error", status_raw=status_raw, only_if_pending=True)
                _completed = True
                return

            if isinstance(chunk, dict) and "_stats" in chunk:
                stats = dict(chunk["_stats"])
                session_id = stats.get("session_id")
                if session_id and not adhoc:
                    stats["session_turn_count"] = ensure_session_turn_index(asst_msg_id, session_id)
                yield sse_event("stats", json.dumps(stats))

            elif isinstance(chunk, dict) and "_tool" in chunk:
                tool_events.append(chunk["_tool"])
                yield sse_event("tool", json.dumps(chunk["_tool"]))

            elif isinstance(chunk, dict) and "_status" in chunk:
                status_raw += chunk["_status"]
                text = chunk["_status"]
                if text:
                    yield sse_event("status", text)

            else:
                raw += chunk
                yield sse_chunk(chunk)
                now = time.monotonic()
                if raw and now - last_partial_save >= 0.5:
                    context_json = json.dumps(tool_events) if tool_events else None
                    update_assistant_message(asst_msg_id, raw, session_id, "pending", context=context_json, status_raw=status_raw)
                    last_partial_save = now

            await asyncio.sleep(0)

        context_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, raw, session_id, "done", context=context_json, status_raw=status_raw, only_if_pending=True)
        yield sse_event("done")
        _completed = True

    except Exception as exc:
        log.exception("Unexpected error in stream_response")
        err_text = f"Internal error: {exc}"
        yield sse_event("error", err_text)
        context_json = json.dumps(tool_events) if tool_events else None
        update_assistant_message(asst_msg_id, err_text, session_id, "error", context=context_json, status_raw=status_raw)
        _completed = True

    finally:
        if not _completed:
            try:
                update_assistant_message(asst_msg_id, raw, session_id, "pending", status_raw=status_raw, only_if_pending=True)
            except Exception:
                pass
            asyncio.create_task(
                _drain_to_completion(
                    out_q, asst_msg_id, raw, status_raw, session_id, tool_events,
                    topic=topic, agent=agent, backend=backend, model=model,
                    cwd=effective_cwd, adhoc=adhoc, lookback=lookback,
                    drain_timeout=response_timeout,
                ),
                name=f"squid-drain-{asst_msg_id}",
            )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    if req.route:
        return JSONResponse({"error": "route chains are executed as explicit UI turns"}, status_code=400)

    flow_route = canonical_flow_route(req.flow_route)
    flow_run_id = req.flow_run_id if flow_route else None
    if flow_route and not flow_run_id:
        flow_run_id = allocate_id("flow_run")

    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic

    # Only the origin step of a Squid Flow route chain (ADR-0032) is ever
    # posted by a client. Every step after that — the target handoff, and the
    # return handoff for a "<>" round — is dispatched by agent/flow.py once
    # the prior step finishes, entirely server-side (see continue_chain()).
    prepared = await _prepare_chat_turn(
        message=req.message,
        topic=topic,
        agent=req.agent,
        adhoc=req.adhoc,
        lookback=req.lookback,
        lookback_via_pins=req.lookback_via_pins,
        pinned_ids=req.pinned_ids,
        include_topic_memory=req.include_topic_memory,
        source=req.source,
        flow_run_id=flow_run_id,
        flow_route=flow_route,
    )
    if isinstance(prepared, JSONResponse):
        return prepared
    await maybe_sync()
    return StreamingResponse(
        stream_response(
            prepared["effective_message"], prepared["topic"], prepared["agent"], prepared["backend"], prepared["model"], prepared["cwd"],
            prepared["context_history"], prepared["asst_msg_id"], prepared["response_timeout"],
            resume_session_id=prepared["resume_session_id"],
            adhoc=prepared["adhoc"],
            lookback=prepared["lookback"],
            code_roots=prepared["code_roots"],
            display_prompt=prepared["display_prompt"],
            source_cwd=prepared["source_cwd"],
            harness=prepared["harness"],
            provider=prepared["provider"],
        ),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "X-Squid-Msg-Id": str(prepared["asst_msg_id"]),
            "X-Squid-Flow-Run-Id": flow_run_id or "",
        },
    )


@app.post("/cmd")
async def run_cmd(req: CmdRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic

    if req.command == "stop_msg":
        if req.msg_id:
            mark_assistant_cancelled(req.msg_id, "Cancelled")
        killed = kill_proc_by_msg_id(req.msg_id) if req.msg_id else 0
        log.info("cmd stop_msg topic=%s msg_id=%s killed=%s", req.topic, req.msg_id, killed)
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stop":
        killed = dispatcher.stop_topic(topic, agent=req.agent, adhoc=req.adhoc)
        log.info("cmd stop topic=%s agent=%s adhoc=%s killed=%s", topic, req.agent, req.adhoc, killed)
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stopall":
        result = dispatcher.stopall_topic(topic, agent=req.agent, adhoc=req.adhoc)
        log.info(
            "cmd stopall topic=%s agent=%s adhoc=%s killed=%s drained=%s",
            topic,
            req.agent,
            req.adhoc,
            result.get("killed"),
            result.get("drained"),
        )
        return JSONResponse({"ok": True, **result})
    if req.command == "deq":
        drained = dispatcher.drain_topic(topic, req.pos)
        log.info("cmd deq topic=%s pos=%s drained=%s", topic, req.pos, drained)
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

    if req.command == "clear":
        agent = req.agent or get_active_agent_for_topic(topic)
        if not agent:
            topic_row = get_topic(topic)
            agent = topic_row.get("agent") if topic_row else None
        if not agent:
            return JSONResponse({"ok": False, "error": "no active session"}, status_code=400)
        killed = kill_procs_by_topic(topic, agent=agent, adhoc=False)
        from .worktree import cleanup_worktrees
        conflicts = await cleanup_worktrees(topic)
        clear_topic_session(topic, agent)
        log.info("cmd %s topic=%s agent=%s killed=%s", req.command, topic, agent, killed)
        result: dict = {"ok": True, "agent": agent}
        if conflicts:
            result["worktree_conflicts"] = conflicts
        return JSONResponse(result)

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


def _gauge_authed(gauge_type: str, provider: Provider) -> Optional[bool]:
    """Whether the account behind a gauge is authenticated — a provider fact
    (credentials/API key), never a harness one (ADR-0028)."""
    if gauge_type == "claude":
        return bool(creds.get_org_id() and creds.get_session_key())
    if gauge_type == "codex":
        return bool(creds.get_codex_token())
    if gauge_type == "cursor":
        return bool(creds.get_cursor_token())
    if gauge_type == "deepseek":
        try:
            return bool(provider.resolved_api_key())
        except ValueError:
            return False
    if gauge_type == "static":
        return True
    return None


@app.get("/health")
async def health():
    providers = public_providers()
    for provider_id, info in providers.items():
        info["gauge_authed"] = _gauge_authed(info["gauge"]["type"], require_provider(provider_id))
    return JSONResponse({
        "status": "ok",
        "boot_time": BOOT_TIME,
        "version": SQUID_VERSION,
        "squid_home": SQUID_HOME,
        "harnesses": list_harnesses(),
        "providers": providers,
        **get_usage_stats(),
    })


@app.get("/history")
async def history(offset: int = 0, limit: int = 5, topic: Optional[str] = None,
                  agent: Optional[str] = None, adhoc: Optional[bool] = None,
                  flow_route: Optional[str] = None):
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    return JSONResponse(list_history(topic=topic, agent=agent, adhoc=adhoc, offset=offset, limit=limit, flow_route=canonical_flow_route(flow_route)))


@app.get("/history/by-ids")
async def history_by_ids(ids: str = ""):
    if not ids.strip():
        return JSONResponse({"items": [], "total": 0})
    try:
        parsed = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"error": "invalid ids"}, status_code=400)
    parsed = parsed[:200]  # cap to prevent abuse
    return JSONResponse(list_history_by_ids(parsed))


@app.get("/search")
async def search(q: str, limit: int = 100, topic: Optional[str] = None,
                 agent: Optional[str] = None, adhoc: Optional[bool] = None,
                 role: str = "assistant", bookmarked: bool = False,
                 flow_route: Optional[str] = None):
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    limit = min(limit, 100)
    flow_route = canonical_flow_route(flow_route)
    if role == "user":
        return JSONResponse(search_prompts(q=q, topic=topic, agent=agent, adhoc=adhoc, limit=limit, bookmarked=bookmarked, flow_route=flow_route))
    return JSONResponse(search_messages(q=q, topic=topic, agent=agent, adhoc=adhoc, limit=limit, bookmarked=bookmarked, flow_route=flow_route))


@app.get("/prompts/recent")
async def prompts_recent(limit: int = 50):
    limit = min(limit, 200)
    return JSONResponse({"items": get_recent_prompts(limit=limit), "agents": _public_agent_map()})


@app.get("/chat/previews")
async def chat_previews(ids: str):
    parsed_ids = []
    seen = set()
    for raw in ids.split(","):
        try:
            msg_id = int(raw)
        except ValueError:
            continue
        if msg_id > 0 and msg_id not in seen:
            parsed_ids.append(msg_id)
            seen.add(msg_id)
        if len(parsed_ids) >= 200:
            break
    return JSONResponse({"items": get_message_previews(parsed_ids)})


@app.get("/chat/{msg_id}/status")
async def message_status(msg_id: int):
    row = get_message(msg_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    recovered_content = get_completed_run_text(msg_id)
    if (
        row.get("status") == "pending"
        and row.get("role") == "assistant"
        and recovered_content
    ):
        recovered_status_raw = row.get("status_raw") or get_completed_run_status_raw(msg_id)
        update_assistant_message(
            msg_id,
            recovered_content,
            row.get("session_id"),
            "done",
            context=row.get("context"),
            status_raw=recovered_status_raw,
            only_if_pending=True,
        )
        row = get_message(msg_id)
    return JSONResponse(row)


@app.get("/chat/{msg_id}/events")
async def message_events(msg_id: int, after_seq: int = -1):
    row = get_message(msg_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def event_stream() -> AsyncGenerator[str, None]:
        last_seq = after_seq
        while True:
            events = get_run_events(msg_id, last_seq)
            for event in events:
                last_seq = event["seq"]
                event_type = event["event_type"]
                payload = event["payload"] or ""
                if event_type == "text":
                    yield sse_chunk(payload)
                elif event_type in {"stats", "status", "tool"}:
                    yield sse_event(event_type, payload)
                elif event_type == "done":
                    yield sse_event("done")
                    return
                elif event_type == "error":
                    current = get_message(msg_id)
                    if current and current.get("status") == "done":
                        yield sse_event("done")
                        return
                    yield sse_event("error", payload)
                    return

            current = get_message(msg_id)
            if not current:
                yield sse_event("error", "Message not found")
                return
            if current.get("status") == "done":
                yield sse_event("done")
                return
            if current.get("status") == "cancelled":
                yield sse_event("error", current.get("content") or "Cancelled")
                return
            if current.get("status") == "error":
                yield sse_event("error", current.get("content") or "Response interrupted.")
                return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/chat/flow/{flow_run_id}/steps")
async def flow_run_steps(flow_run_id: str, after_id: int = 0):
    """Poll target for a Squid Flow route chain (ADR-0032): steps now run
    server-side (agent/flow.py) without any client request, so a client
    watching an in-progress chain polls this to discover new steps and
    attach to them via /chat/{msg_id}/events, same as a step it sent itself."""
    from .flow import parse_route_chain
    rows = get_flow_run_messages(flow_run_id)
    new_rows = [r for r in rows if r["id"] > after_id]

    chain = parse_route_chain(rows[0]["flow_route"]) if rows else None
    expected_rows = (6 if chain["operator"] == "<>" else 4) if chain else 0
    last_status = rows[-1]["status"] if rows else None
    complete = (
        not rows
        or last_status in ("error", "cancelled")
        or (not chain)
        or len(rows) >= expected_rows
    )

    return JSONResponse({
        "messages": [
            {
                "id": r["id"],
                "role": r["role"],
                "topic": r["topic"],
                "agent": r["agent"],
                "status": r["status"],
            }
            for r in new_rows
        ],
        "complete": complete,
    })


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
            mem_display = "~/.squid/" + str(mem_path.relative_to(Path.home() / ".squid"))
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
    injected_context = get_session_injected_context(stored["session_id"])
    return JSONResponse({"session_id": stored["session_id"], "cwd": stored["cwd"],
                         **injected_context})


@app.delete("/topics/{topic}/agent")
async def remove_topic_agent(topic: str, agent: str, adhoc: Optional[bool] = None):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    delete_topic_agent(topic, agent, adhoc=adhoc)
    return JSONResponse({"ok": True})


@app.delete("/topics/{topic}/session")
async def clear_session(topic: str, agent: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    from .worktree import cleanup_worktrees
    conflicts = await cleanup_worktrees(topic)
    clear_topic_session(topic, agent)
    result: dict = {"ok": True}
    if conflicts:
        result["worktree_conflicts"] = conflicts
    return JSONResponse(result)


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


@app.get("/config/yaml")
async def get_config_yaml(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration reads are not allowed"}, status_code=403)
    content = config_text()
    return JSONResponse({
        "content": content,
        "revision": config_revision(content),
        "path": str(_USER_CONFIG),
    })


@app.put("/config/yaml")
async def update_config_yaml(req: ConfigRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration writes are not allowed"}, status_code=403)
    try:
        parsed = _validate_config_content(req.content)
        revision = write_config_text(req.content, req.revision)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _cfg.clear()
    _cfg.update(parsed)
    _LOCALFILE_ROOTS[:] = _localfile_roots_from(parsed)
    return JSONResponse({
        "ok": True,
        "revision": revision,
        "restart_required": True,
        "backup": str(_USER_CONFIG.with_suffix(_USER_CONFIG.suffix + ".bak")),
    })


@app.get("/config/localfile-roots")
async def get_localfile_roots(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration reads are not allowed"}, status_code=403)
    return JSONResponse({"roots": [str(root) for root in _LOCALFILE_ROOTS]})


@app.get("/config/agents")
async def get_agents():
    return JSONResponse([_public_agent_config(agent) for agent in list_agents()])


@app.post("/config/agents")
async def create_agent(req: AgentRequest):
    if not req.harness:
        return JSONResponse({"error": "harness is required"}, status_code=400)
    harness, provider = req.harness, req.provider
    try:
        resolve_agent(harness, provider)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    key_changed = upsert_agent(req.name, harness, provider, req.model, req.cwd)
    sessions_cleared = clear_agent_sessions(req.name) if key_changed else []
    return JSONResponse({"ok": True, "sessions_cleared": sessions_cleared})


@app.delete("/config/agents/{name}")
async def remove_agent(name: str):
    deleted = delete_agent(name)
    return JSONResponse({"ok": deleted})


class StatsFilterPresetRequest(BaseModel):
    name: Optional[str] = None
    state: Optional[dict] = None
    is_default: Optional[bool] = None


@app.get("/stats/filters")
async def stats_filter_options():
    return JSONResponse(get_stats_filter_options())


@app.get("/stats/filter-presets")
async def stats_filter_presets():
    return JSONResponse(list_stats_filter_presets())


@app.post("/stats/filter-presets")
async def create_stats_preset(req: StatsFilterPresetRequest):
    if not req.name or req.state is None:
        return JSONResponse({"error": "name and state are required"}, status_code=400)
    try:
        return JSONResponse(create_stats_filter_preset(req.name, req.state))
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            return JSONResponse({"error": f'A view named "{req.name}" already exists.'}, status_code=400)
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put("/stats/filter-presets/{preset_id}")
async def update_stats_preset(preset_id: int, req: StatsFilterPresetRequest):
    try:
        preset = update_stats_filter_preset(preset_id, req.name, req.state, req.is_default)
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            return JSONResponse({"error": f'A view named "{req.name}" already exists.'}, status_code=400)
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not preset:
        return JSONResponse({"error": "preset not found"}, status_code=404)
    return JSONResponse(preset)


@app.delete("/stats/filter-presets/{preset_id}")
async def delete_stats_preset(preset_id: int):
    return JSONResponse({"ok": delete_stats_filter_preset(preset_id)})


def _parse_chart_series(chart_metrics: str, chart_aggs: str) -> list[dict]:
    metrics = [m for m in chart_metrics.split(",") if m]
    aggs = chart_aggs.split(",") if chart_aggs else []
    return [
        {"metric": metric, "agg": aggs[i] if i < len(aggs) else "sum"}
        for i, metric in enumerate(metrics)
    ]


@app.get("/stats")
async def usage_stats(
    period: str = "daily",
    group: str = "time",
    breakdown: str = "",
    days: int = 30,
    hours: int = 0,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    tz_offset_minutes: int = 0,
    chart_metrics: str = "",
    chart_aggs: str = "",
    anchor: str = "",
):
    chart_series = _parse_chart_series(chart_metrics, chart_aggs)
    if period == "turn":
        return JSONResponse(get_stats_by_turn(days=days, hours=hours, agent=agent, topic=topic, adhoc=adhoc, anchor=anchor or None))
    if group == "time" and breakdown in {"agent", "agent_session", "topic_agent", "topic_agent_session"}:
        return JSONResponse(get_stats_by_breakdown(
            period=period,
            days=days,
            agent=agent,
            topic=topic,
            adhoc=adhoc,
            tz_offset_minutes=tz_offset_minutes,
            breakdown=breakdown,
            # Breakdown series are dimension values (topic/agent), not metrics —
            # only the first requested metric is meaningful here.
            chart_series=chart_series[:1],
            anchor=anchor or None,
        ))
    if group == "topic":
        return JSONResponse(get_stats_by_topic(days=days, agent=agent, topic=topic, adhoc=adhoc, anchor=anchor or None))
    if group == "agent":
        return JSONResponse(get_stats_by_agent(days=days, agent=agent, topic=topic, adhoc=adhoc, anchor=anchor or None))
    return JSONResponse(get_aggregated_stats(
        period=period,
        days=days,
        agent=agent,
        topic=topic,
        adhoc=adhoc,
        tz_offset_minutes=tz_offset_minutes,
        chart_series=chart_series,
        anchor=anchor or None,
    ))


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
    if _validate_repo_path(repo) is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)
    eligibility = await asyncio.to_thread(get_diff_revert_eligibility, msg_id, repo)
    if not eligibility:
        return JSONResponse({"error": "diff not found"}, status_code=404)
    return JSONResponse(eligibility)


def _validate_repo_path(repo: str) -> Optional[Path]:
    """Return resolved Path if repo is an absolute path to a real git repo, else None."""
    try:
        p = Path(repo).resolve()
    except Exception:
        return None
    if not p.is_absolute() or not (p / ".git").exists():
        return None
    return p


@app.post("/chat/{msg_id}/revert")
async def revert_diff(msg_id: int, req: RevertRequest):
    from .git_changes import extract_file_diff, apply_reverse_patch

    repo_root = _validate_repo_path(req.repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

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


@app.get("/bookmarks")
async def list_bookmarks():
    return JSONResponse({"items": get_bookmarks()})


@app.post("/bookmarks")
async def create_bookmark(request: Request):
    body = await request.json()
    msg_id = body.get("msg_id")
    if not isinstance(msg_id, int):
        return JSONResponse({"error": "msg_id required"}, status_code=400)
    add_bookmark(msg_id, body.get("topic"), body.get("agent"), body.get("content"))
    return JSONResponse({"ok": True})


@app.delete("/bookmarks/{msg_id}")
async def delete_bookmark(msg_id: int):
    remove_bookmark(msg_id)
    return JSONResponse({"ok": True})


@app.post("/config/creds")
async def save_creds(req: CredsRequest):
    creds.save(req.claude_org_id.strip(), req.claude_session_key.strip())
    return JSONResponse({"ok": True})


@app.post("/config/creds/auto")
async def auto_detect_creds():
    try:
        found = await asyncio.to_thread(creds.read_chrome_claude_creds)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    session_key = found.get("sessionKey")
    org_id = found.get("lastActiveOrg")
    if not session_key or not org_id:
        missing = [k for k, v in {"sessionKey": session_key, "lastActiveOrg": org_id}.items() if not v]
        return JSONResponse({"error": f"Cookies not found: {', '.join(missing)}. Make sure you are logged into claude.ai in Chrome."}, status_code=404)
    creds.save(org_id, session_key)
    return JSONResponse({"ok": True, "claude_org_id": org_id})


@app.post("/config/creds/codex/auto")
async def auto_detect_codex_creds():
    try:
        token = await asyncio.to_thread(creds.read_codex_creds)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    creds.save_codex(token)
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


@app.get("/quota/cursor")
async def quota_cursor():
    token = creds.get_cursor_token()
    if not token:
        return JSONResponse({"error": "Cursor not logged in — run cursor-agent to authenticate"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api2.cursor.sh/auth/usage-summary",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"cursor.sh returned {r.status_code}"}, status_code=502)
        return JSONResponse(r.json())
    except Exception as exc:
        log.error("cursor quota fetch failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/quota/deepseek")
async def quota_deepseek():
    provider = get_provider("deepseek")
    try:
        deepseek_key = provider.resolved_api_key() if provider else None
    except ValueError:
        deepseek_key = None
    if not deepseek_key:
        return JSONResponse({"error": "deepseek key not configured"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {deepseek_key}"},
                timeout=10,
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"DeepSeek returned {r.status_code}"}, status_code=502)
        return JSONResponse(r.json())
    except Exception as exc:
        log.error("deepseek balance fetch failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/config/deepseek/max-budget")
async def set_deepseek_max_budget(body: dict):
    amount = body.get("amount")
    if not amount or amount <= 0:
        return JSONResponse({"error": "invalid amount"}, status_code=400)
    creds.save_deepseek_max_budget(amount)
    return JSONResponse({"status": "ok"})


@app.delete("/config/deepseek/max-budget")
async def clear_deepseek_max_budget():
    creds.clear_deepseek_max_budget()
    return JSONResponse({"status": "ok"})


def _json_response_data(response: JSONResponse) -> dict:
    try:
        return json.loads(response.body)
    except (TypeError, json.JSONDecodeError):
        return {}


async def _quota_snapshot_for_provider(provider: Provider, ref: str) -> JSONResponse:
    """Return a normalized gauge snapshot for one provider. Quota is a provider
    attribute (ADR-0028) — this never needs to know which harness is using it."""
    gauge = provider.gauge
    if gauge.type == "none":
        return JSONResponse({"status": "none"})
    if gauge.type == "static":
        return JSONResponse({
            "status": "static", "text": gauge.text, "title": gauge.title,
            "used_percent": None, "reset_at": None,
        })

    if gauge.type == "deepseek":
        try:
            api_key = provider.resolved_api_key()
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not api_key:
            return JSONResponse({"error": "api_key not configured"}, status_code=400)
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": f"Bearer {api_key}"}, timeout=10,
                )
            if response.status_code != 200:
                return JSONResponse({"error": f"DeepSeek returned {response.status_code}"}, status_code=502)
            data = response.json()
        except Exception as exc:
            log.error("deepseek balance fetch failed for %s: %s", ref, exc)
            return JSONResponse({"error": str(exc)}, status_code=502)
        balances = data.get("balance_infos") or []
        info = next((item for item in balances if item.get("currency") == "USD"), None)
        info = info or next((item for item in balances if item.get("currency") == "CNY"), None)
        if not info:
            return JSONResponse({"error": "balance unavailable"}, status_code=502)
        symbol = "$" if info.get("currency") == "USD" else "¥"
        balance = float(info.get("total_balance") or 0)
        max_budget = creds.get_deepseek_max_budget()
        if max_budget and max_budget > 0:
            spent = max(0, max_budget - balance)
            pct = max(0, min(100, round(spent / max_budget * 100)))
            return JSONResponse({
                "status": "ok", "text": f"{symbol}{balance:.2f}",
                "raw": balance, "used_percent": None, "reset_at": None,
                "title": f"DeepSeek · {symbol}{spent:.2f} spent of {symbol}{max_budget:.2f}",
                "max_budget": max_budget, "max_budget_pct": pct, "spent": spent,
            })
        return JSONResponse({
            "status": "ok", "text": f"{symbol}{balance:.2f}",
            "raw": balance, "used_percent": None, "reset_at": None,
            "title": f"DeepSeek balance · {symbol}{balance:.2f}",
        })

    raw_response = await {
        "claude": quota_claude,
        "codex": quota_codex,
        "cursor": quota_cursor,
    }[gauge.type]()
    if raw_response.status_code >= 400:
        return raw_response
    data = _json_response_data(raw_response)
    if gauge.type == "claude":
        window = data.get("five_hour") or {}
        used = window.get("utilization")
        sd = data.get("seven_day") or {}
        return JSONResponse({
            "status": "ok", "text": f"{round(used)}%" if used is not None else None,
            "raw": used, "used_percent": used, "reset_at": window.get("resets_at"),
            "title": "Claude session usage",
            "seven_day": {
                "used_percent": sd.get("utilization"),
                "reset_at": sd.get("resets_at"),
            } if sd else None,
        })
    if gauge.type == "codex":
        rate_limit = data.get("rate_limit") or {}
        window = rate_limit.get("primary_window") or {}
        used = window.get("used_percent")
        reset_at = window.get("reset_at")
        if reset_at is None and window.get("reset_after_seconds") is not None:
            reset_at = time.time() + window["reset_after_seconds"]
        secondary = rate_limit.get("secondary_window") or {}
        return JSONResponse({
            "status": "ok", "text": f"{round(used)}%" if used is not None else None,
            "raw": used, "used_percent": used, "reset_at": reset_at,
            "title": "Codex usage",
            "seven_day": {
                "used_percent": secondary.get("used_percent"),
                "reset_at": secondary.get("reset_at"),
            } if secondary else None,
        })
    if data.get("isUnlimited"):
        return JSONResponse({
            "status": "static", "text": "Unlimited", "used_percent": None,
            "reset_at": None, "title": "Cursor unlimited",
        })
    plan = (data.get("individualUsage") or {}).get("plan") or {}
    used = plan.get("totalPercentUsed")
    return JSONResponse({
        "status": "ok", "text": f"{round(used)}%" if used is not None else None,
        "raw": used, "used_percent": used, "reset_at": data.get("billingCycleEnd"),
        "title": data.get("autoModelSelectedDisplayMessage") or "Cursor usage",
    })


@app.get("/quota/provider/{provider_id}")
async def quota_provider(provider_id: str):
    """Return a normalized gauge snapshot for one configured provider, with no
    harness involved — quota is an account fact, not tied to any particular CLI."""
    provider = get_provider(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Unknown provider {provider_id!r}"}, status_code=404)
    return await _quota_snapshot_for_provider(provider, provider_id)


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


_LOCALFILE_ROOTS: list[Path] = _localfile_roots_from(_cfg)
_LOCALFILE_TEXT_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}

def _localfile_child(parent: Path, name: str) -> Union[Path, JSONResponse]:
    if not name or name in {".", ".."} or Path(name).name != name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    child = (parent / name).resolve()
    if child.parent != parent:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    return child

def _looks_like_text_file(path: Path, sample_size: int = 65536) -> bool:
    try:
        sample = path.read_bytes()[:sample_size]
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text:
        return True
    bad = sum(1 for ch in text if ch not in "\n\r\t" and (ord(ch) < 32 or ord(ch) == 127))
    return bad / len(text) <= 0.01

@app.get("/localfile")
async def serve_local_file(path: str, request: Request, render: bool = False):
    """Serve a local file — any path readable by the server's OS user is allowed."""
    import mimetypes
    from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
    _nocache = {"Cache-Control": "no-store"}
    p = Path(path).expanduser().resolve()
    if (
        not _same_origin(request)
        and not _safe_browser_navigation(request)
        and not _safe_document_preview_request(request, render=render, path=p)
    ):
        return JSONResponse({"error": "cross-origin file reads are not allowed"}, status_code=403)
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if p.is_dir():
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        entry_list = []
        for e in entries:
            try:
                st = e.stat()
                size = st.st_size if not e.is_dir() else None
                mtime = st.st_mtime
            except OSError:
                size = None
                mtime = None
            entry_list.append({"name": e.name, "path": str(e), "is_dir": e.is_dir(), "size": size, "mtime": mtime})
        return JSONResponse({"type": "directory", "path": str(p), "entries": entry_list}, headers=_nocache)
    if not p.is_file():
        return JSONResponse({"error": "not a file"}, status_code=400)
    if render and p.suffix.lower() in (".md", ".markdown"):
        raw_content = p.read_text(errors="replace")
        _frontmatter, markdown_body = _split_frontmatter(raw_content)
        md_content = json.dumps(markdown_body).replace("</", "<\\/")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:860px;margin:40px auto;padding:0 20px;line-height:1.6;color:#24292e}}pre{{background:#f6f8fa;padding:16px;border-radius:6px;overflow:auto}}code{{background:#f6f8fa;padding:.2em .4em;border-radius:3px}}a{{color:#0366d6}}</style>
</head><body><div id="out"></div>
<script src="/vendor/marked.min.js"></script>
<script>marked.setOptions({{gfm:true,breaks:true}});document.getElementById('out').innerHTML=marked.parse({md_content});</script>
</body></html>"""
        return HTMLResponse(html, headers=_nocache)
    mime = _LOCALFILE_TEXT_MIME_BY_SUFFIX.get(p.suffix.lower())
    if mime is None:
        mime, _ = mimetypes.guess_type(str(p))
    if mime and mime.startswith("text/"):
        return PlainTextResponse(p.read_text(errors="replace"), media_type=mime, headers=_nocache)
    if (not mime or mime == "application/octet-stream") and _looks_like_text_file(p):
        return PlainTextResponse(p.read_text(errors="replace"), media_type="text/plain", headers=_nocache)
    return FileResponse(str(p), media_type=mime or "application/octet-stream", headers=_nocache)


@app.post("/localfile")
async def write_local_file(req: LocalfileWriteRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    p = Path(req.path).expanduser().resolve()
    if not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    before = p.read_text(errors="replace")
    p.write_text(req.content)
    edit_id = await asyncio.to_thread(save_file_edit, str(p), before, req.content)
    return JSONResponse({"ok": True, "edit_id": edit_id})


@app.post("/localfile/create-file")
async def create_local_file(req: LocalfileCreateRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    parent = Path(req.parent).expanduser().resolve()
    if not parent.is_dir():
        return JSONResponse({"error": "parent is not a directory"}, status_code=400)
    child = _localfile_child(parent, req.name.strip())
    if isinstance(child, JSONResponse):
        return child
    if child.exists():
        return JSONResponse({"error": "path already exists"}, status_code=409)
    child.write_text("")
    return JSONResponse({"ok": True, "path": str(child)})


@app.post("/localfile/create-folder")
async def create_local_folder(req: LocalfileCreateRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    parent = Path(req.parent).expanduser().resolve()
    if not parent.is_dir():
        return JSONResponse({"error": "parent is not a directory"}, status_code=400)
    child = _localfile_child(parent, req.name.strip())
    if isinstance(child, JSONResponse):
        return child
    if child.exists():
        return JSONResponse({"error": "path already exists"}, status_code=409)
    child.mkdir()
    return JSONResponse({"ok": True, "path": str(child)})


@app.post("/localfile/upload")
async def upload_local_file(parent: str, name: str, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    parent_path = Path(parent).expanduser().resolve()
    if not parent_path.is_dir():
        return JSONResponse({"error": "parent is not a directory"}, status_code=400)
    child = _localfile_child(parent_path, name.strip())
    if isinstance(child, JSONResponse):
        return child
    if child.exists():
        return JSONResponse({"error": "path already exists"}, status_code=409)
    child.write_bytes(await request.body())
    return JSONResponse({"ok": True, "path": str(child)})


@app.post("/localfile/rename")
async def rename_local_path(req: LocalfileRenameRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    p = Path(req.path).expanduser().resolve()
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if req.to_path:
        target = Path(req.to_path).expanduser().resolve()
        if not target.parent.is_dir():
            return JSONResponse({"error": "target parent is not a directory"}, status_code=400)
    elif req.name:
        target = _localfile_child(p.parent, req.name.strip())
        if isinstance(target, JSONResponse):
            return target
    else:
        return JSONResponse({"error": "name or to_path is required"}, status_code=400)
    if target == p:
        return JSONResponse({"ok": True, "path": str(target)})
    if target.exists():
        return JSONResponse({"error": "path already exists"}, status_code=409)
    p.rename(target)
    return JSONResponse({"ok": True, "path": str(target)})


@app.post("/localfile/delete")
async def delete_local_file(req: LocalfileDeleteRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    p = Path(req.path).expanduser().resolve()
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not p.is_file():
        return JSONResponse({"error": "not a file"}, status_code=400)
    p.unlink()
    return JSONResponse({"ok": True, "path": str(p)})


@app.get("/localfile/history")
async def local_file_history(path: str, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin reads are not allowed"}, status_code=403)
    p = Path(path).expanduser().resolve()
    rows = await asyncio.to_thread(get_file_edit_history, str(p))
    return JSONResponse({"history": rows})


@app.post("/localfile/revert-edit")
async def revert_file_edit(req: LocalfileRevertEditRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
    edit = await asyncio.to_thread(get_file_edit_by_id, req.edit_id)
    if not edit:
        return JSONResponse({"error": "edit not found"}, status_code=404)
    p = Path(edit["file_path"]).resolve()
    before = p.read_text(errors="replace")
    p.write_text(edit["before"])
    await asyncio.to_thread(save_file_edit, str(p), before, edit["before"])
    return JSONResponse({"ok": True})


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

    log.info("Starting squid on http://%s:%s", host, port)
    uvicorn.run(
        "agent.server:app",
        host=host,
        port=port,
        reload="--reload" in sys.argv,
        # Skip uvicorn's own logging config so its loggers (with no handlers
        # of their own) propagate into our rotating file handler above,
        # instead of writing to stdout unrotated.
        log_config=None,
    )


if __name__ == "__main__":
    main()
