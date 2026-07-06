import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import server
from agent import stats_db
from agent.backends import Backend, Gauge


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
backends:
  codex:
    driver: codex
    color: "#7070A0"
    gauge: codex
'''


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


def test_backend_native_chat_command_detection_excludes_squid_commands():
    assert server._is_backend_native_chat_command("/usage")
    assert server._is_backend_native_chat_command("/cost")
    assert server._is_backend_native_chat_command("/model opus")
    assert not server._is_backend_native_chat_command("/clear")
    assert not server._is_backend_native_chat_command("/s cost")
    assert not server._is_backend_native_chat_command("plain prompt")


def test_backend_native_chat_commands_bypass_context_augmentation_for_any_agent():
    cases = [
        ("/usage", "claude-live", Backend("claude-live", "claude", protocol="interactive-cli")),
        ("/cost", "codex", Backend("codex", "codex")),
        ("/model opus", "opencode", Backend("opencode", "opencode")),
    ]

    for native_command, backend_id, backend in cases:
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
             patch("agent.server.get_backend", return_value=backend), \
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
         patch("agent.server.get_backend", return_value=Backend("codex", "codex")), \
         patch("agent.server.upsert_topic"), \
         patch("agent.server.get_topic_session", return_value=None), \
         patch("agent.server.topic_memory_squid_config", return_value={}), \
         patch("agent.server.insert_user_message", return_value=201), \
         patch("agent.server.insert_assistant_message", return_value=202), \
         patch("agent.server.update_assistant_message"), \
         patch.object(server.dispatcher, "dispatch", fake_dispatch):
        res = client.post("/chat", json={
            "message": "hello",
            "topic": "squid",
            "agent": "codex",
        })

    assert res.status_code == 200
    assert res.headers["X-Squid-Msg-Id"] == "202"


def test_clear_command_kills_only_session_lane():
    client = TestClient(server.app)

    with patch("agent.server.get_active_agent_for_topic", return_value="codex"), \
         patch("agent.server.kill_procs_by_topic", return_value=1) as kill_procs, \
         patch("agent.server.clear_topic_session") as clear_session:
        res = client.post("/cmd", json={"command": "clear", "topic": "squid"})

    assert res.status_code == 200
    assert res.json() == {"ok": True, "agent": "codex"}
    kill_procs.assert_called_once_with("squid", agent="codex", adhoc=False)
    clear_session.assert_called_once_with("squid", "codex")


def test_health_returns_configured_backend_driver_and_color():
    client = TestClient(server.app)
    response = client.get("/health")

    assert response.status_code == 200
    backends = response.json()["backends"]
    assert backends["claude"]["driver"] == "claude"
    assert backends["claude"]["color"].startswith("#")
    assert "env" not in backends["claude"]
    assert "settings" not in backends["claude"]
    assert backends["claude"]["gauge"]["type"] == "claude"


def test_backend_static_quota_is_normalized_from_configuration():
    client = TestClient(server.app)
    backend = Backend(
        "qwen", "codex", gauge=Gauge("static", "Local", "No provider quota")
    )
    with patch("agent.server.get_backend", return_value=backend):
        response = client.get("/quota/backend/qwen")

    assert response.status_code == 200
    assert response.json() == {
        "status": "static",
        "text": "Local",
        "title": "No provider quota",
        "used_percent": None,
        "reset_at": None,
    }


def test_agent_rejects_unconfigured_backend():
    client = TestClient(server.app)
    response = client.post("/config/agents", json={
        "name": "invalid-backend-test",
        "backend": "not-configured",
    })

    assert response.status_code == 400
    assert "not configured" in response.json()["error"]


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


def test_file_root_can_expand_to_an_edited_parent_and_applies_immediately(tmp_path):
    existing_root = tmp_path / "existing"
    existing_root.mkdir()
    project = tmp_path / "workspace" / "project"
    project.mkdir(parents=True)
    requested = project / "notes.md"
    requested.write_text("visible now")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = state_dir / "squid.yaml"
    config_path.write_text(_config_yaml(existing_root))
    client = TestClient(server.app)
    original_cfg = dict(server._cfg)
    original_roots = list(server._LOCALFILE_ROOTS)

    try:
        with patch("agent.config._USER_CONFIG", config_path), \
             patch.object(server, "_USER_CONFIG", config_path):
            server._LOCALFILE_ROOTS[:] = server._localfile_roots_from(
                server._validate_config_content(config_path.read_text())
            )
            blocked = client.get("/localfile", params={"path": str(requested)})
            assert blocked.status_code == 403

            allowed = client.post("/config/localfile-roots", json={
                "path": str(requested),
                "root": str(tmp_path / "workspace"),
            })
            assert allowed.status_code == 200
            assert allowed.json()["added"] is True
            roots = client.get("/config/localfile-roots")
            assert roots.status_code == 200
            assert str(tmp_path / "workspace") in roots.json()["roots"]
            assert '# retained comment' in config_path.read_text()
            assert f'- "{tmp_path / "workspace"}"' in config_path.read_text()

            visible = client.get("/localfile", params={"path": str(requested)})
            assert visible.status_code == 200
            assert visible.headers["content-type"].startswith("text/markdown")
            assert visible.text == "visible now"
    finally:
        server._cfg.clear()
        server._cfg.update(original_cfg)
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
