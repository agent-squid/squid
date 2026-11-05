import pytest
from fastapi.testclient import TestClient
from agent import effort, harnesses, server, stats_db


def test_overrides_and_argument_validation(monkeypatch):
    config = effort.validate_effort_config({
        "args": ["--thinking", "{value}"], "levels": {"low": "Quick"},
        "providers": {"other": {}}, "models": {"deep-model": {"high": "Thorough"}},
    })
    monkeypatch.setitem(harnesses._HARNESS_CONFIG["codex"], "effort", config)
    assert effort.effort_args("codex", "openai", None, None) == ()
    assert effort.effort_args("codex", "openai", None, "low") == ("--thinking", "low")
    assert effort.effort_args("codex", "other", "deep-model", "high") == ("--thinking", "high")
    with pytest.raises(ValueError, match="Unsupported effort"):
        effort.effort_args("codex", "other", None, "low")
    assert next(h for h in harnesses.list_harnesses() if h["id"] == "codex")["effort"] == config


@pytest.mark.parametrize("config", [{"levels": {"low": "Low"}}, {"args": ["--effort", "{value}"], "levels": ["low"]}, {"args": ["--effort", "{value}"], "levels": {}, "models": {"test": []}}])
def test_reject_invalid_config(config):
    with pytest.raises(ValueError):
        effort.validate_effort_config(config)


def test_agent_effort_round_trip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "effort.db")
    stats_db.init_db()
    client = TestClient(server.app)
    body = {"name": "effort-test", "harness": "codex", "provider": "openai", "effort": "high"}
    assert client.post("/config/agents", json=body).status_code == 200
    assert stats_db.get_agent("effort-test")["effort"] == "high"
    assert next(a for a in client.get("/config/agents").json() if a["name"] == "effort-test")["effort"] == "high"
    assert not stats_db.upsert_agent("effort-test", "codex", "openai", None, effort="high")
    assert stats_db.upsert_agent("effort-test", "codex", "openai", None, effort="low")
    body["effort"] = "bogus"
    assert client.post("/config/agents", json=body).status_code == 400
    assert stats_db.get_agent("effort-test")["effort"] == "low"
    body["effort"] = None
    assert client.post("/config/agents", json=body).status_code == 200
    assert stats_db.get_agent("effort-test")["effort"] is None
