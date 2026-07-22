import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent import creds
from agent import server
from agent import stats_db
from agent.providers import Gauge, Provider


def _config_yaml(root: Path) -> str:
    return f'''# retained comment
server:
  host: "127.0.0.1"
  port: 8000
  localfile_roots:
    - "{root}"
agent:
  first_byte_timeout: 300
  response_timeout: 1800
providers:
  openai:
    label: GPT
    color: "#7070A0"
    auth: {{type: subscription}}
    gauge: codex
'''


def test_worktree_diff_blocked_until_synced():
    gitdiff = {
        "name": "GitDiff",
        "worktree_repo": "/tmp/wt",
        "worktree_status": "conflict",
        "files": [{"path": "app.py"}],
    }

    assert server._worktree_diff_blocked(gitdiff) == {"app.py": "conflict"}

    gitdiff["worktree_status"] = "synced"
    assert server._worktree_diff_blocked(gitdiff) is None


def test_worktree_diff_missing_status_is_legacy_unblocked():
    gitdiff = {
        "name": "GitDiff",
        "worktree_repo": "/tmp/wt",
        "files": [{"path": "app.py"}],
    }

    assert server._worktree_diff_blocked(gitdiff) is None


class FinishedWorker:
    def position_of(self, seq):
        return 0


def test_stream_response_passes_agent_cwd_and_code_roots_separately():
    captured = {}

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        out_q = asyncio.Queue()
        await out_q.put(None)
        return out_q, 3, FinishedWorker()

    async def run():
        with patch.object(server.dispatcher, "dispatch", fake_dispatch), \
             patch("agent.server.update_assistant_message"):
            return [
                chunk
                async for chunk in server.stream_response(
                    "edit app.txt",
                    topic="squid",
                    agent="codex",
                    backend="codex",
                    model=None,
                    cwd="/tmp/squid",
                    context_history=[],
                    asst_msg_id=123,
                    code_roots=["/Users/haebin/Work/squid"],
                    display_prompt="user prompt",
                )
            ]

    chunks = asyncio.run(run())

    assert captured["cwd"] == "/tmp/squid"
    assert captured["code_roots"] == ["/Users/haebin/Work/squid"]
    assert captured["display_prompt"] == "user prompt"
    assert chunks[0].startswith("event: meta")
    assert chunks[-1] == "event: done\ndata: \n\n"


def test_sse_event_preserves_multiline_data_fields():
    assert server.sse_event("status", "first\nsecond\n\nthird") == (
        "event: status\n"
        "data: first\n"
        "data: second\n"
        "data: \n"
        "data: third\n\n"
    )


def test_status_recovers_final_content_from_text_events(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "half baked", "session-1", "pending")

    client = TestClient(server.app)

    pending = client.get(f"/chat/{msg_id}/status")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"
    assert pending.json()["content"] == "half baked"

    stats_db.insert_run_event(msg_id, 0, "text", "final from events")
    stats_db.insert_run_event(msg_id, 1, "done", None)
    done = client.get(f"/chat/{msg_id}/status")
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["content"] == "final from events"


def test_status_recovers_status_raw_from_run_events(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "half baked", "session-1", "pending")
    stats_db.insert_run_event(msg_id, 0, "status", "Working...\n")
    stats_db.insert_run_event(msg_id, 1, "text", "final from events")
    stats_db.insert_run_event(msg_id, 2, "status", "Done")
    stats_db.insert_run_event(msg_id, 3, "done", None)

    client = TestClient(server.app)
    done = client.get(f"/chat/{msg_id}/status")

    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["content"] == "final from events"
    assert done.json()["status_raw"] == "Working...\nDone"


def test_status_hydrates_pending_response_from_run_events(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "", None, "pending")
    stats_db.insert_run_event(msg_id, 0, "status", "Reading from DB\n")
    stats_db.insert_run_event(msg_id, 1, "tool", json.dumps({"name": "Read", "path": "agent/server.py"}))
    stats_db.insert_run_event(msg_id, 2, "text", "partial answer")
    stats_db.insert_run_event(msg_id, 3, "stats", json.dumps({"session_id": "session-1"}))

    client = TestClient(server.app)
    pending = client.get(f"/chat/{msg_id}/status")

    assert pending.status_code == 200
    body = pending.json()
    assert body["status"] == "pending"
    assert body["content"] == "partial answer"
    assert body["status_raw"] == "Reading from DB\n"
    assert json.loads(body["context"]) == [{"name": "Read", "path": "agent/server.py"}]
    assert body["session_id"] == "session-1"


def test_status_keeps_pending_partial_content_without_text_events(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "partial snapshot", "session-1", "pending")
    stats_db.insert_run_event(msg_id, 0, "status", "Working...")
    stats_db.insert_run_event(msg_id, 1, "done", None)

    client = TestClient(server.app)
    done = client.get(f"/chat/{msg_id}/status")

    assert done.status_code == 200
    assert done.json()["status"] == "pending"
    assert done.json()["content"] == "partial snapshot"


def test_event_replay_ignores_stale_error_when_message_is_done(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "final text", "session-1", "done")
    stats_db.insert_run_event(msg_id, 0, "text", "final text")
    stats_db.insert_run_event(msg_id, 1, "error", "late transport error")

    client = TestClient(server.app)
    with client.stream("GET", f"/chat/{msg_id}/events") as res:
        body = "".join(res.iter_text())

    assert res.status_code == 200
    assert "data:final text" in body
    assert "event: done" in body
    assert "late transport error" not in body


def test_stream_response_emits_heartbeat_during_idle_gap():
    """During a silent gap (no chunks) the SSE loop emits keepalive comments so
    proxies/mobile links don't drop the connection and force 'recovering' polling."""
    class _SlowQueue:
        """Times out the first get() (idle gap), then finishes the turn."""
        def __init__(self):
            self._n = 0

        async def get(self):
            self._n += 1
            if self._n == 1:
                await asyncio.sleep(3600)  # never returns before poll timeout
            return None  # end the turn

    async def fake_dispatch(**kwargs):
        return _SlowQueue(), 3, FinishedWorker()

    async def run():
        with patch.object(server.dispatcher, "dispatch", fake_dispatch), \
             patch("agent.server._OUT_Q_POLL_TIMEOUT", 0.01), \
             patch("agent.server._HEARTBEAT_TICKS", 1), \
             patch("agent.server.update_assistant_message"):
            return [
                chunk
                async for chunk in server.stream_response(
                    "prompt", topic="squid", agent="claude", backend="claude",
                    model=None, cwd="/tmp/squid", context_history=[], asst_msg_id=123,
                )
            ]

    chunks = asyncio.run(run())
    assert any(chunk == ": ping\n\n" for chunk in chunks)
    assert any(chunk == "event: done\ndata: \n\n" for chunk in chunks)


def test_stream_response_does_not_promote_status_to_final_content():
    async def fake_dispatch(**kwargs):
        out_q = asyncio.Queue()
        await out_q.put({"_status": "Working on it..."})
        await out_q.put(None)
        return out_q, 3, FinishedWorker()

    async def run():
        with patch.object(server.dispatcher, "dispatch", fake_dispatch), \
             patch("agent.server.update_assistant_message") as update_message:
            chunks = [
                chunk
                async for chunk in server.stream_response(
                    "prompt",
                    topic="squid",
                    agent="codex",
                    backend="codex",
                    model=None,
                    cwd="/tmp/squid",
                    context_history=[],
                    asst_msg_id=123,
                )
            ]
            return chunks, update_message.call_args

    chunks, update_call = asyncio.run(run())
    assert any(chunk == "event: status\ndata: Working on it...\n\n" for chunk in chunks)
    assert not any(chunk.startswith("data:Working on it...") for chunk in chunks)
    assert update_call.args[1] == ""
    assert update_call.args[3] == "done"


def test_drain_to_completion_continues_after_idle_timeout():
    async def run():
        out_q = asyncio.Queue()
        calls = 0

        async def fake_wait_for(awaitable, timeout):
            nonlocal calls
            awaitable.close()
            calls += 1
            if calls == 1:
                raise asyncio.TimeoutError()
            if calls == 2:
                return "final after idle"
            return None

        with patch("agent.server.asyncio.wait_for", fake_wait_for), \
             patch("agent.server.update_assistant_message") as update_message:
            await server._drain_to_completion(out_q, 123, "", "", None)
            return update_message.call_args

    update_call = asyncio.run(run())
    assert update_call.args[1] == "final after idle"
    assert update_call.args[3] == "done"


def test_drain_timeout_keeps_message_pending_instead_of_empty_error():
    async def run():
        with patch("agent.server.update_assistant_message") as update_message:
            await server._drain_to_completion(
                asyncio.Queue(),
                123,
                "",
                "Still working...",
                None,
                drain_timeout=0,
            )
            return update_message.call_args

    update_call = asyncio.run(run())
    assert update_call.args[1] == ""
    assert update_call.args[3] == "pending"
    assert update_call.kwargs["only_if_pending"] is True


def test_backend_native_chat_command_detection_excludes_squid_commands():
    assert server._is_backend_native_chat_command("/usage")
    assert server._is_backend_native_chat_command("/cost")
    assert server._is_backend_native_chat_command("/model opus")
    assert not server._is_backend_native_chat_command("/clear")
    assert not server._is_backend_native_chat_command("/s cost")
    assert not server._is_backend_native_chat_command("plain prompt")


def test_compact_is_no_longer_squid_owned():
    """/compact was Squid's own clear-alias (ADR-0013); it's now passed through
    natively so interactive-protocol backends can run their own compaction."""
    assert server._is_backend_native_chat_command("/compact")
    assert "compact" not in server._SQUID_CHAT_COMMANDS


def test_chat_rejects_server_side_hidden_route_chain():
    client = TestClient(server.app)
    res = client.post("/chat", json={
        "route": "#squid@codex>@revucla",
        "message": "review this",
        "topic": "squid",
        "agent": "codex",
    })

    assert res.status_code == 400
    assert res.json()["error"] == "route chains are executed as explicit UI turns"


def test_canonical_flow_route_strips_whitespace_without_reordering_clauses():
    # Comma-separated parts must keep their original order: an Origin
    # Broadcast atom can inherit an omitted half from its nearest
    # fully-explicit predecessor by rolling anchor (ADR-0032), so reordering
    # parts can change what they resolve to — it is not a safe normalization.
    route = " @review>@codex , #squid@codex>@test "

    assert server.canonical_flow_route(route) == "@review>@codex,#squid@codex>@test"


def test_backend_native_chat_commands_bypass_context_augmentation_for_any_agent():
    cases = [
        ("/usage", "claude-live"),
        ("/cost", "codex"),
        ("/model opus", "opencode"),
    ]

    for native_command, backend_id in cases:
        captured = {}

        async def fake_dispatch(**kwargs):
            captured.update(kwargs)
            out_q = asyncio.Queue()
            await out_q.put(None)
            return out_q, 1, FinishedWorker()

        client = TestClient(server.app)

        with patch("agent.server.get_agent", return_value={
                "backend": backend_id, "model": None, "cwd": "/tmp/project",
             }), \
             patch("agent.server.upsert_topic"), \
             patch("agent.server.get_topic_session", return_value=None), \
             patch("agent.server.get_context_history") as get_context_history, \
             patch("agent.server.topic_memory_squid_config", return_value={
                 "code_roots": ["/Users/haebin/Work/squid"],
             }), \
             patch("agent.server.code_roots_prompt_block") as code_roots_prompt_block, \
             patch("agent.server.read_topic_memory") as read_topic_memory, \
             patch("agent.server.get_messages_by_ids") as get_messages_by_ids, \
             patch("agent.server.insert_user_message", return_value=201), \
             patch("agent.server.insert_assistant_message", return_value=202), \
             patch("agent.server.update_assistant_message"), \
             patch.object(server.dispatcher, "dispatch", fake_dispatch):
            res = client.post("/chat", json={
                "message": native_command,
                "topic": "squid",
                "agent": "clive",
                "adhoc": True,
                "lookback": 3,
                "pinned_ids": [123],
                "include_topic_memory": True,
            })

        assert res.status_code == 200
        assert captured["prompt"] == native_command
        assert captured["context_history"] == []
        assert captured["code_roots"] == []
        get_context_history.assert_not_called()
        code_roots_prompt_block.assert_not_called()
        read_topic_memory.assert_not_called()
        get_messages_by_ids.assert_not_called()


def test_chat_response_exposes_message_id_header():
    async def fake_dispatch(**kwargs):
        out_q = asyncio.Queue()
        await out_q.put(None)
        return out_q, 1, FinishedWorker()

    client = TestClient(server.app)

    with patch("agent.server.get_agent", return_value={
            "backend": "codex", "model": None, "cwd": "/tmp/project",
         }), \
         patch("agent.server.upsert_topic"), \
         patch("agent.server.get_topic_session", return_value=None), \
         patch("agent.server.topic_memory_squid_config", return_value={}), \
         patch("agent.server.insert_user_message", return_value=201) as insert_user_message, \
         patch("agent.server.insert_assistant_message", return_value=202), \
         patch("agent.server.update_assistant_message"), \
         patch.object(server.dispatcher, "dispatch", fake_dispatch):
        res = client.post("/chat", json={
            "message": "hello",
            "topic": "squid",
            "agent": "codex",
            "source": "system",
        })

    assert res.status_code == 200
    assert res.headers["X-Squid-Msg-Id"] == "202"
    assert insert_user_message.call_args.kwargs["source"] == "system"


def test_chat_allocates_short_flow_run_id_for_routed_turn():
    async def fake_dispatch(**kwargs):
        out_q = asyncio.Queue()
        await out_q.put(None)
        return out_q, 1, FinishedWorker()

    client = TestClient(server.app)

    with patch("agent.server.allocate_id", return_value="1") as allocate_id, \
         patch("agent.server.get_agent", return_value={
            "backend": "codex", "model": None, "cwd": "/tmp/project",
         }), \
         patch("agent.server.upsert_topic"), \
         patch("agent.server.get_topic_session", return_value=None), \
         patch("agent.server.topic_memory_squid_config", return_value={}), \
         patch("agent.server.insert_user_message", return_value=201) as insert_user_message, \
         patch("agent.server.insert_assistant_message", return_value=202) as insert_assistant_message, \
         patch("agent.server.update_assistant_message"), \
         patch.object(server.dispatcher, "dispatch", fake_dispatch):
        res = client.post("/chat", json={
            "message": "hello",
            "topic": "squid",
            "agent": "codex",
            "flow_route": "#squid@codex>@revuqwen",
        })

    assert res.status_code == 200
    assert res.headers["X-Squid-Flow-Run-Id"] == "1"
    allocate_id.assert_called_once_with("flow_run")
    assert insert_user_message.call_args.kwargs["flow_run_id"] == "1"
    assert insert_assistant_message.call_args.kwargs["flow_run_id"] == "1"


def test_clear_command_kills_only_session_lane():
    client = TestClient(server.app)

    with patch("agent.server.get_active_agent_for_topic", return_value="codex"), \
         patch("agent.server.get_agent", return_value={"name": "codex"}), \
         patch("agent.server.kill_procs_by_topic", return_value=1) as kill_procs, \
         patch("agent.server.clear_topic_session") as clear_session:
        res = client.post("/cmd", json={"command": "clear", "topic": "squid"})

    assert res.status_code == 200
    assert res.json() == {"ok": True, "agent": "codex"}
    kill_procs.assert_called_once_with("squid", agent="codex", adhoc=False)
    clear_session.assert_called_once_with("squid", "codex")


def test_topic_session_includes_session_turn_count():
    client = TestClient(server.app)

    with patch("agent.server.get_agent", return_value={"name": "codex"}), \
         patch("agent.server.get_topic_session", return_value={"session_id": "sid-1", "cwd": "/tmp"}), \
         patch("agent.server.get_session_injected_context", return_value={}), \
         patch("agent.server.get_session_turn_count", return_value=0):
        res = client.get("/topics/squid/session?agent=codex")

    assert res.status_code == 200
    assert res.json()["session_turn_count"] == 0

    with patch("agent.server.get_agent", return_value={"name": "codex"}), \
         patch("agent.server.get_topic_session", return_value=None):
        res = client.get("/topics/squid/session?agent=codex")

    assert res.status_code == 200
    assert res.json()["session_turn_count"] == 0

    with patch("agent.server.get_agent", return_value=None):
        res = client.get("/topics/squid/session?agent=missing")

    assert res.status_code == 404


def test_clear_command_rejects_unknown_agent():
    client = TestClient(server.app)

    with patch("agent.server.get_agent", return_value=None), \
         patch("agent.server.kill_procs_by_topic") as kill_procs, \
         patch("agent.server.clear_topic_session") as clear_session:
        res = client.post("/cmd", json={"command": "clear", "topic": "squid", "agent": "missing"})

    assert res.status_code == 400
    assert res.json()["error"] == "agent not found: missing"
    kill_procs.assert_not_called()
    clear_session.assert_not_called()


def test_health_returns_harnesses_and_providers_without_backend_aliases():
    client = TestClient(server.app)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert "backends" not in body
    assert body["version"] == server.SQUID_VERSION
    claudecode = next(h for h in body["harnesses"] if h["id"] == "claudecode")
    assert claudecode["default_provider"] == "anthropic"
    assert body["providers"]["anthropic"]["gauge"]["type"] == "claude"
    assert body["providers"]["anthropic"]["color"].startswith("#")


def test_public_agent_config_includes_provider_color():
    item = server._public_agent_config({
        "name": "haiku",
        "harness": "claudecode",
        "provider": "anthropic",
        "model": None,
        "cwd": None,
    })

    assert item["color"] == item["provider_color"]
    assert item["color"].startswith("#")
    assert item["provider_label"] == "Claude"


def test_provider_static_quota_is_normalized_with_no_harness_involved():
    client = TestClient(server.app)
    response = client.get("/quota/provider/opencode")

    assert response.status_code == 200
    assert response.json() == {
        "status": "static",
        "text": "Free tier",
        "title": "no reset",
        "used_percent": None,
        "reset_at": None,
    }


def test_provider_quota_404s_for_unknown_provider():
    client = TestClient(server.app)
    response = client.get("/quota/provider/does-not-exist")

    assert response.status_code == 404


def test_codex_quota_normalizes_weekly_primary_window(monkeypatch):
    async def fake_quota_codex():
        return server.JSONResponse({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 50,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 600,
                },
                "secondary_window": None,
            },
        })

    monkeypatch.setattr(server, "quota_codex", fake_quota_codex)
    provider = Provider(id="openai", label="GPT", gauge=Gauge(type="codex"))

    response = asyncio.run(server._quota_snapshot_for_provider(provider, "openai"))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["used_percent"] == 50
    assert body["seven_day"]["used_percent"] == 50
    assert body["reset_at"] == body["seven_day"]["reset_at"]


def test_codex_quota_prefers_cli_auth_and_account_header(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"rate_limit": {"primary_window": {"used_percent": 12}}}

    class FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, impersonate=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["impersonate"] = impersonate
            return FakeResponse()

    fake_requests = types.SimpleNamespace(AsyncSession=FakeAsyncSession)
    fake_curl = types.SimpleNamespace(requests=fake_requests)
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_curl)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)
    monkeypatch.setattr(creds, "get_codex_cli_auth", lambda: {
        "access_token": "header.payload.signature",
        "account_id": "acct_123",
    })
    monkeypatch.setattr(creds, "get_codex_token", lambda: "stale.saved.token")
    monkeypatch.setattr(server, "_codex_bearer_header", lambda token: f"Bearer {token}")

    response = asyncio.run(server.quota_codex())

    assert response.status_code == 200
    assert captured["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert captured["headers"]["Authorization"] == "Bearer header.payload.signature"
    assert captured["headers"]["ChatGPT-Account-ID"] == "acct_123"
    assert captured["headers"]["OpenAI-Beta"] == "codex-1"
    assert captured["headers"]["originator"] == "Codex Desktop"


class _FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _patch_balance_fetch(monkeypatch, payload, status_code=200, captured=None):
    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, timeout=None):
            if captured is not None:
                captured["url"] = url
                captured["headers"] = headers or {}
            return _FakeHttpResponse(payload, status_code)

    monkeypatch.setattr(httpx, "AsyncClient", lambda: FakeAsyncClient())


def _balance_provider(provider_id: str, gauge: str, base_url: str) -> Provider:
    return Provider(
        id=provider_id, label=provider_id.title(), color="#4D6BFE",
        base_url=base_url, auth_type="api_key", api_key="sk-test",
        gauge=Gauge(type=gauge),
    )


def test_kimi_balance_quota_is_normalized(monkeypatch):
    captured = {}
    _patch_balance_fetch(monkeypatch, {
        "code": 0, "status": True,
        "data": {"available_balance": 24.92686, "voucher_balance": 4.92686, "cash_balance": 20},
    }, captured=captured)
    monkeypatch.setattr(creds, "get_max_budget", lambda gauge: None)
    provider = _balance_provider("kimi", "kimi", "https://api.moonshot.ai/anthropic")

    response = asyncio.run(server._quota_snapshot_for_provider(provider, "kimi"))

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ok", "text": "$24.93", "raw": 24.92686,
        "used_percent": None, "reset_at": None,
        "title": "Kimi balance · $24.93",
    }
    assert captured["url"] == "https://api.moonshot.ai/v1/users/me/balance"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_kimi_balance_quota_reports_spend_against_max_budget(monkeypatch):
    _patch_balance_fetch(monkeypatch, {
        "code": 0, "status": True, "data": {"available_balance": 20.0},
    })
    monkeypatch.setattr(creds, "get_max_budget", lambda gauge: 50.0)
    provider = _balance_provider("kimi", "kimi", "https://api.moonshot.ai/anthropic")

    response = asyncio.run(server._quota_snapshot_for_provider(provider, "kimi"))

    body = json.loads(response.body)
    assert body["max_budget"] == 50.0
    assert body["spent"] == 30.0
    assert body["max_budget_pct"] == 60
    assert body["title"] == "Kimi · $30.00 spent of $50.00"


def test_kimi_balance_quota_502s_when_upstream_rejects(monkeypatch):
    _patch_balance_fetch(monkeypatch, {}, status_code=401)
    monkeypatch.setattr(creds, "get_max_budget", lambda gauge: None)
    provider = _balance_provider("kimi", "kimi", "https://api.moonshot.ai/anthropic")

    response = asyncio.run(server._quota_snapshot_for_provider(provider, "kimi"))

    assert response.status_code == 502


def test_deepseek_balance_quota_still_normalized(monkeypatch):
    captured = {}
    _patch_balance_fetch(monkeypatch, {
        "balance_infos": [{"currency": "USD", "total_balance": "12.34"}],
    }, captured=captured)
    monkeypatch.setattr(creds, "get_max_budget", lambda gauge: None)
    provider = _balance_provider("deepseek", "deepseek", "https://api.deepseek.com/anthropic")

    response = asyncio.run(server._quota_snapshot_for_provider(provider, "deepseek"))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["text"] == "$12.34"
    assert body["title"] == "DeepSeek balance · $12.34"
    assert captured["url"] == "https://api.deepseek.com/user/balance"


def test_max_budget_endpoints_are_gauge_scoped(monkeypatch):
    saved = {}
    monkeypatch.setattr(creds, "save_max_budget", lambda gauge, amount: saved.update({gauge: amount}))
    monkeypatch.setattr(creds, "clear_max_budget", lambda gauge: saved.pop(gauge, None))
    client = TestClient(server.app)

    assert client.post("/config/kimi/max-budget", json={"amount": 25}).status_code == 200
    assert saved == {"kimi": 25}
    assert client.post("/config/kimi/max-budget", json={"amount": -1}).status_code == 400
    assert client.post("/config/claude/max-budget", json={"amount": 25}).status_code == 404
    assert client.delete("/config/kimi/max-budget").status_code == 200
    assert saved == {}


def test_agent_rejects_unconfigured_harness():
    client = TestClient(server.app)
    response = client.post("/config/agents", json={
        "name": "invalid-harness-test",
        "harness": "not-configured",
    })

    assert response.status_code == 400
    assert "Unknown harness" in response.json()["error"]


def test_agent_save_supports_legacy_backend_not_null_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    with stats_db._connect() as conn:
        conn.execute("""
            CREATE TABLE agents (
                name TEXT PRIMARY KEY,
                backend TEXT NOT NULL,
                harness TEXT,
                provider TEXT,
                model TEXT,
                cwd TEXT,
                timeout INTEGER,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        """)

    changed = stats_db.upsert_agent(
        "oc",
        "opencode",
        "opencode",
        "opencode/deepseek-v4-flash-free",
        None,
    )

    assert changed is False
    saved = stats_db.get_agent("oc")
    assert saved["backend"] == "opencode:opencode"
    assert saved["harness"] == "opencode"
    assert saved["provider"] == "opencode"
    assert saved["model"] == "opencode/deepseek-v4-flash-free"


def test_yaml_config_can_be_read_validated_and_atomically_updated(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = state_dir / "squid.yaml"
    config_path.write_text(_config_yaml(tmp_path))
    client = TestClient(server.app)
    original_cfg = dict(server._cfg)
    original_roots = list(server._LOCALFILE_ROOTS)

    try:
        with patch("agent.config._USER_CONFIG", config_path), \
             patch.object(server, "_USER_CONFIG", config_path):
            loaded = client.get("/config/yaml")
            assert loaded.status_code == 200
            data = loaded.json()
            assert data["content"].startswith("# retained comment")

            updated = data["content"].replace("port: 8000", "port: 8123")
            saved = client.put("/config/yaml", json={
                "content": updated,
                "revision": data["revision"],
            })
            assert saved.status_code == 200
            assert saved.json()["restart_required"] is True
            assert "port: 8123" in config_path.read_text()
            assert "port: 8000" in (state_dir / "squid.yaml.bak").read_text()

            invalid = client.put("/config/yaml", json={
                "content": updated.replace('host: "127.0.0.1"', 'host: "0.0.0.0"'),
                "revision": saved.json()["revision"],
            })
            assert invalid.status_code == 400
            assert "127.0.0.1" in invalid.json()["error"]
            assert config_path.read_text() == updated
    finally:
        server._cfg.clear()
        server._cfg.update(original_cfg)
        server._LOCALFILE_ROOTS[:] = original_roots


def test_api_routes_are_registered_before_static_ui():
    client = TestClient(server.app)

    bookmarks = client.get("/bookmarks")
    roots = client.get("/config/localfile-roots")

    assert bookmarks.status_code == 200
    assert "items" in bookmarks.json()
    assert roots.status_code == 200
    assert "roots" in roots.json()


def test_localfile_serves_paths_outside_configured_roots(tmp_path):
    existing_root = tmp_path / "existing"
    existing_root.mkdir()
    project = tmp_path / "workspace" / "project"
    project.mkdir(parents=True)
    requested = project / "notes.md"
    requested.write_text("visible now")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [existing_root.resolve()]
        visible = client.get("/localfile", params={"path": str(requested)})
        assert visible.status_code == 200
        assert visible.headers["content-type"].startswith("text/markdown")
        assert visible.text == "visible now"
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_serves_unknown_extension_text_inline(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    text_file = root / "example.customthing"
    text_file.write_text("plain text without a known extension\n")
    binary_file = root / "binary.customthing"
    binary_file.write_bytes(b"\x00\x01\x02\x03")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        text_res = client.get("/localfile", params={"path": str(text_file)})
        assert text_res.status_code == 200
        assert text_res.headers["content-type"].startswith("text/plain")
        assert "plain text" in text_res.text

        binary_res = client.get("/localfile", params={"path": str(binary_file)})
        assert binary_res.status_code == 200
        assert binary_res.headers["content-type"].startswith("application/octet-stream")
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_rename_can_move_across_directories(tmp_path):
    root = tmp_path / "workspace"
    src = root / "src"
    src.mkdir(parents=True)
    note = root / "notes.txt"
    note.write_text("move me")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        moved = src / "notes.txt"
        res = client.post("/localfile/rename", json={
            "path": str(note),
            "to_path": str(moved),
        })
        assert res.status_code == 200
        assert res.json()["path"] == str(moved.resolve())
        assert not note.exists()
        assert moved.read_text() == "move me"

        outside = tmp_path / "outside.txt"
        res2 = client.post("/localfile/rename", json={
            "path": str(moved),
            "to_path": str(outside),
        })
        assert res2.status_code == 200
        assert outside.read_text() == "move me"
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_markdown_preview_strips_frontmatter_and_preserves_escapes(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    md_file = root / "notes.md"
    md_file.write_text(
        "---\n"
        "title: `literal`\n"
        "---\n\n"
        "# Heading\n\n"
        "```js\n"
        "const value = 'code block';\n"
        "```\n\n"
        "Backtick: `code`, pipe: \\|, closing tag: </script>\n\n"
        "| Col |\n"
        "| --- |\n"
        "| A \\| B |\n"
    )
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        res = client.get("/localfile", params={"path": str(md_file), "render": "1"})
        assert res.status_code == 200
        assert "marked.parse(" in res.text
        assert "&#96;" not in res.text
        assert "title: `literal`" not in res.text
        assert "marked.setOptions({gfm:true,breaks:true})" in res.text
        assert '"\\n# Heading\\n\\n```js\\nconst value' in res.text
        assert "pipe: \\\\|" in res.text
        assert "| A \\\\| B |" in res.text
        assert "closing tag: </script>" not in res.text
        assert "closing tag: <\\/script>" in res.text
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_same_origin_fetch_metadata_allows_proxy_origin_mismatch(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    md_file = root / "notes.md"
    md_file.write_text("# Heading\n")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        allowed = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "same-origin",
                "origin": "https://squid.example.test",
            },
        )
        assert allowed.status_code == 200
        assert "marked.parse(" in allowed.text

        blocked = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "cross-site",
                "origin": "https://evil.example.test",
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"] == "cross-origin file reads are not allowed"

    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_allows_forwarded_origin_match(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    md_file = root / "notes.md"
    md_file.write_text("# Heading\n")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        res = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "origin": "https://squid.example.test",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "squid.example.test",
            },
        )
        assert res.status_code == 200
        assert "marked.parse(" in res.text
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_markdown_preview_allows_same_site_document_navigation(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    md_file = root / "notes.md"
    md_file.write_text("# Heading\n")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        allowed = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "origin": "https://squid.example.test",
            },
        )
        assert allowed.status_code == 200
        assert "marked.parse(" in allowed.text

        minimal_metadata = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "same-site",
                "origin": "https://squid.example.test",
            },
        )
        assert minimal_metadata.status_code == 200
        assert "marked.parse(" in minimal_metadata.text

        cross_site_navigation = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "origin": "https://evil.example.test",
            },
        )
        assert cross_site_navigation.status_code == 200
        assert "marked.parse(" in cross_site_navigation.text

    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_markdown_preview_allows_document_accept_without_fetch_metadata(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    md_file = root / "notes.md"
    html_file = root / "community.html"
    txt_file = root / "notes.txt"
    md_file.write_text("# Heading\n")
    html_file.write_text("<!doctype html><title>Community</title><h1>Community</h1>\n")
    txt_file.write_text("plain text\n")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        allowed = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "origin": "https://squid.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert allowed.status_code == 200
        assert "marked.parse(" in allowed.text

        html_preview = client.get(
            "/localfile",
            params={"path": str(html_file)},
            headers={
                "origin": "https://squid.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert html_preview.status_code == 200
        assert "Community" in html_preview.text
        assert html_preview.headers["content-type"].startswith("text/html")

        html_cross_site_navigation = client.get(
            "/localfile",
            params={"path": str(html_file)},
            headers={
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "origin": "https://evil.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert html_cross_site_navigation.status_code == 200
        assert "Community" in html_cross_site_navigation.text

        raw_md = client.get(
            "/localfile",
            params={"path": str(md_file)},
            headers={
                "origin": "https://squid.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert raw_md.status_code == 403

        rendered_txt = client.get(
            "/localfile",
            params={"path": str(txt_file), "render": "1"},
            headers={
                "origin": "https://squid.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert rendered_txt.status_code == 403

        blocked = client.get(
            "/localfile",
            params={"path": str(md_file), "render": "1"},
            headers={
                "sec-fetch-site": "cross-site",
                "origin": "https://evil.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"] == "cross-origin file reads are not allowed"

        blocked_html = client.get(
            "/localfile",
            params={"path": str(html_file)},
            headers={
                "sec-fetch-site": "cross-site",
                "origin": "https://evil.example.test",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        assert blocked_html.status_code == 403
        assert blocked_html.json()["error"] == "cross-origin file reads are not allowed"
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots
