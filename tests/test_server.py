import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import server
from agent.backends import Backend, Gauge


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
