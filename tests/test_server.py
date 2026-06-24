import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import server
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
                )
            ]

    chunks = asyncio.run(run())

    assert captured["cwd"] == "/tmp/squid"
    assert captured["code_roots"] == ["/Users/haebin/Work/squid"]
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
            assert '# retained comment' in config_path.read_text()
            assert f'- "{tmp_path / "workspace"}"' in config_path.read_text()

            visible = client.get("/localfile", params={"path": str(requested)})
            assert visible.status_code == 200
            assert visible.text == "visible now"
    finally:
        server._cfg.clear()
        server._cfg.update(original_cfg)
        server._LOCALFILE_ROOTS[:] = original_roots
