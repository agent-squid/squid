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
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Literal, Optional, Union

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .features import shore_enabled
from .auth_sessions import AUTH_SESSION_MODES
from .config import (
    CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH,
    OPENCODE_PATH, SQUID_HOME, RESPONSE_TIMEOUT, NATIVE_SHELL_TIMEOUT, WORKTREE_ISOLATION_ENABLED,
    REALTIME_TRANSPORT, UPDATES_INSTALL_ON_RESTART,
    REALTIME_OUTBOUND_QUEUE_LIMIT, REALTIME_MAX_FRAME_BYTES, REALTIME_HEARTBEAT_SECONDS,
    _USER_CONFIG, _cfg,
    config_revision, config_text, realtime_transport, shore_identity_dir, write_config_text,
)
from .harnesses import SUPPORTED_HARNESSES, _validate_harness_config, list_harnesses
from .providers import Provider, _validate_provider, get_provider, reload_providers, require_provider
from . import topic_provider_policy as provider_policy
from .resolve import agent_ref_for_storage, remove_pi_models_store, resolve_agent, split_agent_ref
from .runners import list_active_procs, kill_all_procs, kill_procs_by_topic, kill_proc_by_msg_id, get_active_agent_for_topic, set_process_change_listener
from .history import list_history, list_history_by_ids, list_history_around
from .stats_db import get_usage_stats
from .topic_queue import TopicDispatcher
from .context_sync import sync_now, maybe_sync
from .topics import normalize_topic_slug
from .memory import (
    _split_frontmatter,
    ensure_topic_memory_placeholder,
    read_topic_memory,
    topic_memory_path,
    topic_memory_squid_config,
    write_topic_memory_squid_code_roots,
    write_topic_memory,
)
from .stats_db import (
    init_db, database_lifetime, get_aggregated_stats, save_quota_delta, get_stats, get_stats_by_topic, get_stats_by_agent,
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
    get_completed_run_text, get_completed_run_status_raw, get_run_events, get_run_event_snapshot,
    ensure_session_turn_index, get_session_turn_boundaries,
    get_session_injected_context, get_session_turn_count,
    get_topic_session, clear_topic_session,
    delete_topic, delete_topic_agent, set_topic_hidden, get_topic_agents, get_topic_agent_history,
    clear_agent_sessions, get_agent_sessions, get_agent_home_mode, set_agent_home_mode,
    get_diff_revert_eligibility, record_git_diff_revert, get_message_gitdiff,
    search_messages, search_prompts,
    get_recent_prompt_entries, get_composer_history, get_composer_route_entries, update_composer_history,
    save_file_edit, get_file_edit_history, get_file_edit_by_id,
    get_bookmarks, add_bookmark, remove_bookmark,
    get_message_annotations, set_message_annotation, remove_message_annotation,
    get_worktrees, get_all_worktrees_for_topic,
    mark_worktree_status, delete_worktree, delete_all_worktrees,
    delete_all_topic_worktrees,
    allocate_id,
    get_realtime_cursor, get_realtime_snapshot,
    get_realtime_request, save_realtime_request, get_realtime_replay,
    insert_run_event, insert_realtime_event, prune_realtime_data, set_realtime_commit_listener,
    create_flow_run, get_flow_run, get_flow_steps, claim_flow_step,
    link_flow_step_messages, transition_flow_step, cancel_flow_run,
    _utc_now_iso,
)
from .journal import _generate_journal, _current_week, list_topic_journals, read_journal
from . import creds
from . import codex_subscription, sandbox_home

BOOT_TIME = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
try:
    SQUID_VERSION = _pkg_version("agentsquid")
except PackageNotFoundError:
    SQUID_VERSION = "0+local"

init_db()

# Rotate daily, keep a week of history — the log previously grew unbounded
# (shell-appended by bin/start.sh with no timestamps, no cap).
# SQUID_LOG_DIR lets the test suite keep its logs out of the real server.log.
_LOG_DIR = Path(os.environ.get("SQUID_LOG_DIR") or _USER_CONFIG.parent / "logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_handler = logging.handlers.TimedRotatingFileHandler(
    _LOG_DIR / "server.log", when="midnight", backupCount=7, encoding="utf-8", utc=True,
)
_log_formatter = logging.Formatter("%(asctime)sZ %(levelname)s  %(message)s")
_log_formatter.converter = time.gmtime  # the "Z" suffix and midnight rotation are UTC
_log_handler.setFormatter(_log_formatter)
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

def _publish_process_changed(processes: list[dict]) -> None:
    try:
        insert_realtime_event("process.changed", None, None, {"processes": processes})
    except Exception:
        # Process bookkeeping is authoritative. A transient realtime-log
        # failure must not turn a successful spawn/state change into a runner
        # failure; snapshots and later events repair client state.
        log.exception("Failed to publish realtime process state")


def _publish_queue_changed(queue: list[dict]) -> None:
    try:
        insert_realtime_event("queue.changed", None, None, {"queue": queue})
    except Exception:
        # The item is already enqueued/dequeued when this callback runs. Keep
        # domain behavior intact and let a later event or snapshot reconcile.
        log.exception("Failed to publish realtime queue state")


dispatcher = TopicDispatcher()

# Set by _lifespan; None whenever Shore login hasn't been completed on this
# host or the connection failed to start. Read by the local-only /shore/*
# pairing/device endpoints below — never by anything reachable remotely.
_shore_connection: Any = None

# Discard the legacy cached OAuth access token if this process inherited one
# from an older Squid restart.
os.environ.pop("SQUID_NATIVE_CLAUDE_TOKEN", None)

_SQUID_CHAT_COMMANDS = frozenset({
    "clear", "deq", "f", "filter", "help", "pair", "remote", "restart",
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


async def _active_worktree_blockers(topic: str, code_roots: list[str]) -> list[dict]:
    repo_roots = {str(root) for root in await _repo_roots_for_code_roots(code_roots)}
    if not repo_roots:
        return []

    blockers = []
    for rec in await asyncio.to_thread(get_all_worktrees_for_topic, topic):
        if rec.get("repo_root") not in repo_roots:
            continue
        if rec.get("status") in {"conflict", "promotion_failed"}:
            blockers.append({
                "repo_root": rec.get("repo_root"),
                "worktree_path": rec.get("worktree_path"),
                "integration_worktree_path": rec.get("integration_worktree_path"),
                "status": rec.get("status"),
                "msg_id": rec.get("agent"),
            })
    return blockers


async def _repo_roots_for_code_roots(code_roots: list[str]) -> list[Path]:
    from .worktree import repo_root_for

    repo_roots: list[Path] = []
    seen: set[str] = set()
    for root in code_roots:
        repo_root = await asyncio.to_thread(repo_root_for, root)
        if not repo_root:
            continue
        key = str(repo_root)
        if key in seen:
            continue
        seen.add(key)
        repo_roots.append(repo_root)
    return repo_roots


def _worktree_blocker_tools(blockers: list[dict]) -> list[dict]:
    tools: list[dict] = []
    for blocker in blockers:
        conflicts = blocker.get("conflicts") if isinstance(blocker.get("conflicts"), list) else []
        files = (
            [{"status": "U", "path": path} for path in conflicts if isinstance(path, str) and path]
            or [{"status": "M", "path": "worktree changes"}]
        )
        tools.append({
            "name": "GitDiff",
            "repo": blocker.get("repo_root"),
            "source": blocker.get("repo_root"),
            "worktree_repo": blocker.get("worktree_path"),
            "worktree_status": blocker.get("status") or "pending",
            "worktree_conflicts": conflicts,
            "integration_worktree_path": blocker.get("integration_worktree_path") or "",
            "worktree_blocker": True,
            "worktree_msg_id": blocker.get("msg_id"),
            "files": files,
            "file_count": len(files),
            "additions": 0,
            "deletions": 0,
            "diff": "",
        })
    return tools


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
            "provider": resolved.provider.id if resolved.provider.dedicated_login else provider,
            "dedicated_login": resolved.provider.dedicated_login,
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

def _sync_pi_agents_models_store() -> None:
    """Re-sync pi's models.json for every configured pi agent."""
    for agent in list_agents():
        harness = agent.get("harness", "")
        if harness != "pi":
            continue
        provider_id = agent.get("provider")
        if not provider_id:
            continue
        try:
            resolved = resolve_agent(harness, provider_id)
            resolved.sync_pi_provider(agent.get("model"))
        except (ValueError, OSError) as exc:
            log.warning("pi models.json sync failed for provider %s: %s", provider_id, exc)


async def _recover_orphaned_pending_on_startup():
    orphaned = mark_orphaned_pending(before_created_at=BOOT_TIME)
    if orphaned:
        log.warning("Marked %d orphaned pending messages as error", orphaned)


async def _resume_stalled_flows_on_startup():
    from .flow import recover_durable_flows, sweep_incomplete_flows
    durable = await recover_durable_flows(startup=True)
    if durable["reconciled"] or durable["dispatched"]:
        log.warning(
            "Recovered durable Squid Flow work reconciled=%d dispatched=%d",
            durable["reconciled"], durable["dispatched"],
        )
    resumed = await sweep_incomplete_flows()
    if resumed:
        log.warning("Resumed %d stalled Squid Flow chain(s) on startup", resumed)


def _raise_file_descriptor_limit():
    """Leave headroom for reconnects without lowering an existing limit."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY:
            target = 10240 if hard == resource.RLIM_INFINITY else min(hard, 10240)
            if target > soft:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        log.info("File descriptor limit soft=%s hard=%s", *resource.getrlimit(resource.RLIMIT_NOFILE))
    except (ImportError, OSError, ValueError):
        log.warning("Could not raise file descriptor limit", exc_info=True)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Enclose startup as well as shutdown so startup failures also release the
    # idle WAL connection. It is opened and closed on the event loop thread.
    from . import continuations
    _raise_file_descriptor_limit()
    # One shared cap across clients, owned by this server lifespan/event loop.
    _app.state.diff_revert_semaphore = asyncio.Semaphore(4)
    continuations.configure(False)
    try:
        with database_lifetime():
            async with _server_lifespan(_app):
                yield
    finally:
        continuations.configure(False)


@asynccontextmanager
async def _server_lifespan(_app: FastAPI):
    from .context import runtime_templates
    runtime_templates()
    from . import continuations, watch_launch
    from .watch_config import load_watch_configuration
    # Validate before starting tasks. Construction opens no transports and
    # grants no activation or dispatch authority.
    _app.state.watch_configuration = load_watch_configuration(_cfg)
    from .watch_service import WatchService
    _app.state.watch_service = WatchService(_app.state.watch_configuration)
    global _shore_connection
    publication_recovery = None
    continuation_maintenance = None
    route_maintenance = None
    durable_maintenance = None
    shore_task = None
    shore_stop = asyncio.Event()
    try:
        from .watch_auth import load_digest
        _app.state.watch_control_digest = load_digest(os.environ, create=_app.state.watch_configuration.enabled)
        watch_host = _cfg["server"]["host"]
        if watch_host in {"0.0.0.0", "::"}:
            watch_host = "127.0.0.1" if watch_host == "0.0.0.0" else "::1"
        if ":" in watch_host:
            watch_host = f"[{watch_host}]"
        watch_launch.configure(_app.state.watch_service,
                               f'http://{watch_host}:{_cfg["server"]["port"]}')
        _realtime_notifier.start(asyncio.get_running_loop())
        set_process_change_listener(_publish_process_changed)
        dispatcher.set_queue_change_listener(_publish_queue_changed)
        await asyncio.to_thread(prune_realtime_data)
        # Process and queue registries are intentionally in-memory. Publishing
        # their authoritative startup state advances the durable cursor after a
        # server restart, so a reconnecting client cannot retain pre-restart rows.
        _publish_process_changed(list_active_procs())
        _publish_queue_changed(dispatcher.all_queued_items())
        await asyncio.to_thread(_sync_pi_agents_models_store)
        from .task_publication import recover as recover_task_publications
        await _recover_orphaned_pending_on_startup()
        # Watch turns reconcile their own linked evidence, including ambiguous
        # interrupted work, even when execution has been disabled after restart.
        from .watch_dispatch import WatchDispatch
        await WatchDispatch(_app.state.watch_service, dispatcher).recover()
        await asyncio.to_thread(continuations.recover)
        continuation_maintenance = asyncio.create_task(
            continuations.maintain(dispatcher), name='squid-continuations')
        await _resume_stalled_flows_on_startup()
        from .routes import run as run_routes
        route_maintenance = asyncio.create_task(run_routes(), name='squid-routes')
        from .flow import maintain_durable_flows
        durable_maintenance = asyncio.create_task(
            maintain_durable_flows(), name="squid-flow-durable-maintenance",
        )
        shore_connection = None
        try:
            from .shore_transport import configured_host_connection
            if shore_enabled():
                shore_connection = await asyncio.to_thread(configured_host_connection, shore_identity_dir(_cfg))
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            log.error("Shore host connection disabled: %s", exc)
        _shore_connection = shore_connection
        if shore_connection is not None:
            shore_task = asyncio.create_task(
                shore_connection.run(shore_stop), name="squid-shore-host-connection",
            )
            def _log_shore_failure(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                failure = task.exception()
                if failure is not None:
                    log.error("Shore host connection stopped with an error: %s", failure)
                elif not shore_stop.is_set():
                    log.error("Shore host connection stopped and requires daemon restart or login")
            shore_task.add_done_callback(_log_shore_failure)
        _app.state.watch_service.start_monitoring()
        _app.state.watch_service.start_dispatch(dispatcher)
        continuations.configure(WORKTREE_ISOLATION_ENABLED)
        from .watch_async import call
        publication_recovery = asyncio.create_task(
            call(recover_task_publications), name="squid-publication-recovery")
        yield
    finally:
        continuations.configure(False)
        await _cancel_background_task(publication_recovery)
        await _cancel_background_task(route_maintenance)
        await _cancel_background_task(continuation_maintenance)
        watch_launch.configure(None)
        await _app.state.watch_service.stop_dispatch()
        await _app.state.watch_service.stop_monitoring()
        await asyncio.to_thread(_app.state.watch_service.close)
        _shore_connection = None
        if shore_task is not None:
            shore_stop.set()
        await _cancel_background_task(shore_task)
        await _cancel_background_task(durable_maintenance)
        set_process_change_listener(None)
        dispatcher.set_queue_change_listener(None)
        _realtime_notifier.stop()


async def _cancel_background_task(task: "asyncio.Task | None") -> None:
    """Cancel a lifespan-owned background task and wait for it to unwind.

    Swallows any exception the task raises while unwinding, not just
    CancelledError: shutdown must not let one task's failure block the
    remaining cleanup steps that run after it in _lifespan's finally block.
    """
    if task is None:
        return
    task.cancel()
    with suppress(Exception, asyncio.CancelledError):
        await task


from . import instance_auth


app = FastAPI(title="Squid", version=SQUID_VERSION, lifespan=_lifespan)
app.include_router(instance_auth.router)


# These specific endpoints verify existing turn or host-control credentials
# inside their handlers. All other routes default to the browser session gate.
_SCOPED_ENDPOINTS = {
    "GET": re.compile(
        r"/(?:capabilities|routes(?:/[^/]+)?|continuations(?:/[^/]+)?"
        r"|tools|watch-tools|watches(?:/[^/]+(?:/runs)?)?"
        r"|watch-control/[^/]+/(?:tools|watch-tools|watches(?:/[^/]+(?:/runs)?)?)"
        r"|task-publications/[^/]+)"
    ),
    "POST": re.compile(
        r"/(?:routes(?:/validate|/[^/]+/cancel)?|agent-history/[^/]+"
        r"|continuations(?:/result|/[^/]+/cancel)?"
        r"|watches(?:/validate|/[^/]+/[^/]+)?"
        r"|watch-control/[^/]+/watches(?:/validate|/[^/]+/[^/]+)?"
        r"|task-workspaces/[^/]+/publication-(?:grant|revoke)"
        r"|task-publications/[^/]+/reconcile)"
    ),
}
_HOST_ENDPOINTS = {
    ("GET", "/shore/pairing/status"),
    ("GET", "/shore/pairing/requests"),
    ("POST", "/shore/pairing/begin"),
    ("POST", "/shore/pairing/requests/approve"),
    ("POST", "/shore/pairing/requests/reject"),
    ("GET", "/instance/active-process-count"),
    ("POST", "/instance/restart"),
    ("POST", "/instance/shutdown"),
}
_PUBLIC_ENDPOINTS = {
    ("GET", "/instance/pair"),
    ("GET", "/favicon.png"),
    ("POST", "/instance/exchange"),
    ("GET", "/sw.js"),
}


@app.middleware("http")
async def require_instance_access(request: Request, call_next):
    method = "GET" if request.method == "HEAD" else request.method
    path = request.url.path
    if path.startswith("/shore/") and not shore_enabled():
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    endpoint = (method, path)
    scoped = _SCOPED_ENDPOINTS.get(request.method)
    if endpoint in _PUBLIC_ENDPOINTS or (scoped and scoped.fullmatch(path)):
        return await call_next(request)
    if endpoint in _HOST_ENDPOINTS and await asyncio.to_thread(instance_auth.host_authorized, request):
        return await call_next(request)
    if path in {"/instance/active-process-count", "/instance/restart", "/instance/shutdown"}:
        return JSONResponse({"error": "host_authorization_required"}, status_code=401)

    session_id = await asyncio.to_thread(instance_auth.browser_session, request)
    if not session_id:
        headers = {"Cache-Control": "no-store"}
        if endpoint == ("GET", "/health"):
            return JSONResponse({"status": "ok", "boot_time": BOOT_TIME}, headers=headers)
        if method == "GET" and (path in {"/", "/index.html"}
                                or request.headers.get("sec-fetch-mode") == "navigate"):
            return RedirectResponse("/instance/pair", status_code=303, headers=headers)
        return JSONResponse({"error": "pairing_required"}, status_code=401, headers=headers)
    if method not in {"GET", "OPTIONS"} and request.headers.get("origin") != instance_auth.browser_origin(request):
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)
    request.state.browser_session_id = session_id
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "private, no-store")
    return response


@app.get("/config/features")
async def get_features(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration reads are not allowed"}, status_code=403)
    return JSONResponse({"shore": shore_enabled()}, headers={"Cache-Control": "no-store"})


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    lookback_excluded_ids: Optional[list[int]] = None
    adhoc: bool = Field(False)
    pinned_ids: Optional[list[int]] = None
    attached_paths: Optional[list[str]] = None
    include_topic_memory: bool = Field(False)
    provider_approval: Optional[str] = None
    flow_run_id: Optional[str] = None
    flow_route: Optional[str] = None
    task_workspace: Optional[dict] = None
    task_id: Optional[str] = None
    publication: Optional[dict] = None
    publication_expires_at: Optional[float] = None


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
    effort: Optional[str] = None
    cwd: Optional[str] = None
    home_mode: Optional[Literal["user_home", "blank_home"]] = None


class AgentHomeModeRequest(BaseModel):
    home_mode: Literal["user_home", "blank_home"]


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


class LocalfileCheckPathsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class LocalfileRevertEditRequest(BaseModel):
    edit_id: int


class CredsRequest(BaseModel):
    claude_org_id: str = Field(..., min_length=1)
    claude_session_key: str = Field(..., min_length=1)


class CodexCredsRequest(BaseModel):
    token: str = Field(..., min_length=1)
    account_id: str = ""


class ShoreRevokeDeviceRequest(BaseModel):
    device_id: str = Field(..., min_length=1)


class ShoreApprovePairingRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    verification_code: str = Field(..., min_length=1, max_length=64)


class ShoreRejectPairingRequest(BaseModel):
    request_id: str = Field(..., min_length=1)


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
    command: Literal["stop", "stopall", "deq", "list", "restart", "shutdown", "clear", "stop_msg", "journal"]
    topic: str = "default"
    agent: Optional[str] = None
    adhoc: Optional[bool] = None
    pos: Optional[int] = None
    msg_id: Optional[int] = None
    upgrade: Optional[bool] = None
    source: Optional[str] = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")


def _is_running_from_pipx_agentsquid() -> bool:
    try:
        parts = Path(sys.executable).resolve().parts
    except OSError:
        return False
    return any(
        part == "venvs"
        and idx + 1 < len(parts)
        and parts[idx + 1] == "agentsquid"
        for idx, part in enumerate(parts)
    )


def _pipx_upgrade_unavailable_reason() -> Optional[str]:
    if SQUID_VERSION == "0+local":
        return "upgrade is only available for installed agentsquid packages"
    if not _is_running_from_pipx_agentsquid():
        return "upgrade on restart is only available when running the pipx-installed agentsquid app"
    if not shutil.which("pipx"):
        return "pipx is not installed or not on PATH"
    return None


def _pipx_upgrade_agentsquid() -> tuple[bool, str]:
    unavailable = _pipx_upgrade_unavailable_reason()
    if unavailable:
        return False, unavailable
    pipx = shutil.which("pipx")
    try:
        result = subprocess.run(
            [pipx, "upgrade", "agentsquid"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "pipx upgrade agentsquid timed out"
    except OSError as exc:
        return False, f"pipx upgrade agentsquid failed to start: {exc}"
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        return False, output or f"pipx upgrade agentsquid exited with status {result.returncode}"
    return True, output or "pipx upgrade agentsquid completed"


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
    if "shell_timeout" in agent_cfg:
        value = agent_cfg.get("shell_timeout")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("agent.shell_timeout must be a positive integer")

    realtime_transport(parsed)
    from .config import shore_enabled as validate_shore_enabled
    validate_shore_enabled(parsed)
    shore_identity_dir(parsed)
    from .watch_config import load_watch_configuration
    load_watch_configuration(parsed)

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


def _request_is_loopback(headers, direct_host: Optional[str]) -> bool:
    """True only for a browser talking directly to 127.0.0.1/localhost —
    false for every proxied path, including `tailscale serve` to this same
    machine (its IP or MagicDNS name both work now, see ADR-0037's tailscale
    fixes) as well as a genuinely remote tailnet client.

    `direct_host` (the raw TCP peer) is unreliable as *proof of remoteness*
    on its own: `tailscale serve` reverse-proxies to this server over
    loopback, so a tailnet client also arrives with a 127.0.0.1 peer address.
    It's still trustworthy as proof of *localness*, though — the server only
    ever binds 127.0.0.1 (enforced elsewhere), so a raw peer address can't be
    spoofed to loopback by a remote client the way a header can.

    X-Forwarded-For is attacker-controlled input, not a trusted client
    address — a remote client can simply send `X-Forwarded-For: 127.0.0.1`
    itself, and some proxies append to rather than replace an existing
    value, so parsing "the real client IP" out of it is not safe here. Fail
    closed instead: treat the mere *presence* of any forwarding/identity
    header as proof the request came through a proxy (tailscale serve or
    otherwise), hence remote, regardless of its value. A direct local browser
    talking to 127.0.0.1 never sets these.
    """
    # Any of the common proxy/forwarding markers makes a request remote: the
    # proxy-standard X-Forwarded-For and RFC 7239 Forwarded, the de-facto
    # X-Real-IP, and Tailscale-User-Login (set by `tailscale serve` when it
    # forwards a tailnet identity). Checking all of them keeps the gate
    # fail-closed even if a specific proxy sets only one.
    if (
        headers.get("x-forwarded-for")
        or headers.get("x-real-ip")
        or headers.get("forwarded")
        or headers.get("tailscale-user-login")
    ):
        return False
    if not direct_host:
        return False
    try:
        return ipaddress.ip_address(direct_host).is_loopback
    except ValueError:
        return False


def _auth_session_validation_error(harness: str, mode: str, model: Optional[str]) -> Optional[str]:
    """Semantic validation shared by HTTP and WebSocket auth-session starts."""
    if mode == "login":
        if harness not in SUPPORTED_HARNESSES:
            return f"Unknown harness {harness!r}"
    elif mode == "install":
        if harness not in SUPPORTED_HARNESSES and harness != "ollama":
            return f"Unknown install target {harness!r}"
    elif mode == "unlock":
        if harness != "keychain":
            return "Keychain unlock requires the keychain target"
    elif mode in ("pull", "remove"):
        from .providers import PROVIDERS
        if harness not in PROVIDERS or harness != "ollama":
            return f"mode={mode!r} is only supported for the ollama provider"
        if not model:
            return "model is required"
    else:
        return f"Unknown auth-session mode {mode!r}"
    return None


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
    normalized = data.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    encoded = "\n".join("data:" + (" " if line.startswith(" ") else "") + line for line in lines)
    return encoded + "\n\n"

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
    try:
        from .flow import parse_route_chain
        parsed = parse_route_chain(text)
        if parsed:
            return parsed["route"]
    except Exception:
        pass
    parts = [part for part in text.split(",") if part]
    return ",".join(parts)


def _persist_flow_plan(flow_run_id: Optional[str], flow_route: Optional[str], prepared: dict) -> Optional[str]:
    if not flow_run_id or not flow_route:
        return None
    from .flow import durable_flow_plan
    plan = durable_flow_plan(flow_route)
    if not plan:
        return None
    created_run = False
    claimed_origin_id: Optional[str] = None
    origin_running = False
    try:
        existing_run = get_flow_run(flow_run_id)
        create_flow_run(
            flow_run_id, flow_route, prepared["user_msg_id"], plan,
            if_not_exists=True, execution_mode="durable",
        )
        created_run = existing_run is None
        origin = next(
            (step for step in get_flow_steps(flow_run_id)
             if step["leg"] == "origin"
             and step["topic"] == prepared["topic"]
             and step["agent"] == prepared["agent"]
             and step["assistant_msg_id"] is None),
            None,
        )
        now = _utc_now_iso()
        if not origin or not claim_flow_step(flow_run_id, origin["step_id"], now):
            raise RuntimeError("durable Flow origin could not be claimed")
        claimed_origin_id = origin["step_id"]
        if not link_flow_step_messages(
            flow_run_id, origin["step_id"], prepared["user_msg_id"], prepared["asst_msg_id"],
        ):
            raise RuntimeError("durable Flow origin could not be linked")
        if not transition_flow_step(flow_run_id, origin["step_id"], "claimed", "running", now):
            raise RuntimeError("durable Flow origin could not enter running state")
        origin_running = True
    except Exception:
        log.exception("durable Flow plan persistence failed flow_run_id=%s route=%s", flow_run_id, flow_route)
        error = "Durable Flow plan could not be persisted. The turn was not started."
        try:
            if created_run:
                cancel_flow_run(flow_run_id, _utc_now_iso(), error)
            elif claimed_origin_id:
                transition_flow_step(
                    flow_run_id, claimed_origin_id,
                    "running" if origin_running else "claimed",
                    "error", _utc_now_iso(), error=error,
                )
        except Exception:
            log.exception("failed to terminally reconcile Flow activation flow_run_id=%s", flow_run_id)
        update_assistant_message(
            prepared["asst_msg_id"], error, None, "error", only_if_pending=True,
        )
        return error
    return None


async def _prepare_chat_turn(
    *,
    message: str,
    topic: str,
    agent: Optional[str],
    adhoc: bool = False,
    lookback: int = 0,
    lookback_via_pins: bool = False,
    lookback_excluded_ids: Optional[list[int]] = None,
    pinned_ids: Optional[list[int]] = None,
    attached_paths: Optional[list[str]] = None,
    include_topic_memory: bool = False,
    source: str = "human",
    flow_run_id: Optional[str] = None,
    flow_route: Optional[str] = None,
    flow_step_id: Optional[str] = None,
    override_cwd: Optional[str] = None,
    watch_run_id: Optional[str] = None,
    task_workspace: Optional[dict] = None,
    task_id: Optional[str] = None,
    provider_confirmation: Optional[tuple[str, str, Optional[str]]] = None,
) -> Union[dict, JSONResponse]:
    native_shell = message.lstrip().startswith("!")
    try:
        rule = None if native_shell else provider_policy.rule_for(topic)
    except provider_policy.InvalidPolicy as exc:
        return JSONResponse({"error": "provider_policy_invalid", "detail": str(exc), "topic": topic},
                            status_code=403)
    resolved_agent = agent
    agent_config = {}
    if agent:
        agent_config = get_agent(agent) or {}
        if not agent_config:
            return JSONResponse(
                {"error": f"Agent '{agent}' not found. Create it first via /config/agents."},
                status_code=400,
            )
    else:
        sticky_agent = (get_topic(topic) or {}).get("agent")
        if sticky_agent:
            agent_config = get_agent(sticky_agent) or {}
            resolved_agent = sticky_agent

        def preference(candidate):
            if not candidate:
                return 3
            try:
                provider_id = _resolve_agent_runtime(candidate)[3].provider.id
            except ValueError:
                return 2
            decision = rule.decision(provider_id)
            if decision is None:
                return 0
            return 1 if decision == "provider_approval_required" else 2

        sticky_rank = preference(agent_config) if rule else (0 if agent_config else 3)
        if sticky_rank:
            default = get_default_agent()
            if default and (not agent_config or preference(default) < sticky_rank):
                resolved_agent = default["name"]
                agent_config = default

    def policy_error(decision, provider_id, **extra):
        return JSONResponse({
            "error": decision, "detail": provider_policy.detail(decision, topic, resolved_agent, provider_id),
            "topic": topic, "agent": resolved_agent, "provider": provider_id, **extra,
        }, status_code=409 if decision == "provider_approval_required" else 403)

    try:
        harness, provider, backend, resolved_runtime = _resolve_agent_runtime(agent_config)
    except ValueError as exc:
        if rule and rule.deny:
            return policy_error("provider_unverified", None)
        return JSONResponse({"error": str(exc)}, status_code=400)
    provider_id = resolved_runtime.provider.id if rule else None
    decision = rule.decision(provider_id) if rule else None
    if decision and (decision != "provider_approval_required" or not provider_confirmation or source != "human"):
        return policy_error(decision, provider_id)
    if decision is None and provider_confirmation and provider_confirmation[2]:
        return JSONResponse({"error": "provider_approval_stale",
                             "detail": "Provider approval no longer matches this turn. Resubmit the turn."},
                            status_code=409)
    codex_binding = None
    if harness in {"codex", "claudecode"} and not native_shell:
        revision = codex_subscription.agent_revision(resolved_agent)
        codex_binding = await asyncio.to_thread(
            codex_subscription.bind, resolved_runtime.provider, resolved_agent, harness, revision=revision,
        )
        if revision != codex_subscription.agent_revision(resolved_agent):
            return JSONResponse({"error": "Subscription provider changed during preparation. Resubmit the turn."},
                                status_code=409)
    if (codex_binding and codex_binding.account == codex_subscription.CLAUDE_UNVERIFIED
            and source != "human"):
        return JSONResponse(
            {"error": "This Claude login supports fresh manual turns only because its credential identity cannot be verified"},
            status_code=409,
        )
    runtime_fingerprint = (
        codex_subscription.session_fingerprint(resolved_runtime.fingerprint, codex_binding)
        if codex_binding is not None else resolved_runtime.fingerprint
    )
    model: Optional[str] = agent_config.get("model") or None
    approved_runtime = None
    if decision == "provider_approval_required":
        session, request_digest, token = provider_confirmation
        runtime = provider_policy.Runtime(topic, resolved_agent, runtime_fingerprint, model,
                                          codex_subscription.agent_revision(resolved_agent))
        if not provider_policy.accept(token, session, request_digest, runtime):
            return policy_error(decision, provider_id,
                                approval_token=provider_policy.challenge(session, request_digest, runtime))
        approved_runtime = runtime
    native_backend_command = _is_backend_native_chat_command(message)

    if watch_run_id is None and source != "continuation":
        upsert_topic(topic, resolved_agent, last_prompt=message,
                     last_harness=harness, last_provider=provider, last_model=model, adhoc=adhoc)
    agent_cwd: Optional[str] = agent_config.get("cwd") or None
    response_timeout: int = RESPONSE_TIMEOUT

    # 2. Resumable session lookup (skipped for adhoc turns)
    resume_session_id: Optional[str] = None
    cwd: Optional[str] = agent_cwd

    if not native_shell and not adhoc and resolved_agent:
        stored = get_topic_session(topic, resolved_agent)
        if stored:
            stored_fingerprint = stored.get("runtime_fingerprint")
            if codex_binding and codex_binding.error:
                # A temporary probe failure, including an active sign-in,
                # leaves the existing session available for another request.
                pass
            elif (codex_binding and not codex_binding.can_reuse) or (
                (stored_fingerprint or (codex_binding and codex_binding.dedicated))
                and stored_fingerprint != runtime_fingerprint
            ):
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
            topic, lookback, agent=resolved_agent, excluded_ids=lookback_excluded_ids
        )

    memory_config = topic_memory_squid_config(topic)
    code_roots = memory_config.get("code_roots") or []
    if native_shell:
        # Native commands use the resolved lane cwd, matching where the model
        # starts, but bypass per-turn worktree creation and synchronization.
        # Topic code roots describe scope; their ordering must not implicitly
        # select a shell directory when more than one is configured.
        code_roots = []
    if native_backend_command or harness == "echo":
        code_roots = []
    if override_cwd:
        # Runs against a specific pre-existing directory (e.g. an integration
        # worktree mid-conflict) instead of the topic's normal isolated
        # worktree, so skip isolation/blocker checks entirely.
        code_roots = []
        cwd = override_cwd
        agent_cwd = override_cwd
    source_cwd = cwd

    from . import task_workspaces
    try:
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise ValueError('invalid task workspace identity')
        flow_task_id = await asyncio.to_thread(task_workspaces.for_flow, flow_run_id)
        if task_id and flow_task_id and task_id != flow_task_id:
            raise RuntimeError('task Flow destination mismatch')
        task_id = task_id or flow_task_id
        watch_task = await asyncio.to_thread(task_workspaces.for_watch, watch_run_id, resolved_agent,
            service=getattr(app.state, 'watch_service', None)) if watch_run_id else None
        if watch_task:
            if task_id and task_id != watch_task['id']:
                raise RuntimeError('task watch destination mismatch')
            task_id = watch_task['id']
        task_targeted = task_workspace is not None or task_id is not None
        if task_targeted:
            if not WORKTREE_ISOLATION_ENABLED or native_shell or native_backend_command or override_cwd or harness == 'echo':
                raise ValueError('task workspace requires isolated editing turn')
            if task_workspace is not None and (source != 'human' or task_id or
                    set(task_workspace) != {'repository', 'base_ref'}):
                raise ValueError('invalid task workspace request')
            if task_id:
                selected_task = await asyncio.to_thread(task_workspaces.get, task_id)
                await asyncio.to_thread(task_workspaces.validate, selected_task, agent=resolved_agent)
            adhoc, resume_session_id = True, None
    except (RuntimeError, ValueError, PermissionError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)

    if WORKTREE_ISOLATION_ENABLED and code_roots and not task_targeted:
        repo_roots = await _repo_roots_for_code_roots(code_roots)
        if repo_roots:
            from .worktree import settle_worktrees_before_turn
            blockers = await settle_worktrees_before_turn(topic, repo_roots)
        else:
            blockers = []
        if not blockers:
            blockers = await _active_worktree_blockers(topic, code_roots)
        if blockers and watch_run_id is not None:
            return JSONResponse({"error": "watch_worktree_blocked"}, status_code=409)
        if blockers:
            # Persist a trace even though this turn never runs — otherwise a
            # blocked turn (e.g. a broadcast sibling that loses a worktree-sync
            # race) leaves no record anywhere but the server log.
            blocker_desc = "; ".join(
                f"{b.get('worktree_path') or b.get('repo_root')} ({b.get('status')})" for b in blockers
            )
            blocked_user_id = insert_user_message(topic, resolved_agent, message,
                                                    lookback=lookback, source=source,
                                                    flow_run_id=flow_run_id, flow_route=flow_route)
            blocked_asst_id = insert_assistant_message(topic, resolved_agent, blocked_user_id,
                                                         adhoc=adhoc, flow_run_id=flow_run_id, flow_route=flow_route)
            update_assistant_message(
                blocked_asst_id,
                f"Blocked: worktree sync requires attention before starting another turn — {blocker_desc}",
                None, status="error", context=json.dumps(_worktree_blocker_tools(blockers)),
            )
            return JSONResponse({
                "error": "worktree sync requires attention before starting another turn",
                "worktrees": blockers,
                "msg_id": blocked_asst_id,
            }, status_code=409)

    memory_revision: Optional[str] = None
    memory_data_for_prompt: Optional[dict] = None
    if not native_backend_command and include_topic_memory:
        memory_data_for_prompt = read_topic_memory(topic)
        if not native_shell and memory_data_for_prompt["content"].strip():
            memory_revision = memory_data_for_prompt.get("revision")

    # Durable Flow dependencies are source references, resolved at runner
    # launch when the actual resumed session is known, rather than user pins.
    effective_pinned_ids = pinned_ids
    if source == 'workflow' and flow_run_id and flow_step_id:
        from .stats_db import get_flow_steps
        steps = {step['step_id']: step for step in get_flow_steps(flow_run_id)}
        dependencies = steps.get(flow_step_id, {}).get('dependencies', [])
        deferred_ids = {steps[dep].get('assistant_msg_id') for dep in dependencies if dep in steps}
        effective_pinned_ids = [pid for pid in pinned_ids or [] if pid not in deferred_ids]
    stored_context_ids = list(dict.fromkeys([*(context_ids or []), *(effective_pinned_ids or [])])) or None
    if watch_run_id is not None:
        from .watch_runs import prepare_messages
        user_msg_id, asst_msg_id = await asyncio.to_thread(
            prepare_messages, watch_run_id, topic=topic, agent=resolved_agent, content=message, adhoc=adhoc,
            flow_run_id=flow_run_id, flow_route=flow_route, flow_step_id=flow_step_id, context_ids=stored_context_ids)
    else:
        user_msg_id = insert_user_message(topic, resolved_agent, message,
                                          context_ids=stored_context_ids,
                                          mem=bool(memory_revision),
                                          mem_revision=memory_revision,
                                          attached_paths=attached_paths,
                                          lookback=lookback,
                                          source=source,
                                          flow_run_id=flow_run_id,
                                          flow_route=flow_route,
                                          flow_step_id=flow_step_id)
        asst_msg_id = insert_assistant_message(
            topic, resolved_agent, user_msg_id, adhoc=adhoc,
            flow_run_id=flow_run_id, flow_route=flow_route,
            source="shell" if native_shell else source,
            flow_step_id=flow_step_id,
        )
    if codex_binding is not None and codex_binding.dedicated:
        from .stats_db import record_codex_provider_home
        record_codex_provider_home(asst_msg_id, str(codex_binding.home), codex_binding.recorded_account, codex_binding.harness)
    if task_targeted:
        try:
            if task_workspace is not None:
                task = await asyncio.to_thread(task_workspaces.create, topic=topic, agent=resolved_agent,
                    request_id=user_msg_id, **task_workspace)
                task_id = task['id']
            task = await asyncio.to_thread(task_workspaces.bind, asst_msg_id, task_id)
            cwd = source_cwd = task['worktree_path']
        except (RuntimeError, ValueError, PermissionError) as exc:
            update_assistant_message(asst_msg_id, str(exc), None, status='error')
            return JSONResponse({'error': str(exc), 'msg_id': asst_msg_id}, status_code=409)
    if not native_shell:
        attach_assistant_session(asst_msg_id, resume_session_id)

    effective_message = message
    prefix_blocks: list[str] = []
    tracking_roots: list[str] = [] if native_shell or native_backend_command else code_roots
    shell_pinned_contents: list[str] = []
    shell_topic_memory: Optional[str] = None

    if native_shell and pinned_ids:
        shell_pinned_contents = [
            item["content"] for item in get_messages_by_ids(pinned_ids)
            if item.get("role") == "assistant"
        ]

    if native_shell and memory_data_for_prompt:
        shell_topic_memory = memory_data_for_prompt["content"]

    if memory_data_for_prompt and not native_shell:
        memory_content = memory_data_for_prompt["content"].strip()
        if memory_content:
            prefix_blocks.append("\n".join([
                "Persistent user-editable topic memory:",
                f'<topic_memory topic="{memory_data_for_prompt["topic"]}">',
                memory_content,
                "</topic_memory>",
            ]))

    if not native_shell and not native_backend_command and effective_pinned_ids:
        lookback_id_set = set(context_ids or [])
        filtered = [pid for pid in effective_pinned_ids if pid not in lookback_id_set]
        if filtered:
            pinned_context = get_messages_by_ids(filtered)
            if pinned_context:
                if adhoc:
                    context_history = pinned_context + context_history
                else:
                    lines = ["Relevant context from other sessions:\n<referenced_context>"]
                    for i in range(0, len(pinned_context), 2):
                        user_msg, asst_msg = pinned_context[i], pinned_context[i + 1]
                        lines.append(f"From #{asst_msg['topic']}@{asst_msg['agent']}:")
                        lines.append(f"User: {user_msg['content'].strip()}")
                        lines.append(f"Assistant: {asst_msg['content'].strip()}")
                    lines.append("</referenced_context>\n")
                    prefix_blocks.append("\n".join(lines))

    if prefix_blocks:
        effective_message = "\n\n".join(prefix_blocks + [message])

    if attached_paths and not native_shell:
        effective_message = "\n\n".join([
            effective_message,
            "Files:\n" + "\n".join(f"- {path}" for path in attached_paths),
        ])

    log.info(
        "chat  topic=%s  agent=%s  harness=%s  provider=%s  backend=%s  model=%s  adhoc=%s  resume=%s  ctx=%d  pinned=%d  memory=%s  msg=%.80r",
        topic, resolved_agent, harness, provider, backend, model, adhoc,
        bool(resume_session_id), len(context_history) // 2,
        len(pinned_ids) if pinned_ids else 0, include_topic_memory, message,
    )
    return {
        "effective_message": message.lstrip()[1:].lstrip() if native_shell else effective_message,
        "topic": topic,
        "agent": resolved_agent,
        "backend": backend,
        "model": model,
        "effort": agent_config.get("effort"),
        "cwd": cwd,
        "context_history": context_history,
        "context_ids": stored_context_ids or [],
        "user_msg_id": user_msg_id,
        "asst_msg_id": asst_msg_id,
        "task_id": task_id,
        "response_timeout": response_timeout,
        "resume_session_id": resume_session_id,
        "adhoc": adhoc,
        "lookback": lookback,
        "code_roots": tracking_roots,
        "display_prompt": message,
        "source_cwd": source_cwd,
        "configured_cwd": agent_cwd,
        "harness": harness,
        "provider": provider,
        "codex_binding": codex_binding,
        "approved_runtime": approved_runtime,
        "worktree_setup_elapsed_ms": None,
        "worktree_isolated": False,
        "native_shell": native_shell,
        "shell_pinned_contents": shell_pinned_contents,
        "shell_attached_paths": list(attached_paths or []) if native_shell else [],
        "shell_topic_memory": shell_topic_memory,
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
    native_shell: bool = False,
) -> None:
    """Drain the worker queue after client disconnect; save final content to DB."""
    loop = asyncio.get_event_loop()
    timeout_s = float(drain_timeout if drain_timeout is not None else RESPONSE_TIMEOUT)
    command_started = not native_shell or bool(raw or status_raw or tool_events)
    deadline = loop.time() + timeout_s if command_started else None
    tool_events = list(tool_events or [])
    timed_out = False
    try:
        while True:
            left = None if deadline is None else deadline - loop.time()
            if left is not None and left <= 0:
                log.warning("drain timeout msg_id=%s, saving partial", msg_id)
                timed_out = True
                break
            try:
                wait_timeout = 30.0 if left is None else min(left, 30.0)
                chunk = await asyncio.wait_for(out_q.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                continue
            if chunk is None:
                break
            if isinstance(chunk, dict):
                if native_shell and not command_started and "_processing" in chunk:
                    command_started = True
                    deadline = loop.time() + timeout_s
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
    configured_cwd: Optional[str] = None,
    harness: Optional[str] = None,
    provider: Optional[str] = None,
    worktree_setup_elapsed_ms: Optional[float] = None,
    worktree_isolated: bool = False,
    native_shell: bool = False,
    shell_pinned_contents: Optional[list[str]] = None,
    shell_attached_paths: Optional[list[str]] = None,
    shell_topic_memory: Optional[str] = None,
    effort: Optional[str] = None,
    codex_binding: Optional[codex_subscription.Binding] = None,
    approved_runtime: Optional[provider_policy.Runtime] = None,
    context_ids: Optional[list[int]] = None,
) -> AsyncGenerator[str, None]:
    meta_payload = {
        "agent": agent,
        "harness": harness,
        "provider": provider,
        "model": model,
        "msg_id": asst_msg_id,
        "adhoc": adhoc,
        "effort": effort,
        **({"dedicated_codex": codex_binding.dedicated} if codex_binding else {}),
        "kind": "shell_result" if native_shell else "assistant",
        **({"context_ids": context_ids} if context_ids is not None else {}),
        **({"shell_timeout": NATIVE_SHELL_TIMEOUT} if native_shell else {}),
    }
    yield sse_event("meta", json.dumps(meta_payload))
    # Record the same meta for the WebSocket path, whose realtime runner
    # consumes this generator and discards every chunk — the SSE 'meta' event
    # never reaches a WS client. seq=0 reserves the slot ahead of
    # queued/processing/loading (1/2/3) so the resolved provider lands before
    # the client's after-meter read; it is in the replay allowlist so a
    # reconnecting client gets it too.
    insert_run_event(asst_msg_id, 0, "meta", json.dumps(meta_payload))

    effective_cwd = cwd or SQUID_HOME
    dispatch_harness, dispatch_provider = split_agent_ref(harness or backend, provider)
    out_q, seq, worker = await dispatcher.dispatch(
        topic=topic, prompt=message, context_history=context_history,
        harness=dispatch_harness, provider=dispatch_provider,
        model=model, effort=effort, agent=agent, cwd=effective_cwd,
        source_cwd=source_cwd,
        configured_cwd=configured_cwd,
        response_timeout=response_timeout,
        resume_session_id=resume_session_id,
        adhoc=adhoc, lookback=lookback, msg_id=asst_msg_id,
        code_roots=code_roots,
        display_prompt=display_prompt,
        worktree_setup_elapsed_ms=worktree_setup_elapsed_ms,
        worktree_isolated=worktree_isolated,
        native_shell=native_shell,
        shell_pinned_contents=shell_pinned_contents,
        shell_attached_paths=shell_attached_paths,
        shell_topic_memory=shell_topic_memory, codex_binding=codex_binding, approved_runtime=approved_runtime,
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

            elif isinstance(chunk, dict) and "_loading" in chunk:
                # ADR-0037: local-model load/unload visibility — payload is
                # {"to": model} or {"to": model, "from": prev_model} on a switch.
                yield sse_event("loading", json.dumps(chunk["_loading"]))

            elif isinstance(chunk, dict) and "_processing" in chunk:
                yield sse_event("processing", json.dumps(chunk["_processing"]))

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
                    drain_timeout=response_timeout, native_shell=native_shell,
                ),
                name=f"squid-drain-{asst_msg_id}",
            )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _provider_confirmation(req: ChatRequest, browser_session_id: Optional[str]):
    if not browser_session_id or req.source != "human":
        return None
    payload = req.model_dump_json(exclude={"provider_approval"})
    return browser_session_id, hashlib.sha256(payload.encode()).hexdigest(), req.provider_approval


@app.post("/chat")
async def chat(req: ChatRequest, request: Request = None):
    publication_audit = None
    if req.publication is not None or req.publication_expires_at is not None:
        try:
            from .task_publication_api import human
            from .watch_service import WatchDenied
            if (req.source != 'human' or req.publication is None or req.publication_expires_at is None
                    or not (req.task_workspace or req.task_id) or request is None):
                raise WatchDenied()
            publication_audit = human(request)
        except (PermissionError, WatchDenied):
            return JSONResponse({'error':'publication_not_authorized'}, status_code=403)
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
        lookback_excluded_ids=req.lookback_excluded_ids,
        pinned_ids=req.pinned_ids,
        attached_paths=req.attached_paths,
        include_topic_memory=req.include_topic_memory,
        source=req.source,
        flow_run_id=flow_run_id,
        flow_route=flow_route,
        task_workspace=req.task_workspace, task_id=req.task_id,
        provider_confirmation=_provider_confirmation(
            req, getattr(request.state, "browser_session_id", None) if request else None),
    )
    if isinstance(prepared, JSONResponse):
        return prepared
    flow_plan_error = _persist_flow_plan(flow_run_id, flow_route, prepared)
    if flow_plan_error:
        return JSONResponse(
            {"error": "flow_plan_persistence_failed", "detail": flow_plan_error,
             "msg_id": prepared["asst_msg_id"]},
            status_code=500,
        )
    if publication_audit:
        from . import task_publication, task_workspaces
        try:
            task = await asyncio.to_thread(task_workspaces.for_turn, prepared['asst_msg_id'])
            await asyncio.to_thread(task_publication.record_grant, task['id'], request_id=prepared['user_msg_id'],
                publication=req.publication, expires_at=req.publication_expires_at, audit=publication_audit)
        except (RuntimeError, ValueError, TypeError, PermissionError):
            update_assistant_message(prepared['asst_msg_id'], 'Publication grant rejected', None, status='error')
            return JSONResponse({'error':'publication_grant_rejected'}, status_code=409)
    await maybe_sync()
    return StreamingResponse(
        stream_response(
            prepared["effective_message"], prepared["topic"], prepared["agent"], prepared["backend"], prepared["model"], prepared["cwd"],
            prepared["context_history"], prepared["asst_msg_id"], prepared["response_timeout"],
            effort=prepared.get("effort"), codex_binding=prepared.get("codex_binding"),
            approved_runtime=prepared.get("approved_runtime"),
            resume_session_id=prepared["resume_session_id"],
            adhoc=prepared["adhoc"],
            lookback=prepared["lookback"],
            code_roots=prepared["code_roots"],
            display_prompt=prepared["display_prompt"],
            source_cwd=prepared["source_cwd"],
            configured_cwd=prepared["configured_cwd"],
            harness=prepared["harness"],
            provider=prepared["provider"],
            worktree_setup_elapsed_ms=prepared["worktree_setup_elapsed_ms"],
            worktree_isolated=prepared["worktree_isolated"],
            native_shell=prepared["native_shell"],
            shell_pinned_contents=prepared["shell_pinned_contents"],
            shell_attached_paths=prepared["shell_attached_paths"],
            shell_topic_memory=prepared["shell_topic_memory"],
            context_ids=prepared.get("context_ids"),
        ),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "X-Squid-Msg-Id": str(prepared["asst_msg_id"]),
            "X-Squid-Flow-Run-Id": flow_run_id or "",
            "X-Squid-Task-Id": prepared.get("task_id") or "",
        },
    )


@app.post("/instance/restart")
async def host_restart():
    return await run_cmd(CmdRequest(command="restart", topic="default", upgrade=False))


@app.post("/instance/shutdown")
async def host_shutdown():
    return await run_cmd(CmdRequest(command="shutdown", topic="default"))


@app.post("/cmd")
async def run_cmd(req: CmdRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic

    if req.command == "stop_msg":
        drained = 0
        if req.msg_id:
            mark_assistant_cancelled(req.msg_id, "Cancelled")
            # A message still sitting in the topic queue (never started) has no
            # registered process for kill_proc_by_msg_id to find, so it would
            # otherwise linger in /queue and keep the status dot blinking after
            # the chat bubble already shows "Cancelled."
            drained = dispatcher.drain_topic(topic, msg_id=req.msg_id)
        killed = kill_proc_by_msg_id(req.msg_id) if req.msg_id else 0
        log.info(
            "cmd stop_msg topic=%s msg_id=%s source=%s killed=%s drained=%s",
            req.topic, req.msg_id, req.source or "unspecified", killed, drained,
        )
        return JSONResponse({"ok": True, "killed": killed, "drained": drained})
    if req.command == "stop":
        killed = dispatcher.stop_topic(topic, agent=req.agent, adhoc=req.adhoc)
        log.info("cmd stop topic=%s agent=%s adhoc=%s source=%s killed=%s", topic, req.agent, req.adhoc, req.source or "unspecified", killed)
        return JSONResponse({"ok": True, "killed": killed})
    if req.command == "stopall":
        result = dispatcher.stopall_topic(topic, agent=req.agent, adhoc=req.adhoc)
        log.info(
            "cmd stopall topic=%s agent=%s adhoc=%s source=%s killed=%s drained=%s",
            topic,
            req.agent,
            req.adhoc,
            req.source or "unspecified",
            result.get("killed"),
            result.get("drained"),
        )
        return JSONResponse({"ok": True, **result})
    if req.command == "deq":
        drained = dispatcher.drain_topic(topic, req.pos, msg_id=req.msg_id)
        log.info(
            "cmd deq topic=%s pos=%s msg_id=%s source=%s drained=%s",
            topic, req.pos, req.msg_id, req.source or "unspecified", drained,
        )
        return JSONResponse({"ok": True, "drained": drained})
    if req.command == "list":
        return JSONResponse({"ok": True, "topics": get_topics_summary()})
    if req.command == "restart":
        if req.upgrade:
            ok, output = await asyncio.to_thread(_pipx_upgrade_agentsquid)
            if not ok:
                log.warning("upgrade before restart failed: %s", output)
                return JSONResponse({"ok": False, "error": output}, status_code=500)
            log.info("upgrade before restart completed: %s", output)

        async def _restart():
            await asyncio.sleep(0.4)
            kill_all_procs()
            if "--reload" in sys.argv:
                Path(__file__).touch()
            else:
                # uvicorn marks its listening socket inheritable
                # (Config.bind_socket calls sock.set_inheritable(True)), so
                # it survives this in-place re-exec instead of closing. The
                # re-exec'd process's startup port probe in main() then
                # finds that leaked, still-listening fd bound to the same
                # port and exits thinking a second instance already owns
                # it. Close every inheritable fd (past stdio) first so the
                # new process starts with a clean fd table.
                for entry in os.listdir("/dev/fd"):
                    try:
                        fd = int(entry)
                    except ValueError:
                        continue
                    if fd <= 2:
                        continue
                    try:
                        if os.get_inheritable(fd):
                            os.close(fd)
                    except OSError:
                        pass
                from .watch_auth import restart_environment
                environment = restart_environment(
                    os.environ, getattr(app.state, "watch_control_digest", None))
                os.execve(sys.executable, [sys.executable, "-m", "agent.server"], environment)
        asyncio.create_task(_restart())
        return JSONResponse({"ok": True})
    if req.command == "shutdown":
        async def _shutdown():
            await asyncio.sleep(0.4)
            kill_all_procs()
            try:
                pid_file, _boot_log = _lifecycle_paths()
                if _read_lifecycle_pid(pid_file) == os.getpid():
                    pid_file.unlink(missing_ok=True)
            except OSError:
                pass
            os._exit(0)

        asyncio.create_task(_shutdown())
        return JSONResponse({"ok": True})

    if req.command == "clear":
        agent = req.agent or get_active_agent_for_topic(topic)
        if not agent:
            topic_row = get_topic(topic)
            agent = topic_row.get("agent") if topic_row else None
        if not agent:
            return JSONResponse({"ok": False, "error": "no active session"}, status_code=400)
        if not get_agent(agent):
            return JSONResponse({"ok": False, "error": f"agent not found: {agent}"}, status_code=400)
        killed = kill_procs_by_topic(topic, agent=agent, adhoc=False)
        if WORKTREE_ISOLATION_ENABLED:
            from .worktree import cleanup_worktrees

            async def _sweep() -> None:
                try:
                    await cleanup_worktrees(topic)
                except Exception:
                    log.exception("worktree cleanup failed for topic=%s", topic)
            asyncio.create_task(_sweep(), name=f"squid-worktree-sweep-{topic}")
        clear_topic_session(topic, agent)
        log.info("cmd %s topic=%s agent=%s killed=%s", req.command, topic, agent, killed)
        return JSONResponse({"ok": True, "agent": agent})

    if req.command == "journal":
        week_key, week_start, week_end = _current_week()
        path = await _generate_journal(topic, req.agent, week_key, week_start, week_end)
        if path:
            return JSONResponse({"ok": True, "file": str(path)})
        return JSONResponse({"ok": False, "error": "generation failed or no turns"}, status_code=500)

    return JSONResponse({"ok": False, "error": "unknown command"}, status_code=400)


@app.get("/instance/active-process-count")
async def host_active_process_count():
    return JSONResponse({"count": len(list_active_procs())}, headers={"Cache-Control": "no-store"})


@app.get("/processes")
async def processes():
    return JSONResponse(list_active_procs())


class AuthSessionRequest(BaseModel):
    harness: str
    cols: int = Field(default=100, ge=20, le=500)
    rows: int = Field(default=10, ge=5, le=200)
    mode: Literal[AUTH_SESSION_MODES] = "login"
    model: Optional[str] = None
    provider: Optional[str] = None


class AuthSessionInputRequest(BaseModel):
    data: str


class AuthSessionResizeRequest(BaseModel):
    cols: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


@app.post("/auth/session")
async def auth_session_create(req: AuthSessionRequest, request: Request):
    from .auth_sessions import create_session, AuthSessionError, NoLoginCommand

    error = _auth_session_validation_error(req.harness, req.mode, req.model)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    try:
        session = await create_session(req.harness, req.cols, req.rows, mode=req.mode, model=req.model,
                                       **({"provider_id": req.provider} if req.provider is not None else {}))
    except NoLoginCommand as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AuthSessionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if req.mode == "unlock":
        session.browser_session_id = request.state.browser_session_id
    return JSONResponse({
        "id": session.id,
        "harness": session.harness_id,
        "command": session.display_command,
    })


def _browser_auth_session(session_id: str, browser_session_id: str):
    from .auth_sessions import get_session

    session = get_session(session_id)
    if session and session.browser_session_id in (None, browser_session_id):
        return session
    return None


@app.get("/auth/session/{session_id}")
async def auth_session_status(session_id: str, request: Request):
    session = _browser_auth_session(session_id, request.state.browser_session_id)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"state": session.state, "returncode": session.returncode},
                        headers={"Cache-Control": "no-store"})


@app.get("/auth/session/{session_id}/events")
async def auth_session_events(session_id: str, request: Request):
    from .auth_sessions import stream_events

    session = _browser_auth_session(session_id, request.state.browser_session_id)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def event_stream() -> AsyncGenerator[str, None]:
        async for chunk in stream_events(session):
            yield sse_event("data", base64.b64encode(chunk).decode("ascii"))
        yield sse_event("exit", str(session.returncode if session.returncode is not None else -1))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/auth/session/{session_id}/input")
async def auth_session_input(session_id: str, req: AuthSessionInputRequest, request: Request):
    from .auth_sessions import write_input, AuthSessionError

    session = _browser_auth_session(session_id, request.state.browser_session_id)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        write_input(session, req.data.encode())
    except AuthSessionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/auth/session/{session_id}/resize")
async def auth_session_resize(session_id: str, req: AuthSessionResizeRequest, request: Request):
    from .auth_sessions import resize

    session = _browser_auth_session(session_id, request.state.browser_session_id)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    resize(session, req.cols, req.rows)
    return JSONResponse({"ok": True})


@app.post("/auth/session/{session_id}/cancel")
async def auth_session_cancel(session_id: str, request: Request):
    from .auth_sessions import cancel_session

    session = _browser_auth_session(session_id, request.state.browser_session_id)
    ok = bool(session) and await cancel_session(session_id)
    return JSONResponse({"ok": ok})


@app.get("/queue")
async def queued():
    return JSONResponse(dispatcher.all_queued_items())


def _gauge_authed(gauge_type: str, provider: Provider) -> Optional[bool]:
    """Whether the account behind a gauge is authenticated — a provider fact
    (credentials/API key), never a harness one (ADR-0028)."""
    if gauge_type == "claude":
        return bool(creds.get_org_id() and creds.get_session_key())
    if gauge_type == "codex":
        codex_auth = creds.get_codex_cli_auth()
        return bool(codex_auth.get("access_token") or creds.get_codex_token())
    if gauge_type == "cursor":
        return bool(creds.get_cursor_token())
    if gauge_type in _BALANCE_GAUGES:
        try:
            return bool(provider.resolved_api_key())
        except ValueError:
            return False
    if gauge_type == "static":
        return True
    return None


def _uses_dedicated_gauge(provider: Provider) -> bool:
    return provider.dedicated_login and (
        (provider.subscription_harness, provider.gauge.type)
        in {("codex", "codex"), ("claudecode", "claude")}
    )


def _provider_health(provider: Provider) -> dict:
    info = provider.public_dict()
    dedicated_gauge = _uses_dedicated_gauge(provider)
    if provider.dedicated_login:
        if not dedicated_gauge:
            info["gauge"] = {"type": "none"}
        try:
            harness = info["subscription_harness"]
            home = codex_subscription.home_for(provider.id, harness)
            info["codex_home"] = str(home)
            account = None
            if home.is_dir():
                codex_subscription.ensure_home(provider.id, harness)
                with codex_subscription.lock(home, login=False):
                    account = codex_subscription.account_identity(home, harness)
            info["codex_signed_in"] = bool(account)
            info["subscription_fresh_only"] = account == codex_subscription.CLAUDE_UNVERIFIED
        except codex_subscription.ClaudeIdentityUnavailable:
            info["subscription_needs_verification"] = True
        except codex_subscription.SubscriptionBusy:
            info["subscription_busy"] = True
        except (OSError, ValueError):
            info["codex_home_unavailable"] = True
    info["gauge_authed"] = (bool(info.get("codex_signed_in")) if dedicated_gauge
                            else _gauge_authed(info["gauge"]["type"], provider))
    return info


@app.get("/health")
async def health():
    from .providers import PROVIDERS

    # Use one catalog snapshot across the CLI probes, which can outlast a reload.
    catalog = PROVIDERS.copy()
    harnesses = list_harnesses()
    provider_info = await asyncio.gather(*(
        asyncio.to_thread(_provider_health, provider)
        for provider in catalog.values()
    ))
    providers = dict(zip(catalog, provider_info))
    return JSONResponse({
        "status": "ok",
        "boot_time": BOOT_TIME,
        "version": SQUID_VERSION,
        "squid_home": SQUID_HOME,
        "updates": {
            "install_on_restart": UPDATES_INSTALL_ON_RESTART,
            "can_install_on_restart": _pipx_upgrade_unavailable_reason() is None,
        },
        "harnesses": harnesses,
        "providers": providers,
        **get_usage_stats(),
    })


@app.get("/history")
async def history(offset: int = 0, limit: int = 5, topic: Optional[str] = None,
                  agent: Optional[str] = None, adhoc: Optional[bool] = None,
                  flow_route: Optional[str] = None, bookmarked: bool = False,
                  marked_bad: bool = False, topic_subtree: bool = False):
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    payload = await asyncio.to_thread(
        list_history,
        topic=topic,
        agent=agent,
        adhoc=adhoc,
        offset=offset,
        limit=limit,
        flow_route=canonical_flow_route(flow_route),
        bookmarked=bookmarked,
        marked_bad=marked_bad,
        topic_subtree=topic_subtree,
    )
    await asyncio.to_thread(_prepare_history_tools, payload)
    return JSONResponse(payload)


@app.get("/history/by-ids")
async def history_by_ids(ids: str = ""):
    if not ids.strip():
        return JSONResponse({"items": [], "total": 0})
    try:
        parsed = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"error": "invalid ids"}, status_code=400)
    parsed = parsed[:200]  # cap to prevent abuse
    payload = await asyncio.to_thread(list_history_by_ids, parsed)
    await asyncio.to_thread(_prepare_history_tools, payload)
    return JSONResponse(payload)


@app.get("/history/around")
async def history_around(msg_id: Optional[int] = None, flow_run_id: Optional[str] = None,
                         before: int = 20, after: int = 20,
                         direction: Optional[str] = None,
                         cursor_completed_at: Optional[str] = None,
                         cursor_id: Optional[int] = None,
                         limit: int = 20, topic: Optional[str] = None,
                         agent: Optional[str] = None, adhoc: Optional[bool] = None,
                         flow_route: Optional[str] = None, bookmarked: bool = False,
                         marked_bad: bool = False, topic_subtree: bool = False):
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    if direction is not None and direction not in {"older", "newer"}:
        return JSONResponse({"error": "invalid direction"}, status_code=400)
    payload = await asyncio.to_thread(
        list_history_around,
        msg_id=msg_id,
        flow_run_id=flow_run_id,
        before=before,
        after=after,
        direction=direction,
        cursor_completed_at=cursor_completed_at,
        cursor_id=cursor_id,
        limit=limit,
        topic=topic,
        agent=agent,
        adhoc=adhoc,
        flow_route=canonical_flow_route(flow_route),
        bookmarked=bookmarked,
        marked_bad=marked_bad,
        topic_subtree=topic_subtree,
    )
    await asyncio.to_thread(_prepare_history_tools, payload)
    return JSONResponse(payload)


async def _realtime_history_query(message_type: str, payload: object) -> dict:
    """Bounded history reads; never use the durable replay cursor for paging."""
    if message_type == "history.check":
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if not (
            isinstance(ids, list) and len(ids) <= 100
            and all(type(value) is int and 0 < value <= 2**53 - 1 for value in ids)
            and all(type(payload.get(key, False)) is bool for key in ("bookmarked", "marked_bad", "chunked"))
        ):
            return {"ok": False, "error": "invalid_ids"}

        def read_exact_history() -> dict:
            data = list_history_by_ids(
                ids, bookmarked=payload.get("bookmarked", False), marked_bad=payload.get("marked_bad", False),
            )
            _prepare_history_tools(data)
            return {"ok": True, **data}

        return await asyncio.to_thread(read_exact_history)

    common = {"topic", "agent", "adhoc", "flow_route", "bookmarked", "marked_bad", "topic_subtree"}
    around = {"msg_id", "flow_run_id", "before", "after", "direction", "cursor_completed_at", "cursor_id", "limit"}
    allowed = common | ({"offset", "limit"} if message_type == "history.page" else around)
    if not isinstance(payload, dict) or payload.keys() - allowed:
        return {"ok": False, "error": "invalid_history_query"}
    integers = {"offset": (0, 1000000), "limit": (1, 100), "before": (0, 100),
                "after": (0, 100), "msg_id": (1, 2**53 - 1), "cursor_id": (1, 2**53 - 1)}
    for key, value in payload.items():
        if key in integers:
            low, high = integers[key]
            if type(value) is not int or not low <= value <= high:
                return {"ok": False, "error": "invalid_history_query"}
        elif key in {"adhoc", "bookmarked", "marked_bad", "topic_subtree"}:
            if type(value) is not bool:
                return {"ok": False, "error": "invalid_history_query"}
        elif not isinstance(value, str) or len(value) > 1024:
            return {"ok": False, "error": "invalid_history_query"}
    handler = history if message_type == "history.page" else history_around
    response = await handler(**payload)
    data = json.loads(response.body)
    result = {"ok": True, **data} if response.status_code == 200 else {
        "ok": False, "error": data.get("error", "invalid_history_query")}
    # Leave room for the correlated envelope. Chunked transfers are separate work.
    if len(json.dumps(result).encode("utf-8")) > _REALTIME_MAX_FRAME_BYTES - 4096:
        return {"ok": False, "error": "history_result_too_large"}
    return result


_REALTIME_HISTORY_QUERY_LIMIT = 2
_REALTIME_HISTORY_QUERY_TIMEOUT = 5.0


def _history_query_frame(request_id: str, result: dict) -> dict:
    frame = {"v": 1, "type": "command.result", "request_id": request_id, "payload": result}
    # Match WebSocket.send_json's encoding and measure the entire wire envelope.
    if len(json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _REALTIME_MAX_FRAME_BYTES:
        frame["payload"] = {"ok": False, "error": "history_result_too_large"}
    return frame


async def _run_history_query(frame: dict, outbound, principal: str, last_acked_cursor: int) -> None:
    message_type, payload = frame["type"], frame.get("payload")
    worker = asyncio.create_task(_realtime_history_query(message_type, payload))
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(worker), _REALTIME_HISTORY_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"ok": False, "error": "history_query_timeout"}
        except Exception:
            log.exception("Realtime history query failed")
            result = {"ok": False, "error": "history_query_failed"}
        if message_type == "history.check":
            chunked = result.get("ok") and payload.get("chunked", False) and len(payload["ids"]) == 1
            await _send_history_check_result(outbound, frame["request_id"], result, chunked, principal, last_acked_cursor)
        else:
            await _realtime_send(outbound, _history_query_frame(frame["request_id"], result), principal, last_acked_cursor)
        # A timed-out to_thread read cannot be stopped. Keep its admission slot
        # occupied until it finishes, and never emit its late result.
        await asyncio.gather(worker, return_exceptions=True)
    finally:
        worker.cancel()


def _is_squid_worktree_path(value: object) -> bool:
    return isinstance(value, str) and bool(re.search(r"(^|/)\.squid/worktrees/", value))


def _tool_source_repo(tool: dict) -> str:
    for key in ("source", "repo", "cwd"):
        value = tool.get(key)
        if isinstance(value, str) and value and not _is_squid_worktree_path(value):
            return value
    return ""


_TOOL_PATCH_FIELDS = {
    "Diff": ("diff",),
    "Edit": ("old", "new"),
    "MultiEdit": ("edits",),
    "Write": ("content",),
    "Bash": ("command",),
}


def _defer_history_patches(tools: list) -> bool:
    from .git_changes import MAX_INLINE_DIFF_BYTES, summarize_stored_diff

    patches = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        fields = ("diff",) if name == "GitDiff" else _TOOL_PATCH_FIELDS.get(name, ())
        values = [tool[field] for field in fields if tool.get(field)]
        if values:
            # Context is itself a JSON string in the history response.
            wire_size = len(json.dumps(json.dumps(values)))
            patches.append((wire_size, index, fields))

    remaining = sum(size for size, _, _ in patches)
    budget = min(MAX_INLINE_DIFF_BYTES, _REALTIME_MAX_FRAME_BYTES // 4)
    changed = False
    for size, index, fields in sorted(patches, reverse=True):
        if remaining <= budget:
            break
        tool = tools[index]
        if tool.get("name") == "GitDiff":
            changed = summarize_stored_diff(tool) or changed
        else:
            for field in fields:
                tool.pop(field, None)
            if tool.get("name") != "Bash":
                # Bash commands remain available through the thought trace.
                tool["detail_index"] = index
            changed = True
        remaining -= size
    return changed


def _prepare_history_tools(payload: dict) -> None:
    for item in payload.get("items") or []:
        context = item.get("context")
        if not context:
            continue
        try:
            tools = json.loads(context) if isinstance(context, str) else context
        except Exception:
            continue
        if not isinstance(tools, list):
            continue

        topic = item.get("topic") or "default"
        wt_key = str(item.get("id") or "")
        if not wt_key:
            continue
        records_by_key: dict[str, list[dict]] = {}
        changed = _defer_history_patches(tools)

        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") not in {"GitDiff", "WorktreeSync"}:
                continue
            status = tool.get("worktree_status") if tool.get("name") == "GitDiff" else tool.get("status")
            if not status:
                continue

            # A "worktree blocker" GitDiff entry describes another turn's
            # still-unresolved worktree, not this message's own — look its
            # state up under the original turn's msg_id, not this item's id.
            tool_wt_key = str(tool.get("worktree_msg_id") or "") or wt_key
            if tool_wt_key not in records_by_key:
                records_by_key[tool_wt_key] = get_worktrees(topic, tool_wt_key)
            records = records_by_key[tool_wt_key]

            source_repo = _tool_source_repo(tool)
            worktree_repo = tool.get("worktree_repo")
            rec = next((
                row for row in records
                if (source_repo and str(Path(row["repo_root"]).resolve()) == str(Path(source_repo).resolve()))
                or (worktree_repo and row.get("worktree_path") == worktree_repo)
            ), None)

            if rec:
                live_status = rec.get("status") or status
                if tool.get("name") == "GitDiff":
                    tool["worktree_status"] = live_status
                else:
                    tool["status"] = live_status
                tool.setdefault("worktree_repo", rec.get("worktree_path"))
                if rec.get("integration_worktree_path"):
                    tool["integration_worktree_path"] = rec["integration_worktree_path"]
                changed = True
                continue

            if source_repo:
                try:
                    from .worktree import integration_worktree_path, worktree_path
                    repo_root = Path(source_repo).resolve()
                    wt_path = worktree_path(repo_root, topic, tool_wt_key)
                    integration_path = integration_worktree_path(repo_root, topic, tool_wt_key)
                    if wt_path.exists() or integration_path.exists():
                        tool.setdefault("worktree_repo", str(wt_path))
                        if integration_path.exists():
                            tool["integration_worktree_path"] = str(integration_path)
                        changed = True
                        continue
                except Exception:
                    pass

            if tool.get("name") == "GitDiff":
                tool["worktree_status"] = "synced"
            else:
                tool["status"] = "synced"
            tool["already_resolved"] = True
            changed = True

        if changed:
            item["context"] = json.dumps(tools)


@app.get("/search")
async def search(q: str, limit: int = 100, topic: Optional[str] = None,
                 agent: Optional[str] = None, adhoc: Optional[bool] = None,
                 role: str = "assistant", bookmarked: bool = False,
                 flow_route: Optional[str] = None, marked_bad: bool = False,
                 topic_subtree: bool = False, operator: str = "and"):
    if operator not in {"and", "or"}:
        return JSONResponse({"error": "operator must be and or or"}, status_code=400)
    if topic is not None:
        normalized = _normalize_topic_response(topic)
        if isinstance(normalized, JSONResponse):
            return normalized
        topic = normalized
    limit = min(limit, 100)
    flow_route = canonical_flow_route(flow_route)
    search_history = search_prompts if role == "user" else search_messages
    payload = await asyncio.to_thread(
        search_history, q=q, topic=topic, agent=agent, adhoc=adhoc, limit=limit,
        bookmarked=bookmarked, flow_route=flow_route, marked_bad=marked_bad,
        topic_subtree=topic_subtree, operator=operator,
    )
    await asyncio.to_thread(_prepare_history_tools, payload)
    return JSONResponse(payload)


class ComposerHistoryRequest(BaseModel):
    kind: Literal["route", "prompt"]
    key: str = Field(min_length=1)
    value: str = ""
    hidden: bool
    updated_at: int = Field(ge=0, le=2**53 - 1)
    submission_id: Optional[str] = Field(default=None, min_length=1, max_length=80)


@app.get("/prompts/recent")
async def prompts_recent(limit: int = 50):
    entries, agents, history, routes = await asyncio.gather(
        asyncio.to_thread(get_recent_prompt_entries, max(1, min(limit, 200))),
        asyncio.to_thread(_public_agent_map),
        asyncio.to_thread(get_composer_history),
        asyncio.to_thread(get_composer_route_entries),
    )
    return JSONResponse({
        "items": [entry["text"] for entry in entries],
        "entries": entries, "agents": agents, "history": history, "routes": routes,
    })


@app.post("/prompts/history")
async def composer_history_update(req: ComposerHistoryRequest):
    await asyncio.to_thread(
        update_composer_history, req.kind, req.key, req.value, req.hidden, req.updated_at, req.submission_id,
    )
    return {"ok": True}


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
    pending_assistant = row.get("status") == "pending" and row.get("role") == "assistant"
    event_snapshot = get_run_event_snapshot(msg_id) if pending_assistant else {}
    recovered_content = get_completed_run_text(msg_id)
    if (
        pending_assistant
        and recovered_content
    ):
        recovered_status_raw = row.get("status_raw") or event_snapshot.get("status_raw") or get_completed_run_status_raw(msg_id)
        update_assistant_message(
            msg_id,
            recovered_content,
            row.get("session_id") or event_snapshot.get("session_id"),
            "done",
            context=row.get("context") or event_snapshot.get("context"),
            status_raw=recovered_status_raw,
            only_if_pending=True,
        )
        row = get_message(msg_id)
    elif pending_assistant and event_snapshot:
        content = event_snapshot.get("text")
        status_raw = event_snapshot.get("status_raw")
        context = event_snapshot.get("context")
        session_id = event_snapshot.get("session_id")
        if content and len(content) > len(row.get("content") or ""):
            row["content"] = content
        if status_raw and len(status_raw) > len(row.get("status_raw") or ""):
            row["status_raw"] = status_raw
        if context and not row.get("context"):
            row["context"] = context
        if session_id and not row.get("session_id"):
            row["session_id"] = session_id
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
                elif event_type in {"stats", "status", "tool", "loading", "processing", "queued", "meta"}:
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
    from .flow import expected_row_count
    rows = get_flow_run_messages(flow_run_id)
    new_rows = [r for r in rows if r["id"] > after_id]

    expected_rows = expected_row_count(rows[0]["flow_route"]) if rows else 0
    last_status = rows[-1]["status"] if rows else None
    complete = (
        not rows
        or last_status in ("error", "cancelled")
        or (not expected_rows)
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
async def topics_list(include_agent_history: bool = False):
    db_topics = get_topics_summary(include_agent_history=include_agent_history)
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
    try:
        provider_policy.rule_from_memory(req.content)
    except provider_policy.InvalidPolicy as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(write_topic_memory(topic, req.content))


@app.post("/topics/{topic}/memory/squid/seed")
async def seed_topic_memory_route(topic: str):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    return JSONResponse(ensure_topic_memory_placeholder(topic))


@app.put("/topics/{topic}/memory/squid/code-roots")
async def put_topic_memory_code_roots_route(topic: str, req: TopicMemoryCodeRootsRequest):
    topic = _normalize_topic_response(topic)
    if isinstance(topic, JSONResponse):
        return topic
    try:
        return JSONResponse(write_topic_memory_squid_code_roots(
            topic,
            code_roots=req.code_roots,
            code_roots_skipped=req.code_roots_skipped,
        ))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


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
    if not get_agent(agent):
        return JSONResponse({"error": f"agent not found: {agent}"}, status_code=404)
    stored = get_topic_session(topic, agent)
    if not stored:
        return JSONResponse({"session_id": None, "cwd": None, "session_turn_count": 0})
    injected_context = get_session_injected_context(stored["session_id"])
    return JSONResponse({"session_id": stored["session_id"], "cwd": stored["cwd"],
                         "session_turn_count": get_session_turn_count(stored["session_id"]),
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
    conflicts = {}
    if WORKTREE_ISOLATION_ENABLED:
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


@app.get('/chat/{msg_id}/context')
async def turn_context_view(msg_id: int):
    from .turn_context import recorded_context
    if not await asyncio.to_thread(get_message, msg_id):
        return JSONResponse({'error': 'message not found'}, status_code=404)
    return JSONResponse(await asyncio.to_thread(recorded_context, msg_id))


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


@app.get("/config/realtime")
async def get_realtime_config(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration reads are not allowed"}, status_code=403)
    return JSONResponse({"transport": REALTIME_TRANSPORT}, headers={"Cache-Control": "no-store"})


@app.put("/config/yaml")
async def update_config_yaml(req: ConfigRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin configuration writes are not allowed"}, status_code=403)
    try:
        parsed = _validate_config_content(req.content)
        from .providers import _DEFAULT_PROVIDERS
        proposed = parsed.get("providers") or _DEFAULT_PROVIDERS
        affected = []
        incompatible = []
        for agent in list_agents():
            harness, provider_id = split_agent_ref(agent["harness"], agent.get("provider"))
            if not provider_id:
                from .harnesses import default_provider_for
                provider_id = default_provider_for(harness)
            old = get_provider(provider_id)
            raw = proposed.get(provider_id)
            candidate = _validate_provider(provider_id, raw) if raw else None
            dedicated = bool(candidate and candidate.dedicated_login)
            if dedicated and (harness != candidate.subscription_harness or agent.get("home_mode") == "blank_home"):
                incompatible.append(agent["name"])
            if dedicated != bool(old and old.dedicated_login):
                affected.append(agent["name"])
        if incompatible:
            raise ValueError("Dedicated subscription login is incompatible with: " + ", ".join(incompatible))
        revision = write_config_text(req.content, req.revision)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _cfg.clear()
    _cfg.update(parsed)
    reload_providers()
    for agent in affected:
        codex_subscription.invalidate_agent(agent)
        clear_agent_sessions(agent)
    _LOCALFILE_ROOTS[:] = _localfile_roots_from(parsed)

    # Re-sync pi models.json for every pi agent — a provider's base_url
    # may have changed in this config update even if no agent was touched.
    await asyncio.to_thread(_sync_pi_agents_models_store)

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
        resolved = resolve_agent(harness, provider)
        from .effort import effort_args
        effort_args(harness, resolved.provider.id, req.model, req.effort)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Existing agent's home_mode, if any -- read before upsert_agent() so a
    # brand-new agent (no prior row) never counts as a "change" that would
    # try to clear sessions that can't exist yet.
    prior = get_agent(req.name)
    home_mode = req.home_mode or (prior or {}).get("home_mode", "user_home")
    if resolved.provider.dedicated_login and home_mode != "user_home":
        return JSONResponse({"error": "Dedicated subscription login requires User Home"}, status_code=400)
    key_changed = upsert_agent(req.name, harness, provider, req.model, req.cwd, req.effort)
    home_mode_changed = False
    if req.home_mode is not None:
        if prior is not None and prior.get("home_mode", "user_home") != req.home_mode:
            home_mode_changed = True
        set_agent_home_mode(req.name, req.home_mode)
    if key_changed or home_mode_changed:
        codex_subscription.invalidate_agent(req.name)
    sessions_cleared = clear_agent_sessions(req.name) if (key_changed or home_mode_changed) else []
    # If this agent used to point pi at a different non-standard provider,
    # drop the now-orphaned models.json entry before syncing the new one.
    if prior and prior.get("harness") == "pi" and prior.get("provider") != provider:
        remove_pi_models_store(prior["provider"], prior.get("model"))
    # Sync pi models.json so non-standard providers can route to their
    # custom endpoints (pi ignores env-var base URLs for providers it
    # doesn't natively know).
    resolved.sync_pi_provider(req.model)
    return JSONResponse({"ok": True, "sessions_cleared": sessions_cleared})


@app.delete("/config/agents/{name}")
async def remove_agent(name: str):
    prior = get_agent(name)
    deleted = delete_agent(name)
    if deleted:
        codex_subscription.invalidate_agent(name)
    if deleted and prior and prior.get("harness") == "pi":
        remove_pi_models_store(prior["provider"], prior.get("model"))
    return JSONResponse({"ok": deleted})


@app.put("/config/agents/{name}/home-mode")
async def set_agent_home_mode_route(name: str, req: AgentHomeModeRequest):
    agent_config = get_agent(name)
    if not agent_config:
        return JSONResponse({"error": f"agent not found: {name}"}, status_code=404)
    from .harnesses import default_provider_for
    harness, provider = split_agent_ref(agent_config.get("harness"), agent_config.get("provider"))
    runtime_provider = get_provider(provider or default_provider_for(harness))
    if runtime_provider and runtime_provider.dedicated_login and req.home_mode != "user_home":
        return JSONResponse({"error": "Dedicated subscription login requires User Home"}, status_code=400)
    # A resumable session's transcript lives at a $HOME-relative path, so
    # flipping home_mode orphans every session stored for this agent the
    # same way changing harness/model/cwd does -- clear them the same way.
    changed = get_agent_home_mode(name) != req.home_mode
    set_agent_home_mode(name, req.home_mode)
    if changed:
        codex_subscription.invalidate_agent(name)
    sessions_cleared = clear_agent_sessions(name) if changed else []
    return JSONResponse({"ok": True, "sessions_cleared": sessions_cleared})


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
    flow: str = "all",
    status: str = "",
    tz_offset_minutes: int = 0,
    chart_metrics: str = "",
    chart_aggs: str = "",
    anchor: str = "",
):
    chart_series = _parse_chart_series(chart_metrics, chart_aggs)
    if period == "turn":
        return JSONResponse(get_stats_by_turn(days=days, hours=hours, agent=agent, topic=topic, adhoc=adhoc, flow=flow, status=status, anchor=anchor or None))
    if group == "time" and breakdown in {"agent", "agent_session", "topic_agent", "topic_agent_session"}:
        return JSONResponse(get_stats_by_breakdown(
            period=period,
            days=days,
            agent=agent,
            topic=topic,
            adhoc=adhoc,
            flow=flow,
            status=status,
            tz_offset_minutes=tz_offset_minutes,
            breakdown=breakdown,
            # Breakdown series are dimension values (topic/agent), not metrics —
            # only the first requested metric is meaningful here.
            chart_series=chart_series[:1],
            anchor=anchor or None,
        ))
    if group == "topic":
        return JSONResponse(get_stats_by_topic(days=days, agent=agent, topic=topic, adhoc=adhoc, flow=flow, anchor=anchor or None))
    if group == "agent":
        return JSONResponse(get_stats_by_agent(days=days, agent=agent, topic=topic, adhoc=adhoc, flow=flow, anchor=anchor or None))
    return JSONResponse(get_aggregated_stats(
        period=period,
        days=days,
        agent=agent,
        topic=topic,
        adhoc=adhoc,
        flow=flow,
        status=status,
        tz_offset_minutes=tz_offset_minutes,
        chart_series=chart_series,
        anchor=anchor or None,
    ))


@app.post("/stats/quota-delta")
async def record_quota_delta(req: QuotaDeltaRequest):
    if not save_quota_delta(req.session_id, req.before, req.after):
        return JSONResponse({"error": "Quota unavailable for this subscription provider"}, status_code=409)
    return JSONResponse({"ok": True})


class MsgQuotaSnapshotRequest(BaseModel):
    before: float
    after: float


class RevertRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    file_path: Optional[str] = None


class WorktreeDiscardRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    force: bool = False


class AnnotationRequest(BaseModel):
    msg_id: int
    kind: str = Field(..., min_length=1)
    payload: dict = Field(default_factory=dict)


@app.post("/chat/{msg_id}/quota-delta")
async def record_msg_quota_delta(msg_id: int, req: MsgQuotaSnapshotRequest):
    if not update_message_quota_snapshot(msg_id, req.before, req.after):
        return JSONResponse({"error": "Quota unavailable for this subscription provider"}, status_code=409)
    return JSONResponse({"ok": True})


@app.get("/chat/{msg_id}/diff-revert-status")
async def diff_revert_status(msg_id: int, repo: str, request: Request):
    if _validate_repo_path(repo) is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)
    async with request.app.state.diff_revert_semaphore:
        gitdiff = await asyncio.to_thread(get_message_gitdiff, msg_id, repo)
        blocked = _worktree_diff_blocked(gitdiff)
        if blocked:
            return JSONResponse(blocked)
        eligibility = await asyncio.to_thread(get_diff_revert_eligibility, msg_id, repo)
        if not eligibility:
            return JSONResponse({"error": "diff not found"}, status_code=404)
        return JSONResponse(eligibility)


async def _worktree_record_or_existing_paths(topic: str, wt_key: str, repo_root: Path) -> tuple[Optional[dict], bool]:
    rows = await asyncio.to_thread(get_worktrees, topic, wt_key)
    rec = next((row for row in rows if Path(row["repo_root"]).resolve() == repo_root), None)
    if rec:
        return rec, True

    from .worktree import integration_worktree_path, worktree_path
    if worktree_path(repo_root, topic, wt_key).exists() or integration_worktree_path(repo_root, topic, wt_key).exists():
        return {"status": "missing_registry"}, True
    return None, False


@app.get("/chat/{msg_id}/worktree/status")
async def worktree_blocker_status(msg_id: int, topic: str, repo: str):
    """Side-effect-free status read, used by the auto-resolve client to check
    what actually happened when the SSE stream drops before delivering the
    final resolve_result event (the server-side resolve may have already
    completed by then)."""
    normalized_topic = _normalize_topic_response(topic)
    if isinstance(normalized_topic, JSONResponse):
        return normalized_topic
    repo_root = _validate_repo_path(repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

    rec, found = await _worktree_record_or_existing_paths(normalized_topic, str(msg_id), repo_root)
    if not found:
        return JSONResponse({"ok": True, "status": "synced"})
    return JSONResponse({"ok": True, "status": rec.get("status") or "pending"})


@app.post("/chat/{msg_id}/worktree/discard")
async def discard_worktree_blocker(msg_id: int, req: WorktreeDiscardRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic
    repo_root = _validate_repo_path(req.repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

    wt_key = str(msg_id)
    rec, found = await _worktree_record_or_existing_paths(topic, wt_key, repo_root)
    if not found:
        log.info("worktree discard skipped; already gone topic=%s msg_id=%s repo=%s", topic, msg_id, repo_root)
        return JSONResponse({"ok": True, "already_resolved": True})

    from .runners import get_active_msg_ids
    if msg_id in get_active_msg_ids():
        return JSONResponse({"error": "worktree is still running"}, status_code=409)

    from .worktree import remove_worktree
    await asyncio.to_thread(remove_worktree, repo_root, topic, wt_key)
    # Keep the row (status="discarded") instead of deleting it — a fully
    # deleted row is indistinguishable from "never had a problem" once
    # _prepare_history_tools falls back to reporting "synced" for
    # it, which would misrepresent a discarded turn as having landed cleanly.
    await asyncio.to_thread(mark_worktree_status, topic, wt_key, str(repo_root), "discarded")
    log.info("worktree discarded topic=%s msg_id=%s repo=%s status=%s", topic, msg_id, repo_root, rec.get("status"))
    return JSONResponse({"ok": True})


@app.post("/chat/{msg_id}/worktree/retry")
async def retry_worktree_resolution(msg_id: int, req: WorktreeDiscardRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic
    repo_root = _validate_repo_path(req.repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

    wt_key = str(msg_id)
    rec, found = await _worktree_record_or_existing_paths(topic, wt_key, repo_root)
    if not found:
        log.info("worktree retry skipped; already gone topic=%s msg_id=%s repo=%s", topic, msg_id, repo_root)
        return JSONResponse({"ok": True, "already_resolved": True})

    from .runners import get_active_msg_ids
    if msg_id in get_active_msg_ids():
        return JSONResponse({"error": "worktree is still running"}, status_code=409)

    from .worktree import ConflictMarkersRemainError, promote_resolved_integration_worktree, remove_worktree, sync_after_turn
    if rec.get("status") in {"pending", "active"}:
        try:
            conflict_files = await asyncio.to_thread(sync_after_turn, repo_root, topic, wt_key, msg_id)
        except RuntimeError as exc:
            await asyncio.to_thread(mark_worktree_status, topic, wt_key, str(repo_root), "promotion_failed")
            return JSONResponse({"error": str(exc), "status": "promotion_failed"}, status_code=409)
        if conflict_files:
            await asyncio.to_thread(mark_worktree_status, topic, wt_key, str(repo_root), "conflict")
            return JSONResponse({
                "error": "worktree sync conflict",
                "status": "conflict",
                "conflicts": conflict_files,
            }, status_code=409)
        await asyncio.to_thread(remove_worktree, repo_root, topic, wt_key)
        await asyncio.to_thread(delete_worktree, topic, wt_key, str(repo_root))
        log.info("worktree retry synced topic=%s msg_id=%s repo=%s status=%s", topic, msg_id, repo_root, rec.get("status"))
        return JSONResponse({"ok": True})

    try:
        await asyncio.to_thread(promote_resolved_integration_worktree, repo_root, topic, wt_key, req.force)
    except ConflictMarkersRemainError as exc:
        return JSONResponse({"error": str(exc), "conflict_markers_remain": True, "files": exc.files}, status_code=409)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    # Keep the row (status="resolved") rather than deleting it — otherwise
    # this collapses back to indistinguishable-from-never-conflicted "synced"
    # the same way a deleted "discarded" row did (see that fix's comment).
    await asyncio.to_thread(mark_worktree_status, topic, wt_key, str(repo_root), "resolved")
    log.info("worktree resolved topic=%s msg_id=%s repo=%s status=%s", topic, msg_id, repo_root, rec.get("status"))
    return JSONResponse({"ok": True})


_AUTO_RESOLVE_PROMPT_TEMPLATE = """\
A merge conflict occurred integrating the original turn above (pinned \
context) with the current state of the repository ({repo_root}). Your \
current working directory is already an isolated worktree containing the \
conflicted files below — resolve them in place. Do not `cd` to {repo_root} \
or any other checkout of this repo; that is a different, unrelated copy.

Conflicted files: {file_list}

{conflict_block}

For each file, merge the intent of both sides — don't just pick one side \
wholesale unless the other side is genuinely superseded. Remove all conflict \
markers and leave the file in a syntactically valid state, then `git add` it. \
Do not commit.

If any file's two sides make incompatible changes to the same logic and you \
can't safely reconcile them, leave that file's markers in place and say so \
plainly in your response instead of guessing."""


@app.post("/chat/{msg_id}/worktree/auto-resolve")
async def auto_resolve_worktree_conflict(msg_id: int, req: WorktreeDiscardRequest):
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return topic
    repo_root = _validate_repo_path(req.repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

    wt_key = str(msg_id)
    rec, found = await _worktree_record_or_existing_paths(topic, wt_key, repo_root)
    if not found:
        log.info("worktree auto-resolve skipped; already gone topic=%s msg_id=%s repo=%s", topic, msg_id, repo_root)
        return JSONResponse({"ok": True, "already_resolved": True})
    if rec.get("status") != "conflict":
        return JSONResponse({"error": "auto-resolve is only available while conflicted"}, status_code=409)
    from . import task_workspaces
    try:
        await asyncio.to_thread(task_workspaces.check_destination, repo_root, topic, wt_key)
    except RuntimeError as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)

    from .runners import get_active_msg_ids
    if msg_id in get_active_msg_ids():
        return JSONResponse({"error": "worktree is still running"}, status_code=409)

    from .worktree import (
        integration_worktree_path, integration_conflicts, promote_resolved_integration_worktree,
        conflict_context_summary,
    )
    conflicts = await asyncio.to_thread(integration_conflicts, repo_root, topic, wt_key)
    if not conflicts:
        return JSONResponse({"error": "no conflicted files found"}, status_code=409)

    turn = await asyncio.to_thread(get_message, msg_id)
    conflict_block = await asyncio.to_thread(conflict_context_summary, repo_root, topic, wt_key, conflicts)
    prompt = _AUTO_RESOLVE_PROMPT_TEMPLATE.format(
        repo_root=repo_root,
        file_list=", ".join(conflicts),
        conflict_block=conflict_block,
    )
    integration_wt = integration_worktree_path(repo_root, topic, wt_key)

    # Pin the original turn (its request/response, plus its own gitdiff summary
    # via get_messages_by_ids) as real context_history instead of retyping it
    # into the prompt — same mechanism as any other adhoc pinned-context turn.
    prepared = await _prepare_chat_turn(
        message=prompt,
        topic=topic,
        agent=(turn or {}).get("agent"),
        adhoc=True,
        source="diff_viewer",
        override_cwd=str(integration_wt),
        pinned_ids=[msg_id],
    )
    if isinstance(prepared, JSONResponse):
        return prepared

    try:
        await asyncio.to_thread(task_workspaces.bind_recovery, prepared['asst_msg_id'], msg_id, repo_root)
    except RuntimeError as exc:
        update_assistant_message(prepared['asst_msg_id'], str(exc), None, status='error')
        return JSONResponse({'error': str(exc)}, status_code=409)

    async def _stream() -> AsyncGenerator[str, None]:
        # Forward every event live — the UI renders this exactly like a normal
        # chat turn (user bubble + thinking bubble + streamed response) instead
        # of hiding the model call behind a bare button spinner.
        async for event in stream_response(
            prepared["effective_message"], prepared["topic"], prepared["agent"], prepared["backend"], prepared["model"], prepared["cwd"],
            prepared["context_history"], prepared["asst_msg_id"], prepared["response_timeout"],
            effort=prepared.get("effort"), codex_binding=prepared.get("codex_binding"),
            resume_session_id=prepared["resume_session_id"],
            adhoc=prepared["adhoc"],
            lookback=prepared["lookback"],
            code_roots=prepared["code_roots"],
            display_prompt=prepared["display_prompt"],
            source_cwd=prepared["source_cwd"],
            configured_cwd=prepared["configured_cwd"],
            harness=prepared["harness"],
            provider=prepared["provider"],
        ):
            yield event

        remaining = await asyncio.to_thread(integration_conflicts, repo_root, topic, wt_key)
        if remaining:
            log.warning("auto-resolve left conflicts topic=%s msg_id=%s remaining=%s", topic, msg_id, remaining)
            yield sse_event("resolve_result", json.dumps({
                "error": "unresolved conflicts remain",
                "conflicts": remaining,
            }))
            return

        try:
            await asyncio.to_thread(promote_resolved_integration_worktree, repo_root, topic, wt_key)
        except RuntimeError as exc:
            yield sse_event("resolve_result", json.dumps({"error": str(exc)}))
            return

        await asyncio.to_thread(mark_worktree_status, topic, wt_key, str(repo_root), "resolved")
        log.info("worktree auto-resolved topic=%s msg_id=%s repo=%s files=%d", topic, msg_id, repo_root, len(conflicts))
        yield sse_event("resolve_result", json.dumps({"ok": True, "files": conflicts}))

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "X-Squid-Msg-Id": str(prepared["asst_msg_id"]),
            "X-Squid-Agent": prepared["agent"] or "",
            "X-Squid-Provider": prepared["provider"] or "",
        },
    )


async def _drain_auto_resolve_response(
    response: StreamingResponse, *, worktree_msg_id: int, topic: str, repo: str,
) -> None:
    """Run the existing auto-resolve stream to completion for a WS command."""
    event_buffer = ""
    resolve_result = None
    async for chunk in response.body_iterator:
        event_buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        records = event_buffer.split("\n\n")
        event_buffer = records.pop()
        for record in records:
            lines = record.splitlines()
            event_name = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
            if event_name != "resolve_result":
                continue
            data = "\n".join(
                line[5:].lstrip(" ") for line in lines if line.startswith("data:")
            )
            try:
                resolve_result = json.loads(data)
            except (TypeError, ValueError):
                resolve_result = {"error": "invalid auto-resolve result"}
    # Only normal iterator exhaustion reaches this point. Cancellation and
    # unexpected stream errors propagate without claiming the attempt finished.
    repo_root = _validate_repo_path(repo)
    if repo_root is not None:
        rec, found = await _worktree_record_or_existing_paths(topic, str(worktree_msg_id), repo_root)
        status = (rec or {}).get("status") if found else "synced"
        if status not in {"synced", "resolved", "discarded"}:
            turn = await asyncio.to_thread(get_message, worktree_msg_id)
            outcome = resolve_result if isinstance(resolve_result, dict) else {}
            await asyncio.to_thread(insert_realtime_event,
                "worktree.changed", topic, (turn or {}).get("agent"),
                {"repo": str(repo_root), "status": status or "conflict",
                 "auto_resolve_finished": True,
                 **({"error": outcome["error"]} if outcome.get("error") else {}),
                 **({"conflicts": outcome["conflicts"]} if outcome.get("conflicts") else {})},
                worktree_msg_id,
            )


async def _realtime_worktree_auto_resolve(payload: dict) -> dict:
    msg_id = payload.get("msg_id")
    topic = payload.get("topic")
    repo = payload.get("repo")
    if not isinstance(msg_id, int) or not isinstance(topic, str) or not isinstance(repo, str):
        return {"ok": False, "error": "invalid_frame"}
    try:
        req = WorktreeDiscardRequest(topic=topic, repo=repo)
    except Exception as exc:
        return {"ok": False, "error": "invalid_frame", "detail": str(exc)}
    response = await auto_resolve_worktree_conflict(msg_id, req)
    if isinstance(response, JSONResponse):
        body = json.loads(response.body)
        return {
            "ok": response.status_code < 400 and body.get("ok", False),
            **body,
            "status": response.status_code,
        }
    resolve_msg_id = int(response.headers["X-Squid-Msg-Id"])
    resolve_agent = response.headers.get("X-Squid-Agent") or None
    resolve_provider = response.headers.get("X-Squid-Provider") or None
    task = asyncio.create_task(
        _drain_auto_resolve_response(
            response, worktree_msg_id=msg_id, topic=topic, repo=repo,
        ),
        name=f"squid-ws-auto-resolve-{resolve_msg_id}",
    )
    _realtime_chat_tasks.add(task)
    task.add_done_callback(_realtime_chat_tasks.discard)
    return {"ok": True, "msg_id": resolve_msg_id, "worktree_msg_id": msg_id,
            "agent": resolve_agent, "provider": resolve_provider}


_DIFF_PAGE_CHARS = 48 * 1024
_DIFF_PAGE_LINES = 500


def _turn_diff_paths(gitdiff: dict, path: str) -> Optional[tuple[str, Optional[str]]]:
    from .git_changes import unquote_git_path

    file = next((file for file in gitdiff.get("files", []) if file.get("path") == path), None)
    if not file:
        return None
    old_path = file.get("old_path") if file.get("status", "").startswith("R") else None
    if "binary" not in file:
        # Legacy metadata used Git's quoted path format.
        path = unquote_git_path(path)
        old_path = unquote_git_path(old_path) if old_path is not None else None
    return path, old_path


def _read_turn_file_patch(
    repo: str, gitdiff: dict, path: str, *, offset: int = 0, limit: Optional[int] = None,
    observe_prefix: Optional[Callable[[str], None]] = None,
) -> str:
    from .git_changes import diff_for_path, extract_file_diff

    paths = _turn_diff_paths(gitdiff, path)
    if paths is None:
        return ""
    path, old_path = paths
    patch = extract_file_diff(gitdiff.get("diff") or "", path)
    if patch:
        if observe_prefix is not None and offset:
            observe_prefix(patch[:offset])
        return patch[offset:] if limit is None else patch[offset:offset + limit + 1]
    base, head = gitdiff.get("base"), gitdiff.get("head")
    repo_root = _validate_repo_path(repo)
    if not base or not head or repo_root is None:
        return ""
    return diff_for_path(
        repo_root, base, head, path, old_path, offset=offset, limit=limit,
        observe_prefix=observe_prefix,
    )


def _valid_diff_cursor(offset: int, cursor: Optional[str]) -> bool:
    return (offset == 0 and cursor is None) or (
        offset > 0 and cursor is not None and re.fullmatch(r"[a-f0-9]{64}", cursor) is not None
    )


def _diff_page_response(patch: str, offset: int, cursor: Optional[str], prefix_hash: Any) -> JSONResponse:
    # The cursor covers exactly the text already displayed. Git can regenerate
    # a different patch for the same trees if worktree attributes change.
    if offset and (not patch or prefix_hash.hexdigest() != cursor):
        return JSONResponse({"error": "The diff changed. Reload it to continue."}, status_code=409)
    if not patch:
        return JSONResponse({"error": "This turn’s file diff is unavailable"}, status_code=404)

    page = patch[:_DIFF_PAGE_CHARS]
    lines = page.split("\n", _DIFF_PAGE_LINES)
    if len(lines) > _DIFF_PAGE_LINES:
        page = "\n".join(lines[:_DIFF_PAGE_LINES]) + "\n"
    elif len(page) < len(patch) and "\n" in page:
        page = page[:page.rfind("\n") + 1]
    has_more = len(page) < len(patch)
    prefix_hash.update(page.encode("utf-8"))
    return JSONResponse({
        "diff": page,
        "next_offset": offset + len(page) if has_more else None,
        "next_cursor": prefix_hash.hexdigest() if has_more else None,
    })


@app.get("/chat/{msg_id}/diff-file")
async def diff_file(msg_id: int, repo: str, path: str, offset: int = 0, cursor: Optional[str] = None):
    """Read a bounded page of one historical file's patch."""
    if not _valid_diff_cursor(offset, cursor):
        return JSONResponse({"error": "Invalid diff cursor"}, status_code=400)
    gitdiff = await asyncio.to_thread(get_message_gitdiff, msg_id, repo)
    if not gitdiff:
        return JSONResponse({"error": "This turn’s file diff is unavailable"}, status_code=404)
    prefix_hash = hashlib.sha256()
    patch = await asyncio.to_thread(
        _read_turn_file_patch, repo, gitdiff, path, offset=offset, limit=_DIFF_PAGE_CHARS,
        observe_prefix=lambda chunk: prefix_hash.update(chunk.encode("utf-8")),
    )
    return _diff_page_response(patch, offset, cursor, prefix_hash)


def _read_tool_patch(msg_id: int, tool_index: int) -> Optional[str]:
    row = get_message(msg_id)
    if not row or row.get("role") != "assistant":
        return None
    try:
        tools = json.loads(row.get("context") or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(tools, list) or not 0 <= tool_index < len(tools):
        return None
    tool = tools[tool_index]
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if name == "Diff":
        return str(tool.get("diff") or "")

    def lines(prefix: str, value: Any) -> str:
        if not isinstance(value, str) or not value:
            return ""
        return "".join(prefix + line + "\n" for line in value.removesuffix("\n").split("\n"))

    if name == "Write":
        return lines("+ ", tool.get("content") or "")
    if name == "Edit":
        return lines("- ", tool.get("old") or "") + lines("+ ", tool.get("new") or "")
    edits = tool.get("edits")
    if name == "MultiEdit" and isinstance(edits, list):
        return "\n".join(
            lines("- ", edit.get("old_string")) + lines("+ ", edit.get("new_string"))
            for edit in edits if isinstance(edit, dict)
        )
    return None


@app.get("/chat/{msg_id}/diff-tool/{tool_index}")
async def diff_tool(msg_id: int, tool_index: int, offset: int = 0, cursor: Optional[str] = None):
    if not _valid_diff_cursor(offset, cursor):
        return JSONResponse({"error": "Invalid diff cursor"}, status_code=400)
    patch = await asyncio.to_thread(_read_tool_patch, msg_id, tool_index)
    if patch is None:
        return JSONResponse({"error": "This turn’s tool diff is unavailable"}, status_code=404)
    prefix_hash = hashlib.sha256(patch[:offset].encode("utf-8"))
    return _diff_page_response(patch[offset:offset + _DIFF_PAGE_CHARS + 1], offset, cursor, prefix_hash)


def _validate_repo_path(repo: str) -> Optional[Path]:
    """Return resolved Path if repo is an absolute path to a real git repo, else None."""
    try:
        p = Path(repo).resolve()
    except Exception:
        return None
    if not p.is_absolute() or not (p / ".git").exists():
        return None
    return p


def _worktree_diff_blocked(gitdiff: Optional[dict]) -> Optional[dict[str, str]]:
    if not gitdiff or not gitdiff.get("worktree_repo"):
        return None
    status = gitdiff.get("worktree_status")
    if not status or status in {"synced", "resolved", "discarded"}:
        return None
    return {
        f.get("path"): status
        for f in gitdiff.get("files", [])
        if f.get("path")
    }


def _read_turn_revert_patch(repo_root: Path, gitdiff: dict, file_path: str) -> bytes:
    from .git_changes import diff_bytes_for_path, extract_file_diff

    paths = _turn_diff_paths(gitdiff, file_path)
    if paths is None:
        return b""
    path, old_path = paths
    patch = extract_file_diff(gitdiff.get("diff") or "", path)
    if patch and (gitdiff.get("diff_lossless") or "\ufffd" not in patch):
        return patch.encode("utf-8")
    # Older inline patches may already contain replacements. Recover them from
    # the snapshots, or fail rather than writing the replacement to disk.
    base, head = gitdiff.get("base"), gitdiff.get("head")
    if base and head:
        return diff_bytes_for_path(repo_root, base, head, path, old_path)
    return b""


@app.post("/chat/{msg_id}/revert")
async def revert_diff(msg_id: int, req: RevertRequest):
    from .git_changes import apply_reverse_patch

    repo_root = _validate_repo_path(req.repo)
    if repo_root is None:
        return JSONResponse({"error": "invalid repo path"}, status_code=400)

    this_diff = await asyncio.to_thread(get_message_gitdiff, msg_id, req.repo)
    blocked = _worktree_diff_blocked(this_diff)
    if blocked:
        return JSONResponse({"error": "worktree diff is not synced", "status": next(iter(blocked.values()), "pending")}, status_code=400)

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

    if not this_diff:
        return JSONResponse({"error": "GitDiff not found"}, status_code=404)

    reverted: list[str] = []
    failed: list[dict] = []

    for fpath in files_to_revert:
        file_diff = await asyncio.to_thread(_read_turn_revert_patch, repo_root, this_diff, fpath)
        if not file_diff:
            failed.append({'file': fpath, 'error': 'no lossless patch available'})
            continue
        ok, err = await asyncio.to_thread(apply_reverse_patch, repo_root, file_diff)
        if ok:
            reverted.append(fpath)
        else:
            failed.append({'file': fpath, 'error': err})

    if reverted:
        await asyncio.to_thread(record_git_diff_revert, msg_id, req.repo, reverted)
    elif failed:
        return JSONResponse(
            {"ok": False, "reverted": reverted, "failed": failed, "error": "revert failed"},
            status_code=409,
        )

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
    add_bookmark(msg_id)
    return JSONResponse({"ok": True})


@app.delete("/bookmarks/{msg_id}")
async def delete_bookmark(msg_id: int):
    remove_bookmark(msg_id)
    return JSONResponse({"ok": True})


@app.get("/annotations")
async def list_annotations(kind: str = ""):
    return JSONResponse({"items": get_message_annotations(kind or None)})


@app.post("/annotations")
async def create_annotation(req: AnnotationRequest):
    if req.kind not in {"bad_response", "bookmark"}:
        return JSONResponse({"error": "unsupported annotation kind"}, status_code=400)
    msg = get_message(req.msg_id)
    if not msg or msg.get("role") != "assistant":
        return JSONResponse({"error": "assistant message not found"}, status_code=404)
    set_message_annotation(req.msg_id, req.kind, req.payload)
    return JSONResponse({"ok": True})


@app.delete("/annotations/{kind}/{msg_id}")
async def delete_annotation(kind: str, msg_id: int):
    if kind not in {"bad_response", "bookmark"}:
        return JSONResponse({"error": "unsupported annotation kind"}, status_code=400)
    remove_message_annotation(msg_id, kind)
    return JSONResponse({"ok": True})


@app.get("/config/creds/capabilities")
async def credential_capabilities(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin credential requests are not allowed."}, status_code=403)
    direct_host = request.client.host if request.client else None
    macos = sys.platform == "darwin"
    return JSONResponse({
        "host_browser_detection": macos and _request_is_loopback(request.headers, direct_host),
        "keychain_unlock": macos,
    }, headers={"Cache-Control": "no-store"})


@app.get("/config/creds/profiles")
async def claude_meter_profiles(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    if sys.platform != "darwin" or not _request_is_loopback(request.headers, request.client.host if request.client else None):
        return JSONResponse({"error": "Host detection requires a direct macOS loopback connection."}, status_code=403)
    profiles = await asyncio.to_thread(creds.claude_browser_profiles)
    return JSONResponse(
        [{"id": key, "label": label} for key, (label, _) in profiles.items()],
        headers={"Cache-Control": "no-store"},
    )


class ClaudeBrowserProfileRequest(BaseModel):
    profile: str


class ClaudeMeterConfirmation(BaseModel):
    confirmation: str


ClaudeMeterCredentials = tuple[str | None, str | None, str | None]
_claude_meter_preview: tuple[str, float, str, str, str, ClaudeMeterCredentials] | None = None
_CLAUDE_METER_REVIEW_SECONDS = 300


def _expire_claude_meter_preview(confirmation: str) -> None:
    global _claude_meter_preview
    if _claude_meter_preview is not None and _claude_meter_preview[0] == confirmation:
        _claude_meter_preview = None


def _saved_claude_meter() -> ClaudeMeterCredentials:
    saved = creds.load()
    return (saved.get("claude_org_id") or saved.get("org_id"),
            saved.get("claude_session_key") or saved.get("session_key"),
            saved.get("claude_email"))


async def _preview_claude_meter(org_id: str | None, session_key: str | None) -> JSONResponse:
    from curl_cffi.requests import AsyncSession

    global _claude_meter_preview
    if not org_id or not session_key:
        return JSONResponse({"error": "Claude organization and session key are both required."}, status_code=400)
    previous = _saved_claude_meter()
    try:
        async with AsyncSession() as session:
            response = await session.get(
                "https://claude.ai/api/account",
                headers={"Cookie": f"sessionKey={session_key}", "Accept": "application/json"},
                impersonate="chrome", allow_redirects=False, timeout=15,
            )
            response.raise_for_status()
            account = response.json()
        email = account["email_address"]
        organization = next(
            membership["organization"] for membership in account["memberships"]
            if membership["organization"]["uuid"] == org_id
        )
        if not isinstance(email, str) or not email.strip():
            raise ValueError("Missing Claude email")
    except Exception:
        return JSONResponse({
            "error": "Could not verify the Claude account and organization. Check the selected profile or session.",
        }, status_code=422)

    previous_org, _, previous_email = previous
    confirmation = secrets.token_urlsafe(32)
    expires = time.monotonic() + _CLAUDE_METER_REVIEW_SECONDS
    _claude_meter_preview = (confirmation, expires, org_id, session_key, email, previous)
    asyncio.get_running_loop().call_later(
        _CLAUDE_METER_REVIEW_SECONDS, _expire_claude_meter_preview, confirmation,
    )
    return JSONResponse({
        "confirmation": confirmation, "email": email,
        "organization": organization.get("name") or org_id, "claude_org_id": org_id,
        "previous_email": previous_email, "previous_org": previous_org,
        "account_changed": bool(previous_org and (
            previous_org != org_id or not previous_email or previous_email.casefold() != email.casefold()
        )),
    }, headers={"Cache-Control": "no-store"})


@app.post("/config/creds")
async def legacy_save_claude_meter(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    return JSONResponse({"error": "Refresh Squid to review and save Claude meter credentials."}, status_code=409)


@app.post("/config/creds/preview")
async def preview_claude_meter(request: Request, req: CredsRequest):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    return await _preview_claude_meter(req.claude_org_id.strip(), req.claude_session_key.strip())


@app.post("/config/creds/auto")
async def auto_detect_creds(request: Request, req: ClaudeBrowserProfileRequest | None = None):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    if req is None:
        return JSONResponse({"error": "Refresh Squid to choose a browser profile for the Claude meter."}, status_code=409)
    if sys.platform != "darwin" or not _request_is_loopback(request.headers, request.client.host if request.client else None):
        return JSONResponse({"error": "Host detection requires a direct macOS loopback connection."}, status_code=403)
    try:
        found = await asyncio.to_thread(creds.read_claude_browser_creds, req.profile)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return await _preview_claude_meter(found["lastActiveOrg"], found["sessionKey"])


@app.post("/config/creds/confirm")
async def confirm_claude_meter(request: Request, req: ClaudeMeterConfirmation):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    global _claude_meter_preview
    preview = _claude_meter_preview
    if preview is None or req.confirmation != preview[0]:
        return JSONResponse({"error": "Review the Claude account again before saving."}, status_code=409)
    _, expires, org_id, session_key, email, previous = preview
    _claude_meter_preview = None
    if time.monotonic() > expires or _saved_claude_meter() != previous:
        return JSONResponse({"error": "The review expired or the saved session changed. Review the account again."}, status_code=409)
    usage_response = await _fetch_claude_web_usage(org_id, session_key)
    if usage_response.status_code != 200 or not _has_claude_web_usage(_json_response_data(usage_response)):
        return JSONResponse({
            "error": "Could not load usage for the reviewed Claude organization. The saved meter was not changed.",
        }, status_code=502)
    if time.monotonic() > expires or _saved_claude_meter() != previous:
        return JSONResponse({"error": "The review expired or the saved session changed. Review the account again."}, status_code=409)
    try:
        creds.save(org_id, session_key, email)
    except OSError:
        log.exception("Could not write Claude meter credentials")
        return JSONResponse({"error": "Could not save the Claude meter credentials. The saved meter was not changed."},
                            status_code=500)
    return JSONResponse({"ok": True})


@app.post("/config/creds/codex")
async def save_codex_creds(req: CodexCredsRequest):
    creds.save_codex(req.token.strip(), req.account_id.strip())
    return JSONResponse({"ok": True})


def _shore_pair_fragment(offer: dict, code: str) -> str:
    """Encodes the ceremony offer + secret for the pair link's URL fragment
    (never the query string or path) so a browser-to-relay request for the
    page itself can't leak either value into server logs."""
    payload = json.dumps({"v": 1, "offer": offer, "code": code}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


@app.post("/shore/pairing/begin", include_in_schema=shore_enabled())
async def shore_pairing_begin(request: Request):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    from .shore import _load_runtime_config
    from .shore_crypto import ShoreProtocolError, uuid7
    from .shore_transport import _client_release_version
    config = await asyncio.to_thread(_load_runtime_config, shore_identity_dir(_cfg))
    if config is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    try:
        client_version = _client_release_version()
    except ShoreProtocolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    ceremony_id = uuid7()
    try:
        result = await asyncio.to_thread(_shore_connection.channel.begin_pairing, ceremony_id)
    except ShoreProtocolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    pair_url = (
        f"{config.relay.rstrip('/')}/@{config.username}/pair"
        f"?v={urllib.parse.quote(client_version, safe='')}"
        f"#{_shore_pair_fragment(result['offer'], result['code'])}"
    )
    return JSONResponse({
        "ceremony_id": ceremony_id, "code": result["code"], "expires_at": result["expires_at"],
        "pair_url": pair_url,
    })


@app.get("/shore/pairing/status", include_in_schema=shore_enabled())
async def shore_pairing_status(request: Request, ceremony_id: str):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    status = await asyncio.to_thread(_shore_connection.channel.pairing_status, ceremony_id)
    return JSONResponse(status)


@app.get("/shore/pairing/requests", include_in_schema=shore_enabled())
async def shore_pairing_requests(request: Request):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    return JSONResponse({"requests": _shore_connection.list_pairing_requests()})


@app.post("/shore/pairing/requests/approve", include_in_schema=shore_enabled())
async def shore_pairing_request_approve(request: Request, req: ShoreApprovePairingRequest):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    from .shore_crypto import ShoreProtocolError
    try:
        await _shore_connection.approve_pairing_request(req.request_id, req.verification_code)
    except ShoreProtocolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True})


@app.post("/shore/pairing/requests/reject", include_in_schema=shore_enabled())
async def shore_pairing_request_reject(request: Request, req: ShoreRejectPairingRequest):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    return JSONResponse({"ok": _shore_connection.reject_pairing_request(req.request_id)})


@app.get("/shore/devices", include_in_schema=shore_enabled())
async def shore_list_devices(request: Request):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    from .shore_crypto import ShoreProtocolError
    try:
        devices = await asyncio.to_thread(_shore_connection.channel.list_devices)
    except ShoreProtocolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"devices": [
        {"device_id": device.device_id, "key_epoch": device.key_epoch, "capabilities": list(device.capabilities),
         "approved_at": device.approved_at}
        for device in devices
    ]})


@app.post("/shore/devices/revoke", include_in_schema=shore_enabled())
async def shore_revoke_device(request: Request, req: ShoreRevokeDeviceRequest):
    direct_host = request.client.host if request.client else None
    if not _request_is_loopback(request.headers, direct_host):
        return JSONResponse({"error": "loopback_required"}, status_code=403)
    if _shore_connection is None:
        return JSONResponse({"error": "shore_not_configured"}, status_code=400)
    from .shore_crypto import ShoreProtocolError
    try:
        revoked = await _shore_connection.revoke_device(req.device_id)
    except ShoreProtocolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"ok": revoked})


@app.get("/quota")
@app.get("/quota/claude")
async def quota_claude():
    org_id = creds.get_org_id()
    session_key = creds.get_session_key()
    if not org_id or not session_key:
        return JSONResponse({"error": "credentials not configured"}, status_code=400)
    return await _fetch_claude_web_usage(org_id, session_key)


def _has_claude_web_usage(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    has_usage = False
    for name in ("five_hour", "seven_day"):
        window = data.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            return False
        utilization = window.get("utilization")
        if utilization is None:
            continue
        if type(utilization) not in (int, float) or not 0 <= utilization < math.inf:
            return False
        has_usage = True
    return has_usage


async def _fetch_claude_web_usage(org_id: str, session_key: str) -> JSONResponse:
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            r = await session.get(
                f"https://claude.ai/api/organizations/{urllib.parse.quote(org_id, safe='')}/usage",
                headers={"Cookie": f"sessionKey={session_key}"},
                impersonate="chrome",
                allow_redirects=False,
                timeout=15,
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"claude.ai returned {r.status_code}"}, status_code=502)
        return JSONResponse(r.json())
    except Exception as exc:
        log.error("quota fetch failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/quota/codex")
async def quota_codex():
    codex_auth = creds.get_codex_cli_auth()
    saved_token = creds.get_codex_token()
    saved_account_id = creds.get_codex_account_id()
    # New browser imports always store an account_id key (possibly empty).
    # Legacy fallback tokens must not displace an existing CLI login.
    use_saved = bool(saved_token and (saved_account_id is not None or not codex_auth.get("access_token")))
    token = saved_token if use_saved else codex_auth.get("access_token")
    account_id = saved_account_id if use_saved else codex_auth.get("account_id")
    return await _fetch_codex_quota(token, account_id)


async def _fetch_codex_quota(token: Optional[str], account_id: Optional[str]) -> JSONResponse:
    if not token:
        return JSONResponse({"error": "credentials not configured"}, status_code=400)
    authorization = _codex_bearer_header(token)
    if not authorization:
        return JSONResponse({"error": "Codex bearer token required"}, status_code=400)
    try:
        from curl_cffi.requests import AsyncSession
        common_headers = {
            "Accept": "application/json",
            "OpenAI-Beta": "codex-1",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "originator": "Codex Desktop",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        if account_id:
            common_headers["ChatGPT-Account-ID"] = account_id
        async with AsyncSession() as session:
            r = await session.get(
                "https://chatgpt.com/backend-api/wham/usage",
                headers={**common_headers, "Authorization": authorization},
                impersonate="chrome",
                allow_redirects=False,
            )
        if r.status_code != 200:
            if r.status_code == 401:
                return JSONResponse({"error": "Codex token expired"}, status_code=401)
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


@app.post("/config/{gauge}/max-budget")
async def set_max_budget(gauge: str, body: dict):
    if gauge not in _BALANCE_GAUGES:
        return JSONResponse({"error": f"unknown balance gauge {gauge!r}"}, status_code=404)
    amount = body.get("amount")
    if not amount or amount <= 0:
        return JSONResponse({"error": "invalid amount"}, status_code=400)
    creds.save_max_budget(gauge, amount)
    return JSONResponse({"status": "ok"})


@app.delete("/config/{gauge}/max-budget")
async def clear_max_budget(gauge: str):
    if gauge not in _BALANCE_GAUGES:
        return JSONResponse({"error": f"unknown balance gauge {gauge!r}"}, status_code=404)
    creds.clear_max_budget(gauge)
    return JSONResponse({"status": "ok"})


def _parse_deepseek_balance(data: dict, host: str) -> tuple[float, str]:
    balances = data.get("balance_infos") or []
    info = next((item for item in balances if item.get("currency") == "USD"), None)
    info = info or next((item for item in balances if item.get("currency") == "CNY"), None)
    if not info:
        raise ValueError("balance unavailable")
    return float(info.get("total_balance") or 0), ("$" if info.get("currency") == "USD" else "¥")


def _parse_kimi_balance(data: dict, host: str) -> tuple[float, str]:
    if data.get("status") is not True and data.get("code") != 0:
        raise ValueError("balance unavailable")
    balance = (data.get("data") or {}).get("available_balance")
    if balance is None:
        raise ValueError("balance unavailable")
    # moonshot.cn bills in CNY, moonshot.ai in USD — the payload has no currency field
    return float(balance), ("¥" if host.endswith(".cn") else "$")


# Prepaid-balance gauges: one entry per provider family. The balance URL is the
# provider's configured base_url origin (default_base as fallback) plus `path`;
# `parse` normalizes the upstream payload to (balance, currency_symbol).
_BALANCE_GAUGES = {
    "deepseek": {
        "label": "DeepSeek", "default_base": "https://api.deepseek.com",
        "path": "/user/balance", "parse": _parse_deepseek_balance,
    },
    "kimi": {
        "label": "Kimi", "default_base": "https://api.moonshot.ai",
        "path": "/v1/users/me/balance", "parse": _parse_kimi_balance,
    },
}


def _balance_url(provider: Provider, spec: dict) -> str:
    raw = (provider.base_url or "").strip()
    parsed = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}") if raw else None
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else spec["default_base"]
    return f"{origin}{spec['path']}"


async def _balance_snapshot(provider: Provider, ref: str, spec: dict) -> JSONResponse:
    try:
        api_key = provider.resolved_api_key()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not api_key:
        return JSONResponse({"error": "api_key not configured"}, status_code=400)
    url = _balance_url(provider, spec)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10,
            )
        if response.status_code != 200:
            return JSONResponse({"error": f"{spec['label']} returned {response.status_code}"}, status_code=502)
        balance, symbol = spec["parse"](response.json(), urllib.parse.urlparse(url).netloc)
    except Exception as exc:
        log.error("%s balance fetch failed for %s: %s", provider.gauge.type, ref, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    label = spec["label"]
    max_budget = creds.get_max_budget(provider.gauge.type)
    if max_budget and max_budget > 0:
        spent = max(0, max_budget - balance)
        pct = max(0, min(100, round(spent / max_budget * 100)))
        return JSONResponse({
            "status": "ok", "text": f"{symbol}{balance:.2f}",
            "raw": balance, "used_percent": None, "reset_at": None,
            "title": f"{label} · {symbol}{spent:.2f} spent of {symbol}{max_budget:.2f}",
            "max_budget": max_budget, "max_budget_pct": pct, "spent": spent,
        })
    return JSONResponse({
        "status": "ok", "text": f"{symbol}{balance:.2f}",
        "raw": balance, "used_percent": None, "reset_at": None,
        "title": f"{label} balance · {symbol}{balance:.2f}",
    })


def _json_response_data(response: JSONResponse) -> dict:
    try:
        return json.loads(response.body)
    except (TypeError, json.JSONDecodeError):
        return {}


def _quota_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _codex_limit_window(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    used = _quota_number(value.get("used_percent", value.get("usedPercent")))
    reset_at = value.get("reset_at", value.get("resets_at", value.get("resetsAt")))
    if reset_at is None and value.get("reset_after_seconds") is not None:
        reset_after = _quota_number(value.get("reset_after_seconds"))
        if reset_after is not None:
            reset_at = time.time() + reset_after
    window_seconds = _quota_number(
        value.get("limit_window_seconds", value.get("window_seconds"))
    )
    if used is None and reset_at is None and window_seconds is None:
        return None
    return {"used_percent": used, "reset_at": reset_at, "window_seconds": window_seconds}


def _codex_usage_windows(data: dict) -> tuple[Optional[dict], Optional[dict]]:
    rate_limit = data.get("rate_limit") or data.get("rateLimits") or {}
    primary = _codex_limit_window(rate_limit.get("primary_window") or rate_limit.get("primary"))
    secondary = _codex_limit_window(rate_limit.get("secondary_window") or rate_limit.get("secondary"))
    five_hour = None
    seven_day = None
    for window in (primary, secondary):
        if not window:
            continue
        seconds = window.get("window_seconds")
        if seconds == 18_000:
            five_hour = window
        elif seconds == 604_800:
            seven_day = window
        elif five_hour is None:
            five_hour = window
        elif seven_day is None:
            seven_day = window
    return five_hour, seven_day


_last_native_quota_sample = 0


async def quota_antigravity():
    """Ask the native CLI for fresh quota, without starting a model turn."""
    global _last_native_quota_sample
    _last_native_quota_sample = max(_last_native_quota_sample + 1, time.time_ns() // 1000)
    headers = {"X-Squid-Quota-Sample": str(_last_native_quota_sample)}
    if not AGY_PATH:
        return JSONResponse({"error": "agy CLI not installed"}, status_code=503, headers=headers)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            AGY_PATH, "--print", "/usage", "--output-format", "json",
            "--print-timeout", "30s",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
        data = json.loads(stdout)
        if proc.returncode or data.get("status") != "SUCCESS":
            raise ValueError("agy usage command failed; check native login")
        buckets = []
        for group in data.get("command", {}).get("data", {}).get("groups", []):
            for bucket in group.get("buckets", []):
                remaining = _quota_number(bucket.get("remaining_fraction"))
                if remaining is None or not 0 <= remaining <= 1:
                    continue
                buckets.append({
                    "group": group.get("name", ""),
                    "window": bucket.get("window", ""),
                    "used_percent": (1 - remaining) * 100,
                    "reset_at": bucket.get("reset_time"),
                })
        if not buckets:
            raise ValueError("agy returned no quota buckets")
        limiting = max(buckets, key=lambda bucket: bucket["used_percent"])
        used = limiting["used_percent"]
        title = "Antigravity usage · " + " · ".join(
            f"{b['group']} {b['window']}: {b['used_percent']:.1f}% used"
            + (f" (resets {b['reset_at']})" if b['reset_at'] else "")
            for b in buckets
        )
        return JSONResponse({
            "status": "ok", "text": f"{round(used)}%", "raw": used,
            "used_percent": used, "reset_at": limiting["reset_at"],
            "title": title, "buckets": buckets,
        }, headers=headers)
    except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
        return JSONResponse({"error": "Antigravity quota unavailable; check agy login"}, status_code=502, headers=headers)
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def _dedicated_codex_quota(provider: Provider) -> JSONResponse:
    try:
        home = codex_subscription.home_for(provider.id)
        if not home.is_dir():
            return JSONResponse({"error": "Provider is signed out"}, status_code=401)
        codex_subscription.ensure_home(provider.id)
        with codex_subscription.lock(home, login=False):
            tokens, identity = codex_subscription.codex_quota_credentials(home)
        if not identity or not tokens:
            return JSONResponse({"error": "Provider is signed out"}, status_code=401)
        response = await _fetch_codex_quota(tokens.get("access_token"), tokens.get("account_id"))
        with codex_subscription.lock(home, login=False):
            if codex_subscription.account_identity(home) != identity:
                return JSONResponse({"error": "Provider login changed"}, status_code=401)
        return response
    except codex_subscription.SubscriptionBusy:
        return JSONResponse({"error": "Provider sign-in is in progress"}, status_code=401)
    except (OSError, ValueError):
        return JSONResponse({"error": "Provider credentials unavailable"}, status_code=401)


async def _fetch_claude_oauth_quota(token: str) -> JSONResponse:
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
                follow_redirects=False,
            )
        if response.status_code in (401, 403):
            return JSONResponse({"error": "Claude login could not authorize usage"}, status_code=401)
        if response.status_code != 200:
            return JSONResponse({"error": f"Claude usage returned {response.status_code}"}, status_code=502)
        data = response.json()
        if isinstance(data, dict):
            return JSONResponse(data)
    except (httpx.HTTPError, ValueError):
        pass
    return JSONResponse({"error": "Claude usage unavailable"}, status_code=502)


def _claude_quota_credentials(home: Path) -> tuple[str | None, str | None]:
    with codex_subscription.lock(home, login=False):
        return codex_subscription.claude_quota_credentials(home)


async def _dedicated_claude_quota(provider: Provider) -> JSONResponse:
    try:
        home = codex_subscription.home_for(provider.id, "claudecode")
        if not home.is_dir():
            return JSONResponse({"error": "Provider is signed out"}, status_code=401)
        codex_subscription.ensure_home(provider.id, "claudecode")
        token, revision = await asyncio.to_thread(_claude_quota_credentials, home)
        if not token:
            return JSONResponse({"error": "Provider is signed out"}, status_code=401)
        response = await _fetch_claude_oauth_quota(token)
        _, current_revision = await asyncio.to_thread(_claude_quota_credentials, home)
        if current_revision != revision:
            return JSONResponse({"error": "Provider login changed"}, status_code=401)
        return response
    except codex_subscription.SubscriptionBusy:
        return JSONResponse({"error": "Provider sign-in is in progress"}, status_code=401)
    except codex_subscription.ClaudeKeychainInteractionRequired:
        return JSONResponse({
            "error": "Provider credentials unavailable", "code": "keychain_interaction_required",
        }, status_code=401)
    except (OSError, ValueError):
        return JSONResponse({"error": "Provider credentials unavailable"}, status_code=401)


async def _quota_snapshot_for_provider(provider: Provider, ref: str) -> JSONResponse:
    """Return a normalized gauge snapshot for one provider. Quota is a provider
    attribute (ADR-0028) — this never needs to know which harness is using it."""
    gauge = provider.gauge
    dedicated_gauge = _uses_dedicated_gauge(provider)
    if gauge.type == "none" or (provider.dedicated_login and not dedicated_gauge):
        return JSONResponse({"status": "none"})
    if gauge.type == "static":
        return JSONResponse({
            "status": "static", "text": gauge.text, "title": gauge.title,
            "used_percent": None, "reset_at": None,
        })

    if not dedicated_gauge and _gauge_authed(gauge.type, provider) is False:
        label = provider.label or ref
        return JSONResponse({
            "status": "unauthenticated",
            "text": "auth",
            "title": f"{label} credentials not configured",
            "used_percent": None,
            "reset_at": None,
        })

    if gauge.type == "antigravity":
        return JSONResponse({"status": "none"})

    if gauge.type in _BALANCE_GAUGES:
        return await _balance_snapshot(provider, ref, _BALANCE_GAUGES[gauge.type])

    if dedicated_gauge:
        fetch = {"claude": _dedicated_claude_quota, "codex": _dedicated_codex_quota}[gauge.type]
        raw_response = await fetch(provider)
    else:
        raw_response = await {"claude": quota_claude, "codex": quota_codex, "cursor": quota_cursor}[gauge.type]()
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
        five_hour, seven_day = _codex_usage_windows(data)
        display_window = five_hour or seven_day or {}
        used = display_window.get("used_percent")
        reset_at = display_window.get("reset_at")
        return JSONResponse({
            "status": "ok", "text": f"{round(used)}%" if used is not None else None,
            "raw": used, "used_percent": used, "reset_at": reset_at,
            "title": "Codex usage",
            "seven_day": {
                "used_percent": seven_day.get("used_percent"),
                "reset_at": seven_day.get("reset_at"),
            } if seven_day else None,
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


@app.get("/quota/provider/{provider_id}/account")
async def claude_quota_account(request: Request, provider_id: str):
    if not _same_origin(request):
        return JSONResponse({"error": "Cross-origin Claude meter requests are not allowed."}, status_code=403)
    provider = get_provider(provider_id)
    if not provider or provider.gauge.type != "claude" or not _uses_dedicated_gauge(provider):
        return JSONResponse({"error": "No dedicated Claude meter for this provider."}, status_code=404)

    try:
        home = codex_subscription.home_for(provider.id, "claudecode")
        details = await asyncio.to_thread(codex_subscription.claude_account_details, home) if home.is_dir() else None
    except (OSError, ValueError):
        details = None
    if details is None:
        return JSONResponse({"error": "Claude Code account could not be verified."}, status_code=409)
    return JSONResponse(details, headers={"Cache-Control": "no-store"})


@app.get("/quota/provider/{provider_id}")
async def quota_provider(provider_id: str):
    """Return a normalized gauge snapshot for one configured provider, with no
    harness involved — quota is an account fact, not tied to any particular CLI."""
    provider = get_provider(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Unknown provider {provider_id!r}"}, status_code=404)
    return await _quota_snapshot_for_provider(provider, provider_id)


class NativeQuotaRefreshRequest(BaseModel):
    turn_id: int = Field(gt=0)


# Share the native check, including failures, across browsers observing a turn.
_native_quota_turn_checks: OrderedDict[tuple[str, int], asyncio.Task[JSONResponse]] = OrderedDict()
_NATIVE_QUOTA_TURN_CACHE_SIZE = 1024


@app.post("/quota/provider/{provider_id}")
async def refresh_native_provider_quota(
    provider_id: str, refresh: NativeQuotaRefreshRequest | None = None,
):
    """Refresh on demand or reuse the check for a completed turn."""
    provider = get_provider(provider_id)
    if provider is None or provider.gauge.type != "antigravity":
        return JSONResponse({"error": "Native quota refresh unavailable"}, status_code=404)
    if refresh is None:
        return await quota_antigravity()

    key = (provider.id, refresh.turn_id)
    check = _native_quota_turn_checks.get(key)
    if check is None:
        turn = get_message(refresh.turn_id)
        if not turn or turn["role"] != "assistant" or turn["status"] != "done" or turn["source"] == "shell":
            return JSONResponse({"error": "Native quota refresh requires a successful turn"}, status_code=409)
        _, recorded_provider = split_agent_ref((turn.get("stats") or {}).get("backend"))
        if recorded_provider != provider.id:
            return JSONResponse({"error": "Native quota refresh requires the turn's provider"}, status_code=409)
        check = asyncio.create_task(quota_antigravity())
        _native_quota_turn_checks[key] = check
    # A disconnected browser must not cancel a check another browser is using.
    response = await asyncio.shield(check)
    while len(_native_quota_turn_checks) > _NATIVE_QUOTA_TURN_CACHE_SIZE:
        oldest = next(iter(_native_quota_turn_checks))
        if not _native_quota_turn_checks[oldest].done():
            break
        _native_quota_turn_checks.popitem(last=False)
    return response


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
def get_remote_url():
    return JSONResponse(_tailscale_remote_access())


def _tailscale_remote_access():
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {"url": None, "reason": "not_running"}
        data = json.loads(result.stdout)
        dns = data.get("Self", {}).get("DNSName", "").rstrip(".")
        if dns:
            return {"url": f"https://{dns}/"}
        return {"url": None, "reason": "no_dns"}
    except FileNotFoundError:
        return {"url": None, "reason": "not_installed"}
    except Exception:
        return {"url": None, "reason": "error"}


@app.post("/remote")
def create_remote_pairing(request: Request):
    headers = {"Cache-Control": "no-store"}
    origin = instance_auth.browser_origin(request)
    if origin is None or request.headers.get("origin") != origin:
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403, headers=headers)
    token = request.cookies.get(instance_auth.SESSION_COOKIE)
    credentials = instance_auth.instance_credentials()
    if not token or not credentials.session_id(token):
        return JSONResponse({"error": "pairing_required"}, status_code=401, headers=headers)
    remote = _tailscale_remote_access()
    if not remote["url"]:
        return JSONResponse({"error": remote["reason"]}, status_code=503, headers=headers)
    code = credentials.issue(session_token=token)
    if not code:
        return JSONResponse({"error": "pairing_required"}, status_code=401, headers=headers)
    return JSONResponse({
        "pair_url": f"{remote['url']}instance/pair#code={code}",
        "expires_in": instance_auth.PAIRING_LIFETIME,
    }, headers=headers)


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

def _localfile_available_child(parent: Path, name: str) -> Union[Path, JSONResponse]:
    child = _localfile_child(parent, name)
    if isinstance(child, JSONResponse) or not child.exists():
        return child
    original = Path(name)
    stem = original.stem if original.suffix else original.name
    suffix = original.suffix
    for i in range(1, 1000):
        candidate = _localfile_child(parent, f"{stem} {i}{suffix}")
        if isinstance(candidate, JSONResponse):
            return candidate
        if not candidate.exists():
            return candidate
    return JSONResponse({"error": "no available filename"}, status_code=409)

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
    # Isolate active documents even when they are opened directly.
    document_headers = {**_nocache, "Content-Security-Policy": "sandbox allow-scripts"}
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
        return HTMLResponse(html, headers=document_headers)
    mime = _LOCALFILE_TEXT_MIME_BY_SUFFIX.get(p.suffix.lower())
    if mime is None:
        mime, _ = mimetypes.guess_type(str(p))
    if mime in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
        _nocache = document_headers
    if mime and mime.startswith("text/"):
        return PlainTextResponse(p.read_text(errors="replace"), media_type=mime, headers=_nocache)
    if (not mime or mime == "application/octet-stream") and _looks_like_text_file(p):
        return PlainTextResponse(p.read_text(errors="replace"), media_type="text/plain", headers=_nocache)
    return FileResponse(str(p), media_type=mime or "application/octet-stream", headers=_nocache)


def _encode_cwd_dashes(cwd: str) -> str:
    return cwd.replace("/", "-")


def _opencode_session_transcript_rows(home: Path, session_id: str) -> Optional[list[dict]]:
    """Read an opencode session's rows directly out of its SQLite DB.

    Unlike the other harnesses, opencode keeps sessions in one SQLite DB
    (`opencode.db`) rather than a per-session file on disk -- there's no raw
    transcript file to locate, and synthesizing a fake one on disk would
    misrepresent this as opencode's own native output when it isn't.
    Returns the raw rows (control events, per-turn messages, per-turn
    content parts) in time order for the caller to render and label
    honestly as reconstructed from SQLite. Opened read-only (`mode=ro`) so
    this never contends with opencode's own WAL writer.
    """
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    if not db_path.is_file():
        return None
    entries: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            for kind, sql in (
                ("session_message", "SELECT id, type, seq, time_created, data FROM session_message WHERE session_id = ?"),
                ("message", "SELECT id, time_created, data FROM message WHERE session_id = ?"),
                ("part", "SELECT id, message_id, time_created, data FROM part WHERE session_id = ?"),
            ):
                for row in conn.execute(sql, (session_id,)):
                    entry = dict(row)
                    entry["kind"] = kind
                    entry["data"] = json.loads(entry["data"])
                    entries.append(entry)
        finally:
            conn.close()
    except (sqlite3.Error, json.JSONDecodeError, OSError):
        return None
    if not entries:
        return None
    entries.sort(key=lambda e: e.get("time_created", 0))
    return entries


def _find_session_log(harness: str, session_id: str, cwd: str, agent: str = "") -> Optional[Path]:
    """Locate a coding agent's raw on-disk session transcript (.jsonl), if any.

    Each CLI encodes cwd into its session directory name differently; these are the
    conventions observed for each harness's own local session storage (not something
    Squid controls). Falls back to a recursive search under the harness's base dir if
    the direct guess misses, so a slightly-off encoding assumption still resolves.

    A Blank Home agent's harness subprocess runs with $HOME pointed at its sandbox
    directory (see sandbox_home.py / ADR-0036), so its transcript lives there too,
    not under the real $HOME -- look up the same home the subprocess actually used.

    opencode has no branch here -- it keeps sessions in a SQLite DB, not a
    per-session file, so there's nothing file-shaped to locate. See
    _opencode_session_transcript_rows() and the /session-log route below.
    """
    if not session_id:
        return None
    home = sandbox_home.sandbox_home_path(agent) if agent and sandbox_home.current_home_mode(agent) == "blank_home" else Path.home()
    try:
        if harness == "antigravity":
            from .agy_logs import session_log_path
            try:
                path = session_log_path(agent, session_id)
            except ValueError:
                return None
            return path if path.is_file() else None
        if harness == "claudecode":
            base = home / ".claude" / "projects"
            direct = base / _encode_cwd_dashes(cwd) / f"{session_id}.jsonl"
            if direct.is_file():
                return direct
            return next(base.rglob(f"{session_id}.jsonl"), None) if base.is_dir() else None
        if harness == "codex":
            base = home / ".codex" / "sessions"
            return next(base.rglob(f"*{session_id}.jsonl"), None) if base.is_dir() else None
        if harness == "pi":
            base = home / ".pi" / "agent" / "sessions"
            direct_dir = base / f"--{_encode_cwd_dashes(cwd)}--"
            if direct_dir.is_dir():
                match = next(direct_dir.glob(f"*_{session_id}.jsonl"), None)
                if match:
                    return match
            return next(base.rglob(f"*_{session_id}.jsonl"), None) if base.is_dir() else None
        if harness == "cursor":
            base = home / ".cursor" / "projects"
            direct = base / cwd.strip("/").replace("/", "-") / "agent-transcripts" / session_id / f"{session_id}.jsonl"
            if direct.is_file():
                return direct
            return next(base.rglob(f"{session_id}.jsonl"), None) if base.is_dir() else None
    except OSError:
        return None
    return None


@app.get("/session-log")
async def session_log(agent: str, session_id: str, cwd: str = ""):
    """Resolve a session's raw transcript for viewing.

    Most harnesses store one file per session on disk -- returns `path` for
    the existing file viewer to open directly. opencode stores sessions in a
    SQLite DB instead, so there's no file to point at: returns `entries`
    (the raw DB rows, in time order) plus `source` so the frontend can
    render them in a dedicated view that's honest about where they came
    from, rather than disguising them as a file opencode itself produced.

    Either way also returns `turns`: squid's own per-turn timestamps for this
    session_id, so the viewer can mark where one squid turn's raw log
    entries end and the next begins -- a raw transcript (jsonl or SQLite) has
    no notion of squid's turn grouping on its own.
    """
    stats = get_stats(session_id)
    recorded_home = stats.get("codex_home") if stats and stats.get("agent") == agent else None
    harness = stats.get("harness") if recorded_home else None
    if recorded_home is None:
        from .stats_db import get_codex_provider_home
        recorded = get_codex_provider_home(agent, session_id)
        if recorded:
            recorded_home, harness = recorded
    if not harness:
        agent_cfg = get_agent(agent)
        harness = agent_cfg.get("harness") if agent_cfg else None
    turns = get_session_turn_boundaries(session_id) if session_id else []
    if harness == "opencode":
        home = (
            sandbox_home.sandbox_home_path(agent)
            if agent and sandbox_home.current_home_mode(agent) == "blank_home"
            else Path.home()
        )
        entries = _opencode_session_transcript_rows(home, session_id)
        return JSONResponse({
            "path": None,
            "entries": entries,
            "source": "opencode-sqlite" if entries else None,
            "turns": turns,
        })
    if recorded_home:
        directory = Path(recorded_home) / ("projects" if harness == "claudecode" else "sessions")
        pattern = f"{session_id}.jsonl" if harness == "claudecode" else f"*{session_id}.jsonl"
        path = next(directory.rglob(pattern), None) if directory.is_dir() else None
    else:
        path = _find_session_log(harness, session_id, cwd, agent) if harness else None
    return JSONResponse({
        "path": str(path) if path else None,
        "entries": None,
        "source": ("agy-ndjson" if harness == "antigravity" else "file") if path else None,
        "turns": turns,
    })


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
    child = _localfile_available_child(parent_path, name.strip())
    if isinstance(child, JSONResponse):
        return child
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


@app.post("/localfile/check-paths")
async def check_local_paths(req: LocalfileCheckPathsRequest, request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin reads are not allowed"}, status_code=403)
    paths = []
    for raw_path in req.paths:
        try:
            p = Path(raw_path).expanduser().resolve()
            exists = p.exists()
            is_file = p.is_file() if exists else False
        except OSError:
            p = Path(raw_path)
            exists = False
            is_file = False
        paths.append({"path": raw_path, "resolved_path": str(p), "exists": exists, "is_file": is_file})
    return JSONResponse({"paths": paths})


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


# ---------------------------------------------------------------------------
# Versioned realtime protocol (ADR-0040)
# ---------------------------------------------------------------------------

_REALTIME_REPLAY_LIMIT = 500
_REALTIME_REPLAY_BYTE_LIMIT = 512 * 1024
_REALTIME_REPLAY_MAX_AGE_SECONDS = 24 * 60 * 60
_REALTIME_SAFETY_POLL_SECONDS = 20.0
_realtime_chat_tasks: set[asyncio.Task] = set()

# Outbound/backpressure and heartbeat limits (ADR-0040). The config values are
# the defaults; server code reads the module-level names so tests can patch
# them via monkeypatch.
_REALTIME_OUTBOUND_QUEUE_LIMIT = REALTIME_OUTBOUND_QUEUE_LIMIT
_REALTIME_MAX_FRAME_BYTES = REALTIME_MAX_FRAME_BYTES
_REALTIME_HEARTBEAT_SECONDS = REALTIME_HEARTBEAT_SECONDS
_REALTIME_HEARTBEAT_MISS_LIMIT = 2
_REALTIME_COALESCIBLE_TYPES = frozenset({"process.changed", "queue.changed"})
_REALTIME_SUPPORTED_VERSIONS = (1,)


class _RealtimeNotifier:
    """Best-effort live wake-up; the durable event log remains authoritative."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._condition: Optional[asyncio.Condition] = None
        self._generation = 0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._condition = asyncio.Condition()
        set_realtime_commit_listener(self.notify_committed)

    def stop(self) -> None:
        set_realtime_commit_listener(None)
        loop = self._loop
        condition = self._condition
        self._generation += 1
        self._loop = None
        self._condition = None
        if loop and not loop.is_closed() and condition:
            # wait() has already captured this condition. Wake that exact
            # object after advancing the generation, even though start() may
            # later install a new condition for another event loop.
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._wake(condition)),
            )

    def notify_committed(self, _event_id: int) -> None:
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._bump, _event_id)

    def _bump(self, event_id: int = 0) -> None:
        self._generation += 1
        condition = self._condition
        if condition:
            asyncio.create_task(self._wake(condition))
        if event_id and event_id % 1000 == 0:
            asyncio.create_task(asyncio.to_thread(prune_realtime_data))

    @staticmethod
    async def _wake(condition: asyncio.Condition) -> None:
        async with condition:
            condition.notify_all()

    @property
    def generation(self) -> int:
        return self._generation

    async def wait(self, observed: int) -> int:
        condition = self._condition
        if not condition or self._generation != observed:
            return self._generation
        try:
            async with condition:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self._generation != observed),
                    timeout=_REALTIME_SAFETY_POLL_SECONDS,
                )
        except asyncio.TimeoutError:
            pass
        return self._generation


_realtime_notifier = _RealtimeNotifier()


class _RealtimeSlowConsumer(Exception):
    """Sentinel raised after a slow_consumer close to unwind realtime_v1."""


class _RealtimeOutbound:
    """Bounded per-connection outbound queue drained by a dedicated sender task.

    Coalescing is limited to state-replacement frames (`process.changed` /
    `queue.changed`): enqueueing one drops any still-queued frame of the same
    type. A non-coalescible frame over the limit reports overflow (False) so
    the caller can close `slow_consumer`. An OrderedDict keyed by frame type
    (coalescible) or a per-frame sequence number (non-coalescible) replaces
    `asyncio.Queue` because it gives O(1) replace-in-place for coalescing —
    unlike a deque, which needs a full O(n) rebuild to drop a same-type entry
    — while still popping in FIFO order via `popitem(last=False)`.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: "OrderedDict[object, dict]" = OrderedDict()
        self._seq = 0
        self._not_empty = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()

    def enqueue(self, frame: dict) -> bool:
        frame_type = frame.get("type")
        if frame_type in _REALTIME_COALESCIBLE_TYPES:
            key: object = frame_type
            self._items.pop(key, None)
        else:
            if len(self._items) >= self._limit:
                return False
            self._seq += 1
            key = self._seq
        self._items[key] = frame
        self._not_empty.set()
        self._drained.clear()
        return True

    async def wait_until_drained(self) -> None:
        # Bulk history leaves room in the shared queue for heartbeat and chat.
        if self._items:
            clock = asyncio.get_running_loop().time
            deadline = clock() + _REALTIME_HISTORY_QUERY_TIMEOUT
            try:
                while self._items:
                    await asyncio.wait_for(self._drained.wait(), deadline - clock())
            except asyncio.TimeoutError as exc:
                raise _RealtimeSlowConsumer() from exc

    async def get(self) -> dict:
        await self._not_empty.wait()
        _, frame = self._items.popitem(last=False)
        if not self._items:
            self._not_empty.clear()
            self._drained.set()
        return frame

    def __len__(self) -> int:
        return len(self._items)


async def _ws_sender(websocket: WebSocket, outbound: _RealtimeOutbound) -> None:
    """Drain the outbound queue; exits only on a send failure or cancellation."""
    while True:
        frame = await outbound.get()
        try:
            await websocket.send_json(frame)
        except RuntimeError as exc:
            # The ASGI server already closed the socket (the peer went away
            # between frames); treat it as the disconnect it is.
            raise WebSocketDisconnect(code=1006) from exc


async def _realtime_terminate(
    websocket: WebSocket,
    sender_task: asyncio.Task,
    code: int,
    reason: str = "",
    final_frame: Optional[dict] = None,
) -> None:
    """Stop the sender, then send an optional final frame and close.

    `_ws_sender` is the only task that may write to the socket (single-writer
    invariant). Terminal paths — slow_consumer, heartbeat timeout,
    frame_too_large — call this so they cancel and reap the sender first,
    guaranteeing this coroutine is the sole remaining writer before it
    touches the socket. A direct `send_json`/`close` alongside a live sender
    races it and can raise an unhandled `RuntimeError`. Only for terminal
    paths that then return or unwind; never during a cancellation unwind
    (see `realtime_v1`'s finally).
    """
    sender_task.cancel()
    await asyncio.gather(sender_task, return_exceptions=True)
    if final_frame is not None:
        try:
            await websocket.send_json(final_frame)
        except Exception:
            pass
    await websocket.close(code=code, reason=reason)


async def _realtime_send(
    outbound: _RealtimeOutbound,
    frame: dict,
    principal: Optional[str],
    last_acked_cursor: int,
) -> None:
    """Enqueue an outbound frame, raising on overflow.

    Raises `_RealtimeSlowConsumer` on overflow so the caller unwinds through
    the connection's cleanup path instead of continuing to enqueue. The
    terminal slow_consumer error frame + close are performed by the caller
    via `_realtime_terminate` after the sender has been stopped.
    """
    if outbound.enqueue(frame):
        return
    log.warning(
        "Realtime slow consumer (principal=%s, last_acked_cursor=%s, queue_depth=%s) — closing 1013",
        principal, last_acked_cursor, len(outbound),
    )
    raise _RealtimeSlowConsumer()


async def _send_history_check_result(
    outbound: _RealtimeOutbound, request_id: str, result: dict, chunked: bool,
    principal: Optional[str], last_acked_cursor: int,
) -> None:
    frame = _history_query_frame(request_id, result)
    if chunked and result.get("ok") and frame["payload"].get("error") == "history_result_too_large":
        # ASCII JSON lets us cut anywhere. Escaping a chunk in its envelope
        # costs at most two bytes per character, including quotes and backslashes.
        encoded = json.dumps(result, separators=(",", ":"))
        chunk_frame = {
            "v": 1, "type": "history.chunk", "request_id": request_id,
            "payload": {"index": len(encoded), "text": ""},
        }
        overhead = len(json.dumps(chunk_frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        width = (_REALTIME_MAX_FRAME_BYTES - overhead) // 2
        if width > 0:
            count = (len(encoded) + width - 1) // width
            for index in range(count):
                offset = index * width
                chunk = {**chunk_frame, "payload": {"index": index, "text": encoded[offset:offset + width]}}
                await outbound.wait_until_drained()
                await _realtime_send(outbound, chunk, principal, last_acked_cursor)
            await outbound.wait_until_drained()
            frame = _history_query_frame(request_id, {"ok": True, "chunk_count": count})
    await _realtime_send(outbound, frame, principal, last_acked_cursor)


def _realtime_envelope(event: dict) -> dict:
    return {
        "v": 1,
        "type": event["event_type"],
        "event_id": event["event_id"],
        "request_id": None,
        "scope": {"topic": event.get("topic"), "agent": event.get("agent")},
        "msg_id": event.get("msg_id"),
        "run_seq": event.get("run_seq"),
        "payload": event.get("payload") or {},
    }


_REALTIME_V1_REPLAY_TYPES = {
    "chat.meta", "chat.queued", "chat.status", "chat.loading", "chat.processing",
    "chat.tool", "chat.text", "chat.stats", "chat.done", "chat.error",
    "process.changed", "queue.changed", "message.changed", "flow.step.created",
    "worktree.changed", "diff.reverted", "composer.history.changed",
}


def _realtime_replay_rollover_reason(requested_cursor: int, window: dict, events: list[dict]) -> Optional[str]:
    current = window["current"]
    if requested_cursor > current:
        return "future_cursor"
    expected_count = current - requested_cursor
    if window["global_event_count"] != expected_count:
        return "replay_gap"
    if window["scoped_event_count"] > _REALTIME_REPLAY_LIMIT:
        return "event_count"
    oldest = window.get("oldest_created_at")
    if oldest:
        try:
            created = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created).total_seconds() > _REALTIME_REPLAY_MAX_AGE_SECONDS:
                return "event_age"
        except (TypeError, ValueError):
            return "event_age"
    encoded_bytes = 0
    for event in events:
        if event.get("event_type") not in _REALTIME_V1_REPLAY_TYPES:
            return "incompatible_event"
        encoded_bytes += len(json.dumps(
            _realtime_envelope(event), separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8"))
        if encoded_bytes > _REALTIME_REPLAY_BYTE_LIMIT:
            return "event_bytes"
    return None


def _realtime_request_fingerprint(message_type: str, payload: dict) -> str:
    canonical = json.dumps({"type": message_type, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _authorize_realtime_scopes(requested) -> Optional[list[dict]]:
    """Authorize phase-one scopes for the fully trusted local Squid session."""
    if not isinstance(requested, list) or not requested:
        return None
    scopes = []
    for scope in requested:
        if not isinstance(scope, dict):
            return None
        if scope == {"lifecycle": "global"}:
            scopes.append(scope)
            continue
        topic = scope.get("topic")
        agent = scope.get("agent")
        if (set(scope) - {"topic", "agent"} or not isinstance(topic, str)
                or not topic.strip() or (agent is not None and not isinstance(agent, str))):
            return None
        scopes.append({"topic": topic, **({"agent": agent} if agent is not None else {})})
    return scopes


async def _realtime_snapshot(scopes: list[dict]) -> dict:
    snapshot = await asyncio.to_thread(get_realtime_snapshot, scopes, 20)
    if any(scope.get("lifecycle") == "global" for scope in scopes):
        snapshot["processes"] = list_active_procs()
        snapshot["queue"] = dispatcher.all_queued_items()
        return snapshot
    topics = {scope.get("topic") for scope in scopes}
    snapshot["processes"] = [item for item in list_active_procs() if item.get("topic") in topics]
    snapshot["queue"] = [item for item in dispatcher.all_queued_items() if item.get("topic") in topics]
    return snapshot


async def _realtime_catchup(
    outbound: _RealtimeOutbound, from_cursor: int, scopes: list[dict],
    principal: Optional[str], last_acked_cursor: int,
) -> int:
    """Replay-or-snapshot catch-up from `from_cursor` for `scopes`; returns the new cursor.

    Shared by the initial subscribe-with-cursor path and realtime_v1's
    steady-state per-iteration drain, which otherwise duplicate this decision
    verbatim except for the starting cursor. Takes no `WebSocket` — only an
    outbound sink — so ADR-0039 Shore's per-device push loop (Milestone 4)
    can call this directly from its own encrypted-channel context.
    """
    window, events = await asyncio.to_thread(
        get_realtime_replay, from_cursor, scopes, _REALTIME_REPLAY_LIMIT + 1,
    )
    rollover = _realtime_replay_rollover_reason(from_cursor, window, events)
    if rollover:
        snapshot = await _realtime_snapshot(scopes)
        snapshot["cursor_reset"] = rollover == "future_cursor"
        cursor = snapshot["cursor"]
        await _realtime_send(outbound, {"v": 1, "type": "snapshot", "event_id": cursor, "payload": snapshot}, principal, last_acked_cursor)
        return cursor
    for event in events:
        await _realtime_send(outbound, _realtime_envelope(event), principal, last_acked_cursor)
        await asyncio.sleep(0)
    return window["current"]


async def _run_realtime_chat(prepared: dict) -> None:
    async for _chunk in stream_response(
        prepared["effective_message"], prepared["topic"], prepared["agent"],
        prepared["backend"], prepared["model"], prepared["cwd"],
        prepared["context_history"], prepared["asst_msg_id"], prepared["response_timeout"],
        effort=prepared.get("effort"), codex_binding=prepared.get("codex_binding"),
        approved_runtime=prepared.get("approved_runtime"),
        resume_session_id=prepared["resume_session_id"], adhoc=prepared["adhoc"],
        lookback=prepared["lookback"], code_roots=prepared["code_roots"],
        display_prompt=prepared["display_prompt"], source_cwd=prepared["source_cwd"],
        configured_cwd=prepared["configured_cwd"],
        harness=prepared["harness"], provider=prepared["provider"],
        worktree_setup_elapsed_ms=prepared["worktree_setup_elapsed_ms"],
        worktree_isolated=prepared["worktree_isolated"], native_shell=prepared["native_shell"],
        shell_pinned_contents=prepared["shell_pinned_contents"],
        shell_attached_paths=prepared["shell_attached_paths"],
        shell_topic_memory=prepared["shell_topic_memory"],
    ):
        pass


async def _realtime_chat_start(payload: dict, browser_session_id: Optional[str] = None) -> dict:
    try:
        req = ChatRequest(**payload)
        if req.publication is not None or req.publication_expires_at is not None:
            return {"ok":False,"error":"publication_requires_host_control_http","status":403}
    except Exception as exc:
        return {"ok": False, "error": "invalid_frame", "detail": str(exc)}
    if req.route:
        return {"ok": False, "error": "invalid_frame", "detail": "route chains require explicit UI turns"}
    flow_route = canonical_flow_route(req.flow_route)
    flow_run_id = req.flow_run_id if flow_route else None
    if flow_route and not flow_run_id:
        flow_run_id = allocate_id("flow_run")
    topic = _normalize_topic_response(req.topic)
    if isinstance(topic, JSONResponse):
        return {"ok": False, "error": "invalid_frame", "detail": "invalid topic"}
    prepared = await _prepare_chat_turn(
        message=req.message, topic=topic, agent=req.agent, adhoc=req.adhoc,
        lookback=req.lookback, lookback_via_pins=req.lookback_via_pins,
        lookback_excluded_ids=req.lookback_excluded_ids,
        pinned_ids=req.pinned_ids, attached_paths=req.attached_paths,
        include_topic_memory=req.include_topic_memory, source=req.source,
        flow_run_id=flow_run_id, flow_route=flow_route,
        task_workspace=req.task_workspace, task_id=req.task_id,
        provider_confirmation=_provider_confirmation(req, browser_session_id),
    )
    if isinstance(prepared, JSONResponse):
        body = json.loads(prepared.body)
        return {"ok": False, **body, "error": body.get("error", "chat_start_failed"),
                "status": prepared.status_code}
    flow_plan_error = _persist_flow_plan(flow_run_id, flow_route, prepared)
    if flow_plan_error:
        return {
            "ok": False, "error": "flow_plan_persistence_failed",
            "detail": flow_plan_error, "status": 500,
            "msg_id": prepared["asst_msg_id"],
        }
    await maybe_sync()
    task = asyncio.create_task(_run_realtime_chat(prepared), name=f"squid-ws-chat-{prepared['asst_msg_id']}")
    _realtime_chat_tasks.add(task)
    task.add_done_callback(_realtime_chat_tasks.discard)
    return {"ok": True, "msg_id": prepared["asst_msg_id"], "flow_run_id": flow_run_id,
            "task_id": prepared.get("task_id"), "context_ids": prepared.get("context_ids", [])}


def _detach_auth_listener(session_id: Optional[str], q: Optional[asyncio.Queue]) -> None:
    """Remove a connection's auth output queue from its session's listeners.

    No-op when either is unset. The queue may already be gone (an exited
    session's listeners are only dropped at disconnect, and a re-attach swaps
    the queue first), so guard the remove like stream_events does.
    """
    if q is None:
        return
    from .auth_sessions import get_session

    session = get_session(session_id) if session_id else None
    if session is not None:
        try:
            session.listeners.remove(q)
        except ValueError:
            pass


def _attach_auth_listener(session_id: str, browser_session_id: str) -> Optional[tuple[str, asyncio.Queue]]:
    """Re-attach to a still-live session for an idempotent auth.start replay.

    Returns (session_id, queue) to wire the pump, or None when the session is
    already gone (post-exit retention lapsed) — the caller then returns the
    stored result unchanged.
    """
    from .auth_sessions import attach_listener

    session = _browser_auth_session(session_id, browser_session_id)
    if not session:
        return None
    return (session_id, attach_listener(session))


async def _realtime_auth_start(payload: dict, websocket: WebSocket) -> tuple[dict, Optional[tuple[str, asyncio.Queue]]]:
    """Validate and spawn an auth session; return (result, attach).

    Shares its semantic validation with the HTTP /auth/session route via
    _auth_session_validation_error. On success the caller persists the result,
    then realtime_v1 installs the attach so its pump drains the session's
    output. attach is None on failure.
    """
    from .auth_sessions import (
        AuthSessionError, NoLoginCommand, attach_listener, create_session,
    )

    harness = payload.get("harness")
    cols = payload.get("cols", 100)
    rows = payload.get("rows", 10)
    mode = payload.get("mode", "login")
    model = payload.get("model")
    provider = payload.get("provider")
    if provider is not None and not isinstance(provider, str):
        return {"ok": False, "error": "invalid_frame", "detail": "provider must be a string"}, None

    if not isinstance(harness, str) or not harness.strip():
        return {"ok": False, "error": "invalid_frame", "detail": "harness is required"}, None
    if not isinstance(cols, int) or not isinstance(rows, int) or not (20 <= cols <= 500) or not (5 <= rows <= 200):
        return {"ok": False, "error": "invalid_frame", "detail": "cols/rows out of range"}, None
    if mode not in AUTH_SESSION_MODES:
        return {"ok": False, "error": "invalid_frame", "detail": f"Unknown auth-session mode {mode!r}"}, None

    error = _auth_session_validation_error(harness, mode, model)
    if error:
        return {"ok": False, "error": "invalid_frame", "detail": error}, None

    try:
        session = await create_session(harness, cols, rows, mode=mode, model=model,
                                       **({"provider_id": provider} if provider is not None else {}))
    except NoLoginCommand as exc:
        return {"ok": False, "error": "no_login_command", "detail": str(exc)}, None
    except AuthSessionError as exc:
        return {"ok": False, "error": "auth_session_error", "detail": str(exc)}, None

    if mode == "unlock":
        session.browser_session_id = websocket.state.browser_session_id

    result = {
        "ok": True,
        "session_id": session.id,
        "harness": session.harness_id,
        "command": session.display_command,
    }
    return result, (session.id, attach_listener(session))


async def _handle_auth_input(frame: dict, outbound: _RealtimeOutbound, principal: Optional[str], last_acked_cursor: int, *, browser_session_id: str) -> None:
    from .auth_sessions import AuthSessionError, write_input

    request_id = frame.get("request_id")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    session_id = payload.get("session_id")
    data = payload.get("data")
    if not isinstance(request_id, str) or not request_id or not isinstance(session_id, str) or not isinstance(data, str):
        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
        return
    session = _browser_auth_session(session_id, browser_session_id)
    if not session:
        await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": {"ok": False, "error": "unknown_session"}}, principal, last_acked_cursor)
        return
    try:
        write_input(session, data.encode())
    except AuthSessionError as exc:
        await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": {"ok": False, "error": str(exc)}}, principal, last_acked_cursor)
        return
    await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": {"ok": True}}, principal, last_acked_cursor)


async def _handle_auth_resize(frame: dict, outbound: _RealtimeOutbound, principal: Optional[str], last_acked_cursor: int, *, browser_session_id: str) -> None:
    from .auth_sessions import resize

    request_id = frame.get("request_id")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    session_id = payload.get("session_id")
    cols = payload.get("cols")
    rows = payload.get("rows")
    if (not isinstance(request_id, str) or not request_id or not isinstance(session_id, str)
            or not isinstance(cols, int) or not isinstance(rows, int)
            or not (20 <= cols <= 500) or not (5 <= rows <= 200)):
        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
        return
    session = _browser_auth_session(session_id, browser_session_id)
    if not session:
        await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": {"ok": False, "error": "unknown_session"}}, principal, last_acked_cursor)
        return
    resize(session, cols, rows)
    await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": {"ok": True}}, principal, last_acked_cursor)


async def _handle_auth_cancel(frame: dict, outbound: _RealtimeOutbound, principal: Optional[str], last_acked_cursor: int, *, browser_session_id: str) -> None:
    """Fire-and-forget, like _handle_auth_input/_handle_auth_resize — not
    routed through _handle_realtime_mutation's idempotent-mutation path.

    The client (closeAuthPanel, ui/app.js) never reads this command's result
    and mints a fresh request_id on every call (authSend(), not the
    idempotent sendCommand() chat/auth.start use), so persisting a
    (principal, request_id) row for it was pure write-only dead weight: a
    row nothing ever looks up, on every panel close. cancel_session() is
    already safe to call more than once for the same session id (a second
    call just finds nothing left in _sessions and returns False), so no
    idempotency guard is needed to make repeat cancels safe.
    """
    from .auth_sessions import cancel_session

    request_id = frame.get("request_id")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    session_id = payload.get("session_id")
    if not isinstance(request_id, str) or not request_id or not isinstance(session_id, str) or not session_id:
        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
        return
    session = _browser_auth_session(session_id, browser_session_id)
    cancelled = bool(session) and await cancel_session(session_id)
    await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id,
                                "payload": {"ok": True, "cancelled": cancelled, "session_id": session_id}}, principal, last_acked_cursor)


async def _handle_realtime_mutation(websocket: WebSocket, frame: dict, principal: str, outbound: _RealtimeOutbound, last_acked_cursor: int) -> Optional[tuple[str, asyncio.Queue]]:
    message_type = frame.get("type")
    request_id = frame.get("request_id")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    if not isinstance(request_id, str) or not request_id or message_type not in {
        "chat.start", "chat.cancel", "auth.start", "worktree.auto_resolve",
    }:
        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
        return None
    if message_type == "auth.start" and payload.get("mode") == "unlock":
        principal = f"unlock:{websocket.state.browser_session_id}:{principal}"
    fingerprint = _realtime_request_fingerprint(message_type, payload)
    previous = await asyncio.to_thread(get_realtime_request, principal, request_id)
    attach: Optional[tuple[str, asyncio.Queue]] = None
    if previous:
        if previous["request_type"] != message_type or previous["request_hash"] != fingerprint:
            await _realtime_send(outbound, {"v": 1, "type": "error", "request_id": request_id, "payload": {"code": "request_id_conflict"}}, principal, last_acked_cursor)
            return None
        result = previous["result"]
        if message_type == "auth.start" and result.get("ok") and result.get("session_id"):
            # Reconnect replay: re-wire output to this socket for the still-live
            # session. If it already exited and was reaped, the stored result is
            # returned unchanged and the client sees no further output.
            attach = _attach_auth_listener(result["session_id"], websocket.state.browser_session_id)
    elif message_type == "chat.start":
        result = await _realtime_chat_start(payload, websocket.state.browser_session_id)
        result = await asyncio.to_thread(save_realtime_request, principal, request_id, message_type, fingerprint, result)
    elif message_type == "auth.start":
        result, attach = await _realtime_auth_start(payload, websocket)
        result = await asyncio.to_thread(save_realtime_request, principal, request_id, message_type, fingerprint, result)
    elif message_type == "worktree.auto_resolve":
        result = await _realtime_worktree_auto_resolve(payload)
        result = await asyncio.to_thread(save_realtime_request, principal, request_id, message_type, fingerprint, result)
    else:  # chat.cancel
        msg_id = payload.get("msg_id")
        raw_source = payload.get("source")
        source = raw_source if isinstance(raw_source, str) and re.fullmatch(r"[a-z0-9_]{1,64}", raw_source) else "unspecified"
        if not isinstance(msg_id, int):
            result = {"ok": False, "error": "invalid_frame"}
        else:
            changed = await asyncio.to_thread(mark_assistant_cancelled, msg_id, "Cancelled")
            # A message still sitting in the topic queue (never started) has no
            # registered process for kill_proc_by_msg_id to find, so without this
            # it lingers in /queue and keeps the status dot blinking after the
            # chat bubble already shows "Cancelled." (mirrors the HTTP /cmd
            # stop_msg handler — this frame carries no topic, so look it up.)
            drained = 0
            msg_row = await asyncio.to_thread(get_message, msg_id)
            if msg_row and msg_row.get("topic"):
                drained = dispatcher.drain_topic(msg_row["topic"], msg_id=msg_id)
            killed = await asyncio.to_thread(kill_proc_by_msg_id, msg_id)
            result = {"ok": True, "cancelled": changed, "killed": killed, "drained": drained, "msg_id": msg_id, "source": source}
            log.info(
                "realtime chat.cancel msg_id=%s source=%s principal=%s cancelled=%s killed=%s drained=%s",
                msg_id, source, principal, changed, killed, drained,
            )
        result = await asyncio.to_thread(save_realtime_request, principal, request_id, message_type, fingerprint, result)
    await _realtime_send(outbound, {"v": 1, "type": "command.result", "request_id": request_id, "payload": result}, principal, last_acked_cursor)
    return attach


@app.websocket("/ws/v1")
async def realtime_v1(websocket: WebSocket):
    browser_session_id = await asyncio.to_thread(instance_auth.browser_session, websocket)
    if not browser_session_id or websocket.headers.get("origin") != instance_auth.browser_origin(websocket):
        await websocket.close(code=1008, reason="paired same-origin session required")
        return
    websocket.state.browser_session_id = browser_session_id
    await websocket.accept()
    cursor = await asyncio.to_thread(get_realtime_cursor)
    await websocket.send_json({
        "v": 1, "type": "hello", "payload": {
            "supported_versions": list(_REALTIME_SUPPORTED_VERSIONS), "cursor": cursor,
            "replay_limit": _REALTIME_REPLAY_LIMIT,
            "replay_byte_limit": _REALTIME_REPLAY_BYTE_LIMIT,
            "replay_max_age_seconds": _REALTIME_REPLAY_MAX_AGE_SECONDS,
            "heartbeat_seconds": _REALTIME_HEARTBEAT_SECONDS,
        },
    })
    loop = asyncio.get_running_loop()
    outbound = _RealtimeOutbound(_REALTIME_OUTBOUND_QUEUE_LIMIT)
    sender_task = asyncio.create_task(_ws_sender(websocket, outbound))
    scopes: list[dict] = []
    principal: Optional[str] = None
    last_acked_cursor = -1
    generation = _realtime_notifier.generation
    auth_output_q: Optional[asyncio.Queue] = None
    auth_session_id: Optional[str] = None
    last_inbound = loop.time()
    next_ping_at = loop.time() + _REALTIME_HEARTBEAT_SECONDS
    receive_task: Optional[asyncio.Task] = None
    notify_task: Optional[asyncio.Task] = None
    heartbeat_task: Optional[asyncio.Task] = None
    output_task: Optional[asyncio.Task] = None
    output_task_queue = None
    history_tasks: set[asyncio.Task] = set()
    try:
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(websocket.receive_text())
            notify_task = asyncio.create_task(_realtime_notifier.wait(generation))
            heartbeat_task = asyncio.create_task(asyncio.sleep(max(0.0, next_ping_at - loop.time())))
            wait_set = {receive_task, notify_task, heartbeat_task, sender_task} | history_tasks
            if output_task is not None and output_task_queue is not auth_output_q:
                output_task.cancel()
                output_task = None
            if auth_output_q is not None:
                if output_task is None:
                    output_task = asyncio.create_task(auth_output_q.get())
                    output_task_queue = auth_output_q
                wait_set.add(output_task)
            done, pending = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED,
            )
            if not await asyncio.to_thread(instance_auth.browser_session, websocket):
                await _realtime_terminate(websocket, sender_task, 1008, reason="session expired")
                return
            if sender_task in done:
                # The sender died (a send raised, e.g. WebSocketDisconnect on a
                # half-closed peer). Surface its exception so cleanup runs.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                exc = sender_task.exception()
                if exc is not None:
                    raise exc
                break
            for query_task in done & history_tasks:
                history_tasks.remove(query_task)
                query_task.result()  # Surface outbound overflow through normal socket cleanup.
            ephemeral = pending - {sender_task, receive_task, output_task} - history_tasks
            for task in ephemeral:
                task.cancel()
            await asyncio.gather(*ephemeral, return_exceptions=True)
            frame_raw: Optional[str] = None
            binary_frame = False
            if receive_task in done:
                last_inbound = loop.time()
                try:
                    frame_raw = receive_task.result()
                except KeyError:
                    binary_frame = True
                receive_task = None
            if notify_task in done:
                generation = notify_task.result()
            if heartbeat_task in done:
                if loop.time() - last_inbound > _REALTIME_HEARTBEAT_SECONDS * _REALTIME_HEARTBEAT_MISS_LIMIT:
                    await _realtime_terminate(websocket, sender_task, 1001, reason="heartbeat timeout")
                    return
                await _realtime_send(outbound, {"v": 1, "type": "ping", "payload": {}}, principal, last_acked_cursor)
                next_ping_at = loop.time() + _REALTIME_HEARTBEAT_SECONDS
            if output_task is not None and output_task in done:
                chunk = output_task.result()
                output_task = None
                if chunk is None:
                    from .auth_sessions import get_session
                    session = get_session(auth_session_id) if auth_session_id else None
                    # A cancel-driven exit (idle reaper / server-side cancel) pops the
                    # session from _sessions the moment _closed fires, racing this
                    # drain. When the session is already gone, report a failure code
                    # rather than null — the client coerces null to 0 (success), so a
                    # reaped login would otherwise read as a completed auth.
                    returncode = session.returncode if session is not None else -1
                    session_id = auth_session_id
                    _detach_auth_listener(auth_session_id, auth_output_q)
                    auth_output_q = None
                    auth_session_id = None
                    await _realtime_send(outbound, {
                        "v": 1, "type": "auth.done",
                        "payload": {"session_id": session_id, "returncode": returncode},
                    }, principal, last_acked_cursor)
                else:
                    await _realtime_send(outbound, {
                        "v": 1, "type": "auth.output",
                        "payload": {
                            "session_id": auth_session_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    }, principal, last_acked_cursor)
            if binary_frame:
                await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
                continue
            if frame_raw is not None:
                if len(frame_raw.encode("utf-8")) > _REALTIME_MAX_FRAME_BYTES:
                    await _realtime_terminate(websocket, sender_task, 1009, final_frame={
                        "v": 1, "type": "error", "payload": {"code": "frame_too_large"},
                    })
                    return
                try:
                    frame = json.loads(frame_raw)
                except ValueError:
                    await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
                    continue
                if not isinstance(frame, dict):
                    await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
                    continue
            else:
                frame = None
            if frame is not None:
                if frame.get("v") not in _REALTIME_SUPPORTED_VERSIONS:
                    await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "unsupported_version", "supported_versions": list(_REALTIME_SUPPORTED_VERSIONS)}}, principal, last_acked_cursor)
                    continue
                message_type = frame.get("type")
                if message_type == "subscribe":
                    subscribe_payload = frame.get("payload", {})
                    client_id = subscribe_payload.get("client_id")
                    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
                        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_client_id"}}, principal, last_acked_cursor)
                        continue
                    requested = subscribe_payload.get("scopes", [])
                    authorized_scopes = _authorize_realtime_scopes(requested)
                    if authorized_scopes is None:
                        await _realtime_send(outbound, {
                            "v": 1, "type": "error",
                            "payload": {"code": "unauthorized_scope"},
                        }, principal, last_acked_cursor)
                        continue
                    principal = f"local:{client_id}"
                    scopes = authorized_scopes
                    requested_cursor = subscribe_payload.get("cursor")
                    await _realtime_send(outbound, {"v": 1, "type": "subscribed", "payload": {"scopes": scopes}}, principal, last_acked_cursor)
                    if not isinstance(requested_cursor, int) or requested_cursor < 0:
                        snapshot = await _realtime_snapshot(scopes)
                        snapshot["cursor_reset"] = True
                        cursor = snapshot["cursor"]
                        await _realtime_send(outbound, {"v": 1, "type": "snapshot", "event_id": cursor, "payload": snapshot}, principal, last_acked_cursor)
                    else:
                        cursor = await _realtime_catchup(outbound, requested_cursor, scopes, principal, last_acked_cursor)
                    generation = _realtime_notifier.generation
                elif message_type == "unsubscribe":
                    scopes = []
                    cursor = await asyncio.to_thread(get_realtime_cursor)
                    await _realtime_send(outbound, {"v": 1, "type": "unsubscribed", "payload": {}}, principal, last_acked_cursor)
                elif message_type == "health.check":
                    result = ({"ok": True, "status": "ok", "boot_time": BOOT_TIME, "version": SQUID_VERSION}
                              if principal else {"ok": False, "error": "client_identity_required"})
                    await _realtime_send(outbound, {
                        "v": 1, "type": "command.result", "request_id": frame.get("request_id"),
                        "payload": result,
                    }, principal, last_acked_cursor)
                elif message_type in {"history.page", "history.around", "history.check"}:
                    request_id = frame.get("request_id")
                    valid_request_id = isinstance(request_id, str) and 1 <= len(request_id) <= 128
                    if valid_request_id:
                        try:
                            request_id.encode("utf-8")
                        except UnicodeEncodeError:
                            valid_request_id = False
                    if not valid_request_id:
                        await _realtime_send(outbound, {
                            "v": 1, "type": "error", "payload": {"code": "invalid_request_id"},
                        }, principal, last_acked_cursor)
                    elif not principal or len(history_tasks) >= _REALTIME_HISTORY_QUERY_LIMIT:
                        error = "client_identity_required" if not principal else "history_query_busy"
                        await _realtime_send(outbound, _history_query_frame(request_id, {
                            "ok": False, "error": error}), principal, last_acked_cursor)
                    else:
                        history_tasks.add(asyncio.create_task(
                            _run_history_query(frame, outbound, principal, last_acked_cursor)))
                elif message_type in {"chat.start", "chat.cancel", "auth.start", "worktree.auto_resolve"}:
                    if not principal:
                        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "client_identity_required"}}, principal, last_acked_cursor)
                    else:
                        attach = await _handle_realtime_mutation(websocket, frame, principal, outbound, last_acked_cursor)
                        if attach is not None:
                            _detach_auth_listener(auth_session_id, auth_output_q)
                            auth_session_id, auth_output_q = attach
                elif message_type in {"auth.input", "auth.resize", "auth.cancel"}:
                    if not principal:
                        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "client_identity_required"}}, principal, last_acked_cursor)
                    elif message_type == "auth.input":
                        await _handle_auth_input(frame, outbound, principal, last_acked_cursor, browser_session_id=browser_session_id)
                    elif message_type == "auth.resize":
                        await _handle_auth_resize(frame, outbound, principal, last_acked_cursor, browser_session_id=browser_session_id)
                    else:
                        await _handle_auth_cancel(frame, outbound, principal, last_acked_cursor, browser_session_id=browser_session_id)
                elif message_type == "ping":
                    await _realtime_send(outbound, {"v": 1, "type": "pong", "payload": {}}, principal, last_acked_cursor)
                elif message_type == "ack":
                    ack_payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
                    acked = ack_payload.get("event_id")
                    if not isinstance(acked, int) or acked < 0:
                        await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "invalid_frame"}}, principal, last_acked_cursor)
                        continue
                    if acked > cursor:
                        log.debug("Realtime ack %s ahead of server cursor %s — clamped", acked, cursor)
                        acked = cursor
                    last_acked_cursor = max(last_acked_cursor, acked)
                elif message_type == "pong":
                    pass
                else:
                    await _realtime_send(outbound, {"v": 1, "type": "error", "payload": {"code": "unsupported_type"}}, principal, last_acked_cursor)
            if scopes:
                cursor = await _realtime_catchup(outbound, cursor, scopes, principal, last_acked_cursor)
    except WebSocketDisconnect:
        pass
    except _RealtimeSlowConsumer:
        await _realtime_terminate(websocket, sender_task, 1013, final_frame={
            "v": 1, "type": "error", "payload": {"code": "slow_consumer", "resumable": True},
        })
    except asyncio.CancelledError:
        # Bare connection-scope cancellation (server shutdown or TestClient
        # teardown). The child tasks are cancelled below; swallowing the
        # cancellation lets the handler return normally so the app task
        # reaches anyio.sleep_forever(), whose own cancellation the outer
        # cancel scope absorbs — surfacing a bare CancelledError here would
        # leak it to the TestClient future instead.
        pass
    finally:
        _detach_auth_listener(auth_session_id, auth_output_q)
        # Cancel (but do not await) the sender and any in-flight child tasks.
        # Awaiting here re-enters anyio's cancel-delivery retry during the
        # cancellation unwind and turns a clean teardown into an uncaught
        # CancelledError; cancelled tasks are reaped by the running loop.
        for task in (receive_task, notify_task, heartbeat_task, output_task):
            if task is not None:
                task.cancel()
        for task in history_tasks:
            task.cancel()
        sender_task.cancel()


from .watch_api import router as watch_router
app.include_router(watch_router)
from .continuation_api import router as continuation_router
app.include_router(continuation_router)
from .route_api import router as route_router
app.include_router(route_router)
from .task_publication_api import router as task_publication_router
app.include_router(task_publication_router)

if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")


def _tailscale_serve_status(port: int) -> Optional[dict]:
    """Read-only snapshot of Tailscale/`tailscale serve` state for `port`.
    Returns None if the `tailscale` binary isn't available or a status call
    fails. Never mutates anything — safe to call just for reporting.
    """
    if not shutil.which("tailscale"):
        return None
    try:
        status_raw = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5,
        )
        serve_raw = subprocess.run(
            ["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5,
        )
        if status_raw.returncode != 0 or serve_raw.returncode != 0:
            return None
        status = json.loads(status_raw.stdout)
        serve_cfg = json.loads(serve_raw.stdout)
        web = serve_cfg.get("Web", {})
        tcp = serve_cfg.get("TCP", {})
    except Exception:
        return None

    https_ready = any(
        host_port.endswith(":443")
        and entry.get("Handlers", {}).get("/", {}).get("Proxy") == f"http://127.0.0.1:{port}"
        for host_port, entry in web.items()
    )
    # Deliberately a --tcp forward, not --http: `tailscale serve --http`
    # routes by Host header (like name-based virtual hosting) even for plain
    # HTTP, so a client hitting the raw Tailscale IP sends `Host: <ip>:<port>`,
    # which matches no rule — tailscaled's own mux 404s before squid ever
    # sees the request. `--tcp` is a raw L4 forward with no Host matching, so
    # it works by IP.
    ip_ready = tcp.get(str(port), {}).get("TCPForward") == f"127.0.0.1:{port}"

    return {
        "https_ready": https_ready,
        "ip_ready": ip_ready,
        "dns_name": status.get("Self", {}).get("DNSName", "").rstrip("."),
        "magic_dns": bool(status.get("CurrentTailnet", {}).get("MagicDNSEnabled")),
        "tailscale_ip": next(iter(status.get("TailscaleIPs", [])), ""),
    }


def _configure_tailscale_serve(port: int) -> None:
    """Best-effort, never fatal — mirrors bin/start.sh's tailscale section so
    a pipx-installed `agentsquid` gets the same one-time persistent
    `tailscale serve` config a source checkout's bin/start.sh already set up.
    Tailscale remembers this across reboots; safe to check/re-run every start.

    Publishes two rules, since either one can be the one that actually works
    depending on tailnet settings:
      - https://<dns-name>/            default HTTPS (443), shortest URL, but
                                        needs MagicDNS enabled to resolve.
      - http://<tailscale-ip>:<port>/  works even with MagicDNS off. This is
                                        a raw `--tcp` forward, not `--http`:
                                        `tailscale serve --http` routes by
                                        Host header even for plain HTTP, so a
                                        client hitting the IP directly (Host:
                                        <ip>:<port>) matches no rule and gets
                                        tailscaled's own 404 before squid ever
                                        sees the request. `--tcp` is an L4
                                        forward with no Host matching, so it
                                        works by IP. `https://<ip>:<port>/`
                                        still wouldn't work over this or any
                                        rule — Tailscale's cert only covers
                                        the DNS name, not the IP. The traffic
                                        is still WireGuard-encrypted at the
                                        tailnet layer either way.

    `tailscale serve` supports multiple concurrent rules at different ports
    on the same node, so exposing another local service on this machine
    (e.g. Ollama or an oMLX server) doesn't require touching either rule —
    add an independent one for it instead:
        tailscale serve --bg --https=<their-port> 127.0.0.1:<their-port>
    """
    info = _tailscale_serve_status(port)
    if info is None:
        return
    try:
        if not info["https_ready"]:
            if not subprocess.run(
                ["tailscale", "serve", "--bg", f"127.0.0.1:{port}"],
                capture_output=True, timeout=5,
            ).returncode == 0:
                log.warning(
                    "tailscale serve (https) failed — squid will run locally only "
                    "(127.0.0.1:%s). To enable later, run: tailscale serve --bg 127.0.0.1:%s",
                    port, port,
                )
        if not info["ip_ready"]:
            # A pre-upgrade squid may have left a --http=<port> rule on this
            # exact port (the bug this --tcp switch fixes). `tailscale serve
            # --tcp` on a port already serving --http fails outright rather
            # than replacing it ("cannot serve TCP; already serving web on
            # <port>"), so clear it first. Best-effort: errors (e.g. no such
            # rule exists) are expected and ignored — only the --tcp add
            # below is actually load-bearing.
            subprocess.run(
                ["tailscale", "serve", f"--http={port}", "off"],
                capture_output=True, timeout=5,
            )
            if not subprocess.run(
                ["tailscale", "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"],
                capture_output=True, timeout=5,
            ).returncode == 0:
                log.warning(
                    "tailscale serve --tcp=%s failed — IP:port access won't work. "
                    "To enable later, run: tailscale serve --bg --tcp=%s tcp://127.0.0.1:%s",
                    port, port, port,
                )

        dns = info["dns_name"] or "<machine-name>"
        if info["magic_dns"]:
            log.info("tailscale serve: https://%s/", dns)
        else:
            log.info(
                "tailscale serve: https://%s/ (MagicDNS is off for this tailnet — "
                "this name may not resolve; enable it in the admin console, or use "
                "the IP URL below)",
                dns,
            )
        if info["tailscale_ip"]:
            log.info("tailscale serve: http://%s:%s/", info["tailscale_ip"], port)
    except Exception as e:
        log.warning("tailscale serve check failed: %s", e)


def _lifecycle_paths() -> tuple[Path, Path]:
    squid_state_dir = Path.home() / ".squid"
    logs_dir = squid_state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return squid_state_dir / "agentsquid.pid", logs_dir / "boot.log"


def _read_lifecycle_pid(pid_file: Path) -> Optional[int]:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_looks_like_agentsquid(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    command = result.stdout.strip()
    return "agentsquid" in command or "agent.server" in command


def _health_ok(host: str, port: int) -> bool:
    return _health_json(host, port) is not None


def _health_json(host: str, port: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=0.5) as response:
            if not 200 <= response.status < 300:
                return None
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _request_host_lifecycle(host: str, port: int, command: str) -> bool:
    try:
        if not ipaddress.IPv4Address(host).is_loopback:
            return False
        token = instance_auth.host_token(create=True)
        request = urllib.request.Request(f"http://{host}:{port}/instance/{command}", method="POST")
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return 200 <= response.status < 300 and bool(payload.get("ok"))
    except Exception:
        return False


def _request_http_restart(host: str, port: int) -> Optional[int]:
    before = _health_json(host, port)
    if not before or not _request_host_lifecycle(host, port, "restart"):
        return None

    print("restarting agentsquid via running server", end="", flush=True)
    before_boot = before.get("boot_time")
    saw_down = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current = _health_json(host, port)
        if not current:
            saw_down = True
            print(".", end="", flush=True)
            continue
        if saw_down or current.get("boot_time") != before_boot:
            print("")
            print(f"agentsquid restarted -> http://{host}:{port}")
            return 0
        print(".", end="", flush=True)
    print("")
    print("restart requested; agentsquid stayed reachable")
    return 0


def _request_http_shutdown(host: str, port: int) -> Optional[int]:
    if not _health_ok(host, port) or not _request_host_lifecycle(host, port, "shutdown"):
        return None

    print("stopping agentsquid via running server", end="", flush=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if not _health_ok(host, port):
            print("")
            print("agentsquid stopped")
            return 0
        print(".", end="", flush=True)
    print("")
    print("agentsquid stop was requested but the server is still reachable", file=sys.stderr)
    return 1


def _print_lifecycle_usage() -> None:
    shore_commands = "|login|pair" if shore_enabled() else ""
    print(
        f"usage: agentsquid [--fg|--reload|start|stop|restart|status|pair-browser{shore_commands}]\n\n"
        "commands:\n"
        "  start     run agentsquid in the background\n"
        "  stop      stop the background agentsquid process\n"
        "  restart   stop then start the background process\n"
        "  status    show whether agentsquid is running\n"
        "  pair-browser  print a single-use local browser pairing link\n"
        + ("  login     register this machine as your Shore host\n"
           "  pair      pair a browser (--requests to approve pairing requests)\n"
           if shore_enabled() else "")
        + "\n"
        + "bare agentsquid runs in the foreground; Ctrl+C stops it."
    )


def _print_browser_pairing_help() -> None:
    print("If your browser asks to pair, run agentsquid pair-browser on the host "
          "and open the printed link within 5 minutes.", flush=True)
    print("Use /remote in the paired browser to add a Tailscale device.", flush=True)


def _print_tailscale_access(port: int) -> None:
    """Print whatever tailnet URLs are currently configured for `port`, if
    any. Read-only — reports what _configure_tailscale_serve already set up
    in the running server process; does not itself change any config.
    """
    info = _tailscale_serve_status(port)
    if not info:
        return
    if info["https_ready"]:
        dns = info["dns_name"] or "<machine-name>"
        suffix = "" if info["magic_dns"] else "  (MagicDNS is off — may not resolve)"
        print(f"  https://{dns}/{suffix}", flush=True)
    if info["ip_ready"] and info["tailscale_ip"]:
        print(f"  http://{info['tailscale_ip']}:{port}/", flush=True)


def _lifecycle_status(host: str, port: int) -> int:
    pid_file, _boot_log = _lifecycle_paths()
    pid = _read_lifecycle_pid(pid_file)
    if pid and _pid_running(pid):
        print(f"agentsquid is running (PID {pid}) -> http://{host}:{port}")
        _print_tailscale_access(port)
        return 0
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
    if _health_ok(host, port):
        print(f"agentsquid is running at http://{host}:{port} (no PID file)")
        _print_tailscale_access(port)
        return 0
    print("agentsquid is not running")
    return 1


def _lifecycle_start(host: str, port: int) -> int:
    pid_file, boot_log = _lifecycle_paths()
    pid = _read_lifecycle_pid(pid_file)
    if pid and _pid_running(pid):
        print(f"agentsquid is already running (PID {pid}) -> http://{host}:{port}")
        _print_tailscale_access(port)
        return 0
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
    if _health_ok(host, port):
        print(f"agentsquid is already running at http://{host}:{port} (no PID file)")
        _print_tailscale_access(port)
        return 0

    with boot_log.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent.server", "--fg"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")

    print("starting agentsquid", end="", flush=True)
    for _ in range(20):
        time.sleep(0.5)
        if _health_ok(host, port):
            print("")
            print(f"agentsquid is up -> http://{host}:{port}")
            _print_tailscale_access(port)
            _print_browser_pairing_help()
            return 0
        if proc.poll() is not None:
            pid_file.unlink(missing_ok=True)
            print("")
            print(f"agentsquid failed to start; check {boot_log}", file=sys.stderr)
            return 1
        print(".", end="", flush=True)
    print("")
    print(f"warning: agentsquid did not respond within 10s; check {boot_log}", file=sys.stderr)
    return 1


def _lifecycle_stop(_host: str, _port: int) -> int:
    pid_file, _boot_log = _lifecycle_paths()
    pid = _read_lifecycle_pid(pid_file)
    if not pid:
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)
        if _health_ok(_host, _port):
            stopped = _request_http_shutdown(_host, _port)
            if stopped is not None:
                return stopped
            print(
                f"agentsquid is running at http://{_host}:{_port} but has no PID file "
                "and does not support HTTP shutdown",
                file=sys.stderr,
            )
            return 1
        print("agentsquid is not running")
        return 0
    if not _pid_running(pid):
        pid_file.unlink(missing_ok=True)
        if _health_ok(_host, _port):
            stopped = _request_http_shutdown(_host, _port)
            if stopped is not None:
                return stopped
            print(
                f"agentsquid is running at http://{_host}:{_port} but its PID file was stale "
                "and it does not support HTTP shutdown",
                file=sys.stderr,
            )
            return 1
        print("agentsquid is not running; removed stale PID file")
        return 0
    if not _pid_looks_like_agentsquid(pid):
        print(f"refusing to stop PID {pid} because it does not look like agentsquid", file=sys.stderr)
        return 1

    print(f"stopping agentsquid (PID {pid})")
    try:
        os.kill(pid, 15)
    except OSError as exc:
        print(f"failed to stop agentsquid: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            pid_file.unlink(missing_ok=True)
            print("agentsquid stopped")
            return 0
        time.sleep(0.25)
    print("agentsquid was signaled but is still running", file=sys.stderr)
    return 1


def _lifecycle_restart(host: str, port: int) -> int:
    restarted = _request_http_restart(host, port)
    if restarted is not None:
        return restarted
    stopped = _lifecycle_stop(host, port)
    if stopped != 0:
        return stopped
    return _lifecycle_start(host, port)


def _run_lifecycle_command(command: str, host: str, port: int) -> int:
    if command == "start":
        return _lifecycle_start(host, port)
    if command == "stop":
        return _lifecycle_stop(host, port)
    if command == "restart":
        return _lifecycle_restart(host, port)
    if command == "status":
        return _lifecycle_status(host, port)
    _print_lifecycle_usage()
    return 2


def main():
    import ipaddress
    import socket
    import uvicorn

    host = _cfg["server"]["host"]
    port = _cfg["server"]["port"]
    args = [arg for arg in sys.argv[1:] if arg != "--fg"]
    # Also support direct `python -m agent.server` invocations; the installed
    # console entry point dispatches clients lazily through agent.cli.
    if args and args[0] == "pair-browser":
        from .instance_auth import pair_browser_cli
        return pair_browser_cli(args[1:])
    if args and args[0] in {"watch", "tools"}:
        from .watch_cli import main as watch_main
        sys.exit(watch_main(args))
    if args and args[0] in {"login", "pair"} and not shore_enabled():
        print(f"Unknown command: {args[0]}", file=sys.stderr)
        sys.exit(2)
    if args and args[0] == "login":
        from .shore import login
        sys.exit(login(args[1:]))
    if args and args[0] == "pair":
        from .shore import pair
        sys.exit(pair(args[1:], f"http://{host}:{port}"))
    lifecycle_commands = {"start", "stop", "restart", "status"}
    if args and args[0] in {"-h", "--help"}:
        _print_lifecycle_usage()
        return
    if args and args[0] in lifecycle_commands:
        if any(arg in {"-h", "--help"} for arg in args[1:]):
            _print_lifecycle_usage()
            return
        sys.exit(_run_lifecycle_command(args[0], host, port))
    unknown = [arg for arg in args if arg != "--reload"]
    if unknown:
        print(f"unknown agentsquid option: {unknown[0]}", file=sys.stderr)
        _print_lifecycle_usage()
        sys.exit(2)

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
            f"  tailscale serve --bg 127.0.0.1:{port}"
        )

    # Probe the port before handing off to uvicorn. uvicorn's ASGI lifespan
    # (this app's @app.on_event("startup") hooks — orphaned-pending recovery,
    # stalled-flow resume) runs *before* uvicorn attempts its own socket bind,
    # so a second process racing an already-running squid on this port would
    # otherwise mutate shared DB state before ever learning it can't take
    # over the port. Failing fast here, ahead of the ASGI lifespan, closes
    # that window.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Match uvicorn's own bind_socket(), which always sets this before
        # binding. Without it, connections this restart just tore down
        # (open UI tabs, SSE streams) sitting in TIME_WAIT on this port make
        # the probe fail for up to a minute even though uvicorn's real bind
        # — which does set SO_REUSEADDR — would have succeeded immediately.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        probe.close()
    except OSError as e:
        sys.exit(
            f"ERROR: port {port} on {host} is already in use ({e}).\n"
            "Another squid instance is likely already running — stop it before\n"
            "starting a new one, rather than launching a second process."
        )

    _configure_tailscale_serve(port)

    log.info("Starting squid on http://%s:%s", host, port)
    # All logging (including uvicorn's own) goes to the rotating file handler
    # above, not the console — so without a direct print here, running
    # `agentsquid` in a terminal produces zero visible output while it blocks
    # in the foreground, indistinguishable from a hang. bin/start.sh avoids
    # this by backgrounding the process and polling /health itself before
    # printing this same confirmation; a bare `agentsquid` invocation has no
    # equivalent, so it has to speak for itself here instead.
    print(f"squid is up → http://{host}:{port}  (running in the foreground — Ctrl+C to stop)", flush=True)
    _print_tailscale_access(port)
    _print_browser_pairing_help()
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
