import asyncio
import json
import sqlite3
import subprocess
import sys
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from agent import creds
from agent import server
from agent import stats_db
from agent import worktree as worktree_mod
from agent import providers as providers_mod
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


def test_validate_config_content_accepts_missing_shell_timeout(tmp_path):
    parsed = server._validate_config_content(_config_yaml(tmp_path))
    assert "shell_timeout" not in parsed["agent"]


def test_validate_config_content_accepts_valid_shell_timeout(tmp_path):
    content = _config_yaml(tmp_path).replace(
        "  response_timeout: 1800\n",
        "  response_timeout: 1800\n  shell_timeout: 90\n",
    )
    parsed = server._validate_config_content(content)
    assert parsed["agent"]["shell_timeout"] == 90


@pytest.mark.parametrize("transport", ["auto", "websocket", "sse"])
def test_validate_config_content_accepts_realtime_transport(tmp_path, transport):
    content = _config_yaml(tmp_path).replace("agent:\n", f"realtime:\n  transport: {transport}\nagent:\n")
    parsed = server._validate_config_content(content)
    assert parsed["realtime"]["transport"] == transport


def test_validate_config_content_defaults_realtime_transport_to_auto(tmp_path):
    parsed = server._validate_config_content(_config_yaml(tmp_path))
    assert server.realtime_transport(parsed) == "auto"


@pytest.mark.parametrize("realtime", ["websocket", "{transport: invalid}"])
def test_validate_config_content_rejects_invalid_realtime_config(tmp_path, realtime):
    content = _config_yaml(tmp_path).replace("agent:\n", f"realtime: {realtime}\nagent:\n")
    with pytest.raises(ValueError, match="realtime"):
        server._validate_config_content(content)


def test_realtime_config_endpoint_uses_effective_mode(monkeypatch):
    monkeypatch.setattr(server, "REALTIME_TRANSPORT", "sse")
    response = TestClient(server.app).get("/config/realtime")
    assert response.json() == {"transport": "sse"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad_value", ["0", "-5", "true", "\"abc\""])
def test_validate_config_content_rejects_invalid_shell_timeout(tmp_path, bad_value):
    content = _config_yaml(tmp_path).replace(
        "  response_timeout: 1800\n",
        f"  response_timeout: 1800\n  shell_timeout: {bad_value}\n",
    )
    with pytest.raises(ValueError, match="agent.shell_timeout must be a positive integer"):
        server._validate_config_content(content)


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
    gitdiff["worktree_status"] = "resolved"
    assert server._worktree_diff_blocked(gitdiff) is None
    gitdiff["worktree_status"] = "discarded"
    assert server._worktree_diff_blocked(gitdiff) is None


def test_worktree_diff_missing_status_is_legacy_unblocked():
    gitdiff = {
        "name": "GitDiff",
        "worktree_repo": "/tmp/wt",
        "files": [{"path": "app.py"}],
    }

    assert server._worktree_diff_blocked(gitdiff) is None


def test_annotations_api_marks_and_unmarks_bad_response(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "fix it")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "bad output", None)

    client = TestClient(server.app)
    res = client.post("/annotations", json={
        "msg_id": msg_id,
        "kind": "bad_response",
        "payload": {"reasons": ["incomplete"]},
    })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    listed = client.get("/annotations?kind=bad_response").json()["items"]
    assert listed[0]["msg_id"] == msg_id
    assert json.loads(listed[0]["payload"]) == {"reasons": ["incomplete"]}

    res = client.delete(f"/annotations/bad_response/{msg_id}")
    assert res.status_code == 200
    assert client.get("/annotations?kind=bad_response").json()["items"] == []


def test_revert_diff_reports_failure_when_patch_does_not_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    (repo / "app.py").write_text("changed\n")
    diff = subprocess.run(["git", "diff", "--binary"], cwd=repo, text=True, check=True, stdout=subprocess.PIPE).stdout
    (repo / "app.py").write_text("base\n")

    user_id = stats_db.insert_user_message("squid", "codex", "change app")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "done", None, context=json.dumps([{
        "name": "GitDiff",
        "repo": str(repo),
        "cwd": str(repo),
        "source": str(repo),
        "file_count": 1,
        "files": [{"status": "M", "path": "app.py"}],
        "diff": diff,
    }]))

    res = TestClient(server.app).post(f"/chat/{msg_id}/revert", json={"repo": str(repo)})

    assert res.status_code == 409
    body = res.json()
    assert body["ok"] is False
    assert body["reverted"] == []
    assert body["failed"][0]["file"] == "app.py"
    assert stats_db.get_diff_revert_eligibility(msg_id, str(repo)) == {"app.py": "revertable"}


def test_revert_diff_restores_target_repo_file(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    (repo / "style.css").write_text("body {}\n")
    subprocess.run(["git", "add", "app.py", "style.css"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    (repo / "app.py").write_text("changed\n")
    (repo / "style.css").write_text("body { color: red; }\n")
    diff = subprocess.run(["git", "diff", "--binary"], cwd=repo, text=True, check=True, stdout=subprocess.PIPE).stdout

    user_id = stats_db.insert_user_message("squid", "codex", "change files")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "done", None, context=json.dumps([{
        "name": "GitDiff",
        "repo": str(repo),
        "cwd": str(repo),
        "source": str(repo),
        "file_count": 2,
        "files": [
            {"status": "M", "path": "app.py"},
            {"status": "M", "path": "style.css"},
        ],
        "diff": diff,
    }]))

    res = TestClient(server.app).post(f"/chat/{msg_id}/revert", json={"repo": str(repo)})

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert sorted(data["reverted"]) == ["app.py", "style.css"]
    assert data["failed"] == []
    assert (repo / "app.py").read_text() == "base\n"
    assert (repo / "style.css").read_text() == "body {}\n"
    assert stats_db.get_diff_revert_eligibility(msg_id, str(repo)) == {
        "app.py": "reverted",
        "style.css": "reverted",
    }


def test_discard_worktree_blocker_marks_conflict_row_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    wt = worktree_mod.ensure_worktree(repo, "squid", "42")
    stats_db.save_worktree("squid", "42", str(repo), str(wt), worktree_mod.branch_name("squid", "42"))
    stats_db.mark_worktree_status("squid", "42", str(repo), "conflict")

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/42/worktree/discard", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    rows = stats_db.get_worktrees("squid", "42")
    assert len(rows) == 1
    assert rows[0]["status"] == "discarded"
    assert not wt.exists()


def test_discard_worktree_blocker_handles_missing_registry_row(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    wt = worktree_mod.ensure_worktree(repo, "squid", "43")

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/43/worktree/discard", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert not wt.exists()


def test_discard_worktree_blocker_is_idempotent_when_already_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/44/worktree/discard", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True, "already_resolved": True}


def test_retry_worktree_resolution_promotes_manual_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    worktree_mod.ensure_worktree(repo, "squid", "50")
    wt_a = worktree_mod.worktree_path(repo, "squid", "50")
    worktree_mod.ensure_worktree(repo, "squid", "51")
    wt_b = worktree_mod.worktree_path(repo, "squid", "51")
    stats_db.save_worktree("squid", "51", str(repo), str(wt_b), worktree_mod.branch_name("squid", "51"))
    (wt_a / "app.py").write_text("first\n")
    worktree_mod.sync_after_turn(repo, "squid", "50", msg_id=50)
    (wt_b / "app.py").write_text("second\n")
    assert worktree_mod.sync_after_turn(repo, "squid", "51", msg_id=51) == ["app.py"]
    stats_db.mark_worktree_status("squid", "51", str(repo), "conflict")

    integration = worktree_mod.integration_worktree_path(repo, "squid", "51")
    (integration / "app.py").write_text("resolved\n")

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/51/worktree/retry", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert (repo / "app.py").read_text() == "resolved\n"
    rows = stats_db.get_worktrees("squid", "51")
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"


def test_retry_worktree_resolution_promotes_stale_pending_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    wt = worktree_mod.ensure_worktree(repo, "squid", "52")
    stats_db.save_worktree("squid", "52", str(repo), str(wt), worktree_mod.branch_name("squid", "52"))
    (wt / "app.py").write_text("stale pending\n")

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/52/worktree/retry", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert (repo / "app.py").read_text() == "stale pending\n"
    assert stats_db.get_worktrees("squid", "52") == []
    assert not wt.exists()


def test_retry_worktree_resolution_handles_missing_registry_row(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    worktree_mod.ensure_worktree(repo, "squid", "60")
    wt_a = worktree_mod.worktree_path(repo, "squid", "60")
    worktree_mod.ensure_worktree(repo, "squid", "61")
    wt_b = worktree_mod.worktree_path(repo, "squid", "61")
    (wt_a / "app.py").write_text("first\n")
    worktree_mod.sync_after_turn(repo, "squid", "60", msg_id=60)
    (wt_b / "app.py").write_text("second\n")
    assert worktree_mod.sync_after_turn(repo, "squid", "61", msg_id=61) == ["app.py"]

    integration = worktree_mod.integration_worktree_path(repo, "squid", "61")
    (integration / "app.py").write_text("resolved without registry\n")

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/61/worktree/retry", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert (repo / "app.py").read_text() == "resolved without registry\n"
    assert not integration.exists()


def test_retry_worktree_resolution_is_idempotent_when_already_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    with patch("agent.runners.get_active_msg_ids", return_value=set()):
        res = TestClient(server.app).post("/chat/62/worktree/retry", json={
            "topic": "squid",
            "repo": str(repo),
        })

    assert res.status_code == 200
    assert res.json() == {"ok": True, "already_resolved": True}


def test_history_marks_missing_worktree_conflict_as_synced(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    tools = [{
        "name": "GitDiff",
        "repo": str(repo),
        "worktree_repo": str(worktree_mod.worktree_path(repo, "squid", str(msg_id))),
        "worktree_status": "conflict",
        "files": [{"status": "M", "path": "app.py"}],
    }]
    stats_db.update_assistant_message(msg_id, "done", "session-1", "done", context=json.dumps(tools))

    res = TestClient(server.app).get(f"/history/by-ids?ids={msg_id}")

    assert res.status_code == 200
    item = res.json()["items"][0]
    hydrated_tools = json.loads(item["context"])
    assert hydrated_tools[0]["worktree_status"] == "synced"
    assert hydrated_tools[0]["already_resolved"] is True


def test_history_backfills_unresolved_worktree_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo / "app.py").write_text("base\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    wt = worktree_mod.ensure_worktree(repo, "squid", str(msg_id))
    integration = worktree_mod.integration_worktree_path(repo, "squid", str(msg_id))
    integration.mkdir(parents=True)
    stats_db.save_worktree(
        "squid",
        str(msg_id),
        str(repo),
        str(wt),
        worktree_mod.branch_name("squid", str(msg_id)),
        integration_worktree_path=str(integration),
    )
    stats_db.mark_worktree_status("squid", str(msg_id), str(repo), "conflict")
    tools = [{
        "name": "GitDiff",
        "repo": str(repo),
        "worktree_repo": str(wt),
        "worktree_status": "conflict",
        "files": [{"status": "U", "path": "app.py"}],
    }]
    stats_db.update_assistant_message(msg_id, "done", "session-1", "done", context=json.dumps(tools))

    res = TestClient(server.app).get(f"/history/by-ids?ids={msg_id}")

    assert res.status_code == 200
    item = res.json()["items"][0]
    hydrated_tools = json.loads(item["context"])
    assert hydrated_tools[0]["worktree_status"] == "conflict"
    assert hydrated_tools[0]["integration_worktree_path"] == str(integration)


class FinishedWorker:
    def position_of(self, seq):
        return 0


def test_realtime_chat_start_preserves_persisted_error_details():
    response = JSONResponse({
        "error": "worktree sync requires attention",
        "msg_id": 202,
        "worktrees": [{"repo_root": "/repo", "worktree_path": "/worktree"}],
    }, status_code=409)
    with patch("agent.server._prepare_chat_turn", new=AsyncMock(return_value=response)):
        result = asyncio.run(server._realtime_chat_start({"message": "hello", "topic": "squid"}))

    assert result == {
        "ok": False,
        "error": "worktree sync requires attention",
        "msg_id": 202,
        "worktrees": [{"repo_root": "/repo", "worktree_path": "/worktree"}],
        "status": 409,
    }


def test_native_shell_uses_agent_cwd_without_worktree_setup(tmp_path):
    code_root = tmp_path / "project"
    code_root.mkdir()

    with patch("agent.server.get_agent", return_value={"cwd": "/tmp/agent-cwd"}), \
         patch("agent.server._resolve_agent_runtime", return_value=("codex", None, "codex", SimpleNamespace(fingerprint="f"))), \
         patch("agent.server.upsert_topic"), \
         patch("agent.server.topic_memory_squid_config", return_value={"code_roots": [str(code_root)]}), \
         patch("agent.server.read_topic_memory", return_value={
             "topic": "squid", "content": "remember this", "revision": "rev-1",
         }), \
         patch("agent.server._repo_roots_for_code_roots") as repo_roots, \
         patch("agent.server.insert_user_message", return_value=201), \
         patch("agent.server.insert_assistant_message", return_value=202):
        prepared = asyncio.run(server._prepare_chat_turn(
            message="! pwd", topic="squid", agent="codex", include_topic_memory=True,
        ))

    assert prepared["cwd"] == "/tmp/agent-cwd"
    assert prepared["source_cwd"] == "/tmp/agent-cwd"
    assert prepared["code_roots"] == []
    assert prepared["native_shell"] is True
    assert prepared["shell_topic_memory"] == "remember this"
    repo_roots.assert_not_called()


def test_override_cwd_is_the_prepared_configured_cwd():
    with patch("agent.server.get_agent", return_value={"cwd": "/tmp/agent-cwd"}), \
         patch("agent.server._resolve_agent_runtime", return_value=("codex", None, "codex", SimpleNamespace(fingerprint="f"))), \
         patch("agent.server.upsert_topic"), \
         patch("agent.server.topic_memory_squid_config", return_value={"code_roots": ["/repo"]}), \
         patch("agent.server.get_context_history", return_value=([], [])), \
         patch("agent.server.get_messages_by_ids", return_value=[]), \
         patch("agent.server.insert_user_message", return_value=201), \
         patch("agent.server.insert_assistant_message", return_value=202):
        prepared = asyncio.run(server._prepare_chat_turn(
            message="inspect", topic="squid", agent="codex", adhoc=True,
            override_cwd="/tmp/integration-worktree",
        ))

    assert prepared["cwd"] == "/tmp/integration-worktree"
    assert prepared["configured_cwd"] == "/tmp/integration-worktree"


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


def test_sse_chunk_preserves_leading_spaces_after_sse_parsing():
    encoded = server.sse_chunk(" hello\n  world")
    assert encoded == "data:  hello\ndata:   world\n\n"
    parsed = []
    for line in encoded.splitlines():
        if line.startswith("data:"):
            field = line[5:]
            parsed.append(field[1:] if field.startswith(" ") else field)
    assert "\n".join(parsed) == " hello\n  world"


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


def test_native_shell_drain_timeout_starts_after_processing_event():
    async def run():
        out_q = asyncio.Queue()
        time_values = iter([100.0, 100.0, 105.0])
        wait_timeouts = []
        wait_calls = 0

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            wait_timeouts.append(timeout)
            if wait_calls == 1:
                awaitable.close()
                raise asyncio.TimeoutError()
            if wait_calls == 2:
                awaitable.close()
                return {"_processing": {"topic": "squid"}}
            if wait_calls == 3:
                awaitable.close()
                return "done"
            awaitable.close()
            return None

        with patch("agent.server.asyncio.wait_for", fake_wait_for), \
             patch("agent.server.asyncio.get_event_loop") as get_loop, \
             patch("agent.server.update_assistant_message") as update_message:
            get_loop.return_value.time.side_effect = lambda: next(time_values)
            await server._drain_to_completion(
                out_q,
                123,
                "",
                "",
                None,
                drain_timeout=10,
                native_shell=True,
            )
            return wait_timeouts, update_message.call_args

    wait_timeouts, update_call = asyncio.run(run())
    assert wait_timeouts == [30.0, 30.0, 10.0, 5.0]
    assert update_call.args[1] == "done"
    assert update_call.args[3] == "done"


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
            "source": "workflow",
        })

    assert res.status_code == 200
    assert res.headers["X-Squid-Msg-Id"] == "202"
    assert insert_user_message.call_args.kwargs["source"] == "workflow"


def test_blocked_worktree_turn_persists_recovery_context(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    blocker = {
        "repo_root": str(repo),
        "worktree_path": str(tmp_path / "turn"),
        "integration_worktree_path": str(tmp_path / "integration"),
        "status": "conflict",
        "msg_id": "7282",
        "conflicts": ["ui/app.js"],
    }

    async def fake_repo_roots(_code_roots):
        return [repo]

    async def fake_settle(_topic, _repo_roots):
        return [blocker]

    with patch("agent.server.get_agent", return_value={"cwd": str(repo)}), \
         patch("agent.server._resolve_agent_runtime", return_value=("codex", None, "codex", SimpleNamespace(fingerprint="f"))), \
         patch("agent.server.get_topic_session", return_value=None), \
         patch("agent.server.topic_memory_squid_config", return_value={"code_roots": [str(repo)]}), \
         patch("agent.server._repo_roots_for_code_roots", fake_repo_roots), \
         patch("agent.worktree.settle_worktrees_before_turn", fake_settle):
        res = asyncio.run(server._prepare_chat_turn(
            message="next",
            topic="squid",
            agent="codex",
        ))

    assert res.status_code == 409
    body = json.loads(res.body)
    assert body["worktrees"] == [blocker]
    saved = stats_db.get_message(body["msg_id"])
    tools = json.loads(saved["context"])
    assert tools[0]["name"] == "GitDiff"
    assert tools[0]["worktree_blocker"] is True
    assert tools[0]["worktree_status"] == "conflict"
    assert tools[0]["worktree_conflicts"] == ["ui/app.js"]


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
    assert body["updates"]["install_on_restart"] == server.UPDATES_INSTALL_ON_RESTART
    assert isinstance(body["updates"]["can_install_on_restart"], bool)
    claudecode = next(h for h in body["harnesses"] if h["id"] == "claudecode")
    assert claudecode["default_provider"] == "anthropic"
    assert body["providers"]["anthropic"]["gauge"]["type"] == "claude"
    assert body["providers"]["anthropic"]["color"].startswith("#")


def test_pipx_upgrade_rejects_source_checkout_install():
    with patch("agent.server.SQUID_VERSION", "0+local"), \
         patch("agent.server.subprocess.run") as run:
        ok, message = server._pipx_upgrade_agentsquid()

    assert ok is False
    assert "installed agentsquid packages" in message
    run.assert_not_called()


def test_pipx_upgrade_rejects_non_pipx_runtime():
    with patch("agent.server.SQUID_VERSION", "0.1.0"), \
         patch("agent.server._is_running_from_pipx_agentsquid", return_value=False), \
         patch("agent.server.subprocess.run") as run:
        ok, message = server._pipx_upgrade_agentsquid()

    assert ok is False
    assert "pipx-installed agentsquid app" in message
    run.assert_not_called()


def test_pipx_upgrade_runs_agentsquid_upgrade():
    completed = subprocess.CompletedProcess(["pipx", "upgrade", "agentsquid"], 0, "upgraded\n", "")
    with patch("agent.server.SQUID_VERSION", "0.1.0"), \
         patch("agent.server._is_running_from_pipx_agentsquid", return_value=True), \
         patch("agent.server.shutil.which", return_value="/usr/local/bin/pipx"), \
         patch("agent.server.subprocess.run", return_value=completed) as run:
        ok, message = server._pipx_upgrade_agentsquid()

    assert ok is True
    assert "upgraded" in message
    run.assert_called_once_with(
        ["/usr/local/bin/pipx", "upgrade", "agentsquid"],
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_lifecycle_start_backgrounds_server(tmp_path):
    pid_file = tmp_path / "agentsquid.pid"
    boot_log = tmp_path / "boot.log"
    proc = SimpleNamespace(pid=1234, poll=lambda: None)
    health_checks = [False, True]

    with patch("agent.server._lifecycle_paths", return_value=(pid_file, boot_log)), \
         patch("agent.server._read_lifecycle_pid", return_value=None), \
         patch("agent.server._health_ok", side_effect=lambda host, port: health_checks.pop(0)), \
         patch("agent.server.subprocess.Popen", return_value=proc) as popen, \
         patch("agent.server.time.sleep"):
        assert server._lifecycle_start("127.0.0.1", 8000) == 0

    assert pid_file.read_text() == "1234\n"
    args, kwargs = popen.call_args
    assert args[0] == [sys.executable, "-m", "agent.server", "--fg"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_lifecycle_paths_use_user_squid_home(tmp_path):
    with patch("agent.server.Path.home", return_value=tmp_path):
        pid_file, boot_log = server._lifecycle_paths()

    assert pid_file == tmp_path / ".squid" / "agentsquid.pid"
    assert boot_log == tmp_path / ".squid" / "logs" / "boot.log"
    assert boot_log.parent.is_dir()


def test_lifecycle_stop_uses_pid_file(tmp_path):
    pid_file = tmp_path / "agentsquid.pid"
    pid_file.write_text("1234\n")
    boot_log = tmp_path / "boot.log"

    with patch("agent.server._lifecycle_paths", return_value=(pid_file, boot_log)), \
         patch("agent.server._pid_running", side_effect=[True, False]), \
         patch("agent.server._pid_looks_like_agentsquid", return_value=True), \
         patch("agent.server.os.kill") as kill:
        assert server._lifecycle_stop("127.0.0.1", 8000) == 0

    kill.assert_called_once_with(1234, 15)
    assert not pid_file.exists()


def test_lifecycle_stop_without_pid_uses_http_shutdown(tmp_path):
    pid_file = tmp_path / "agentsquid.pid"
    boot_log = tmp_path / "boot.log"

    with patch("agent.server._lifecycle_paths", return_value=(pid_file, boot_log)), \
         patch("agent.server._health_ok", return_value=True) as health_ok, \
         patch("agent.server._request_http_shutdown", return_value=0) as shutdown:
        assert server._lifecycle_stop("127.0.0.1", 8000) == 0

    health_ok.assert_called_once_with("127.0.0.1", 8000)
    shutdown.assert_called_once_with("127.0.0.1", 8000)


def test_lifecycle_restart_stops_then_starts():
    with patch("agent.server._request_http_restart", return_value=None) as http_restart, \
         patch("agent.server._lifecycle_stop", return_value=0) as stop, \
         patch("agent.server._lifecycle_start", return_value=0) as start:
        assert server._lifecycle_restart("127.0.0.1", 8000) == 0

    http_restart.assert_called_once_with("127.0.0.1", 8000)
    stop.assert_called_once_with("127.0.0.1", 8000)
    start.assert_called_once_with("127.0.0.1", 8000)


def test_lifecycle_restart_prefers_running_server_restart():
    with patch("agent.server._request_http_restart", return_value=0) as http_restart, \
         patch("agent.server._lifecycle_stop") as stop, \
         patch("agent.server._lifecycle_start") as start:
        assert server._lifecycle_restart("127.0.0.1", 8000) == 0

    http_restart.assert_called_once_with("127.0.0.1", 8000)
    stop.assert_not_called()
    start.assert_not_called()


def test_request_http_restart_posts_cmd_and_waits_for_new_health():
    class Response:
        def __init__(self, payload):
            self.status = 200
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            return Response({"boot_time": "old"})
        if len(calls) == 2:
            return Response({"ok": True})
        return Response({"boot_time": "new"})

    with patch("agent.server.urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("agent.server.time.sleep"):
        assert server._request_http_restart("127.0.0.1", 8000) == 0

    restart_request = calls[1]
    assert restart_request.full_url == "http://127.0.0.1:8000/cmd"
    assert json.loads(restart_request.data.decode("utf-8")) == {
        "command": "restart",
        "topic": "default",
        "upgrade": False,
    }


def test_request_http_shutdown_posts_cmd_and_waits_for_health_down():
    class Response:
        def __init__(self, payload):
            self.status = 200
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    calls = []
    health_checks = 0

    def fake_urlopen(request, timeout):
        nonlocal health_checks
        calls.append(request)
        if isinstance(request, str):
            health_checks += 1
            if health_checks == 1:
                return Response({"status": "ok"})
            raise OSError("down")
        return Response({"ok": True})

    with patch("agent.server.urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("agent.server.time.sleep"):
        assert server._request_http_shutdown("127.0.0.1", 8000) == 0

    shutdown_request = calls[1]
    assert shutdown_request.full_url == "http://127.0.0.1:8000/cmd"
    assert json.loads(shutdown_request.data.decode("utf-8")) == {
        "command": "shutdown",
        "topic": "default",
    }


def test_main_dispatches_start_command():
    with patch.object(sys, "argv", ["agentsquid", "start"]), \
         patch("agent.server._lifecycle_start", return_value=0) as start:
        try:
            server.main()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("main should exit after lifecycle command")

    start.assert_called_once_with(server._cfg["server"]["host"], server._cfg["server"]["port"])


def test_public_agent_config_includes_provider_color(monkeypatch):
    # Provider labels are user-configurable (squid.yaml `providers:` section
    # replaces the shipped defaults wholesale — see providers.py's
    # _configured_providers), so pin the "anthropic" entry here rather than
    # asserting on whatever the machine running this test happens to have
    # configured.
    monkeypatch.setitem(
        providers_mod.PROVIDERS, "anthropic",
        Provider(id="anthropic", label="Claude", color="#AE5332", auth_type="subscription"),
    )
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


def test_ollama_public_models_deduplicate_implicit_latest(monkeypatch):
    monkeypatch.setitem(providers_mod._PROVIDER_BINARY_PATH, "ollama", "/usr/bin/ollama")
    monkeypatch.setattr(
        providers_mod,
        "_installed_ollama_models",
        lambda: {"qwen3.5-optimized", "qwen3.5-optimized:latest", "qwen3:8b"},
    )
    provider = Provider(
        id="ollama", auth_type="none", models=("qwen3.5-optimized",),
    )

    result = provider.public_dict()

    assert result["models"] == ["qwen3.5-optimized", "qwen3:8b"]
    assert "qwen3.5-optimized:latest" in result["pulled_models"]


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


def test_provider_claude_quota_skips_fetch_without_credentials(monkeypatch):
    async def fail_quota_claude():
        raise AssertionError("quota_claude should not be called")

    monkeypatch.setattr(creds, "get_org_id", lambda: None)
    monkeypatch.setattr(creds, "get_session_key", lambda: None)
    monkeypatch.setattr(server, "quota_claude", fail_quota_claude)

    response = asyncio.run(
        server._quota_snapshot_for_provider(
            Provider(id="anthropic", label="Claude", gauge=Gauge(type="claude")),
            "anthropic",
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "unauthenticated",
        "text": "auth",
        "title": "Claude credentials not configured",
        "used_percent": None,
        "reset_at": None,
    }


def test_provider_subscription_quota_skips_fetch_without_credentials(monkeypatch):
    async def fail_quota_codex():
        raise AssertionError("quota_codex should not be called")

    async def fail_quota_cursor():
        raise AssertionError("quota_cursor should not be called")

    monkeypatch.setattr(creds, "get_codex_cli_auth", lambda: {})
    monkeypatch.setattr(creds, "get_codex_token", lambda: None)
    monkeypatch.setattr(creds, "get_cursor_token", lambda: None)
    monkeypatch.setattr(server, "quota_codex", fail_quota_codex)
    monkeypatch.setattr(server, "quota_cursor", fail_quota_cursor)

    codex = asyncio.run(
        server._quota_snapshot_for_provider(
            Provider(id="openai", label="GPT", gauge=Gauge(type="codex")),
            "openai",
        )
    )
    cursor = asyncio.run(
        server._quota_snapshot_for_provider(
            Provider(id="cursor", label="Cursor", gauge=Gauge(type="cursor")),
            "cursor",
        )
    )

    assert codex.status_code == 200
    assert json.loads(codex.body)["status"] == "unauthenticated"
    assert json.loads(codex.body)["title"] == "GPT credentials not configured"
    assert cursor.status_code == 200
    assert json.loads(cursor.body)["status"] == "unauthenticated"
    assert json.loads(cursor.body)["title"] == "Cursor credentials not configured"


def test_provider_balance_quota_skips_fetch_without_api_key():
    response = asyncio.run(
        server._quota_snapshot_for_provider(
            Provider(
                id="deepseek",
                label="DeepSeek",
                auth_type="api_key",
                api_key={"env": "SQUID_TEST_MISSING_DEEPSEEK_KEY"},
                gauge=Gauge(type="deepseek"),
            ),
            "deepseek",
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "unauthenticated",
        "text": "auth",
        "title": "DeepSeek credentials not configured",
        "used_percent": None,
        "reset_at": None,
    }


def test_codex_gauge_auth_detects_cli_auth(monkeypatch):
    monkeypatch.setattr(creds, "get_codex_cli_auth", lambda: {"access_token": "token"})
    monkeypatch.setattr(creds, "get_codex_token", lambda: None)

    assert server._gauge_authed("codex", Provider(id="openai")) is True


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
        providers_mod.reload_providers()
        server._LOCALFILE_ROOTS[:] = original_roots


def test_yaml_provider_update_refreshes_pi_models_config(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = state_dir / "squid.yaml"
    original_cfg = dict(server._cfg)
    original_providers = dict(providers_mod.PROVIDERS)
    content = _config_yaml(tmp_path) + '''
  baseten:
    label: Baseten
    color: "#19E76E"
    base_url: "https://inference.baseten.co"
    supported_apis: [/v1/chat/completions]
    auth: {type: api_key, api_key: test-key}
'''
    config_path.write_text(content)
    pi_models = tmp_path / "models.json"
    monkeypatch.setattr("agent.resolve.PI_MODELS_FILE", str(pi_models))
    monkeypatch.setattr(server, "list_agents", lambda: [{
        "name": "pibt", "harness": "pi", "provider": "baseten",
        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
    }])

    try:
        with patch("agent.config._USER_CONFIG", config_path), \
             patch.object(server, "_USER_CONFIG", config_path):
            loaded = TestClient(server.app).get("/config/yaml").json()
            updated = loaded["content"].replace(
                "https://inference.baseten.co\"",
                "https://inference.baseten.co/v1\"",
            )
            saved = TestClient(server.app).put("/config/yaml", json={
                "content": updated,
                "revision": loaded["revision"],
            })

        assert saved.status_code == 200
        assert providers_mod.PROVIDERS["baseten"].base_url == "https://inference.baseten.co/v1"
        models = json.loads(pi_models.read_text())
        assert models["providers"]["baseten"]["baseUrl"] == "https://inference.baseten.co/v1"
    finally:
        server._cfg.clear()
        server._cfg.update(original_cfg)
        providers_mod.PROVIDERS.clear()
        providers_mod.PROVIDERS.update(original_providers)


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


def test_localfile_upload_suffixes_duplicate_names(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.txt").write_text("existing")
    (root / "report 1.txt").write_text("existing 1")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        res = client.post(
            "/localfile/upload",
            params={"parent": str(root), "name": "report.txt"},
            content=b"uploaded",
        )
        assert res.status_code == 200
        assert res.json()["path"] == str((root / "report 2.txt").resolve())
        assert (root / "report 2.txt").read_bytes() == b"uploaded"
    finally:
        server._LOCALFILE_ROOTS[:] = original_roots


def test_localfile_check_paths_reports_missing_files(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    existing = root / "notes.txt"
    missing = root / "missing.txt"
    existing.write_text("notes")
    original_roots = list(server._LOCALFILE_ROOTS)
    client = TestClient(server.app)

    try:
        server._LOCALFILE_ROOTS[:] = [root.resolve()]
        res = client.post("/localfile/check-paths", json={"paths": [str(existing), str(missing)]})
        assert res.status_code == 200
        assert res.json()["paths"] == [
            {"path": str(existing), "resolved_path": str(existing.resolve()), "exists": True, "is_file": True},
            {"path": str(missing), "resolved_path": str(missing.resolve()), "exists": False, "is_file": False},
        ]
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


def _make_opencode_db(path, session_id, other_session_id="ses_other"):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    conn.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?)",
        ("sm1", session_id, "agent-switched", 1, 100, '{"agent":"build"}'),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("msg1", session_id, 200, '{"role":"user"}'),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        ("prt1", "msg1", session_id, 300, '{"type":"text","text":"hello"}'),
    )
    # A row for a different session must never leak into this session's transcript.
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("msg-other", other_session_id, 50, '{"role":"user"}'),
    )
    conn.commit()
    conn.close()


def test_opencode_session_transcript_reconstructs_rows_in_time_order(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "harness_home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    _make_opencode_db(db_dir / "opencode.db", "ses_target")

    entries = server._opencode_session_transcript_rows(home, "ses_target")
    assert entries is not None
    assert [e["kind"] for e in entries] == ["session_message", "message", "part"]
    assert [e["time_created"] for e in entries] == sorted(e["time_created"] for e in entries)
    assert all(e["data"] for e in entries)
    # The other session's row must not appear.
    assert all(e["id"] != "msg-other" for e in entries)


def test_opencode_session_transcript_returns_none_for_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert server._opencode_session_transcript_rows(tmp_path / "no_such_home", "ses_x") is None


def test_opencode_session_transcript_returns_none_for_unknown_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "harness_home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    _make_opencode_db(db_dir / "opencode.db", "ses_target")

    assert server._opencode_session_transcript_rows(home, "ses_does_not_exist") is None


def test_find_session_log_has_no_opencode_branch(tmp_path, monkeypatch):
    # opencode has no per-session file -- _find_session_log stays file-only;
    # /session-log routes opencode through _opencode_session_transcript_rows
    # instead (see the endpoint test below), never through this function.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    _make_opencode_db(db_dir / "opencode.db", "ses_target")

    assert server._find_session_log("opencode", "ses_target", "/some/cwd", "") is None


def test_session_log_endpoint_returns_entries_not_path_for_opencode(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    _make_opencode_db(db_dir / "opencode.db", "ses_target")
    stats_db.upsert_agent("oc-viewer-test", "opencode", "opencode", "opencode/some-model", None)

    res = TestClient(server.app).get(
        "/session-log",
        params={"agent": "oc-viewer-test", "session_id": "ses_target", "cwd": "/some/cwd"},
    )
    body = res.json()
    assert body["path"] is None
    assert body["source"] == "opencode-sqlite"
    assert body["entries"] is not None
    assert {e["kind"] for e in body["entries"]} == {"session_message", "message", "part"}
    # No file should have been written anywhere for this -- entries are
    # served straight from the DB read, never persisted to disk.
    assert not (tmp_path / ".squid" / "artifacts").exists()


def test_session_log_endpoint_includes_squid_turn_boundaries(tmp_path, monkeypatch):
    # The raw transcript (opencode's SQLite rows here, but equally a jsonl
    # file for other harnesses) has no notion of squid's own turn grouping --
    # /session-log must also return squid's per-turn timestamps for this
    # session_id so the viewer can mark where one turn ends and the next begins.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    _make_opencode_db(db_dir / "opencode.db", "ses_target")
    stats_db.upsert_agent("oc-turns-test", "opencode", "opencode", "opencode/some-model", None)

    u1 = stats_db.insert_user_message("squid", "oc-turns-test", "first")
    m1 = stats_db.insert_assistant_message("squid", "oc-turns-test", u1)
    stats_db.update_assistant_message(m1, "reply one", "ses_target", "done")
    u2 = stats_db.insert_user_message("squid", "oc-turns-test", "second")
    m2 = stats_db.insert_assistant_message("squid", "oc-turns-test", u2)
    stats_db.update_assistant_message(m2, "reply two", "ses_target", "done")

    res = TestClient(server.app).get(
        "/session-log",
        params={"agent": "oc-turns-test", "session_id": "ses_target", "cwd": "/some/cwd"},
    )
    body = res.json()
    assert [t["msg_id"] for t in body["turns"]] == [m1, m2]
    assert [t["turn_index"] for t in body["turns"]] == [1, 2]
