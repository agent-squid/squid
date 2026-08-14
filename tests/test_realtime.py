import json
import sqlite3
import threading

from fastapi.testclient import TestClient

from agent import server, stats_db


CLIENT_ID = "testclient0000000000000001"


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()


def test_realtime_log_has_global_cursor_and_local_run_sequence(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.insert_run_event(msg_id, 0, "text", "one")
    stats_db.insert_run_event(msg_id, 1, "text", "two")

    events = stats_db.get_realtime_events(0, [{"topic": "squid", "agent": "codex"}])
    assert [event["event_id"] for event in events] == sorted(event["event_id"] for event in events)
    text_events = [event for event in events if event["event_type"] == "chat.text"]
    assert [event["run_seq"] for event in text_events] == [0, 1]
    assert [event["payload"] for event in text_events] == [{"text": "one"}, {"text": "two"}]


def test_realtime_snapshot_is_bounded_but_keeps_old_pending(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    old_user = stats_db.insert_user_message("squid", "codex", "old")
    old_pending = stats_db.insert_assistant_message("squid", "codex", old_user)
    stats_db.insert_run_event(old_pending, 0, "text", "partial")
    for index in range(25):
        stats_db.insert_user_message("squid", "codex", f"new {index}")

    snapshot = stats_db.get_realtime_snapshot([{"topic": "squid", "agent": "codex"}], 20)
    messages = snapshot["conversations"][0]["messages"]
    assert old_pending in {message["id"] for message in messages}
    pending = next(message for message in messages if message["id"] == old_pending)
    assert pending["content"] == "partial"
    assert pending["run_seq"] == 0
    assert len(messages) == 21


def test_global_lifecycle_scope_discovers_turns_across_topics(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    first_user = stats_db.insert_user_message("desktop", "codex", "one")
    first_msg = stats_db.insert_assistant_message("desktop", "codex", first_user)
    second_user = stats_db.insert_user_message("mobile", "claude", "two")
    second_msg = stats_db.insert_assistant_message("mobile", "claude", second_user)

    scope = [{"lifecycle": "global"}]
    events = stats_db.get_realtime_events(0, scope)
    assert {event["topic"] for event in events} == {"desktop", "mobile"}
    snapshot = stats_db.get_realtime_snapshot(scope, 20)
    message_ids = {message["id"] for message in snapshot["conversations"][0]["messages"]}
    assert {first_msg, second_msg} <= message_ids


def test_global_lifecycle_snapshot_bounds_pending_rows(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    pending_ids = []
    for index in range(5):
        user_id = stats_db.insert_user_message("topic", "codex", f"turn {index}")
        pending_ids.append(stats_db.insert_assistant_message("topic", "codex", user_id))

    snapshot = stats_db.get_realtime_snapshot([{"lifecycle": "global"}], message_limit=2)
    messages = snapshot["conversations"][0]["messages"]
    returned_pending = [message["id"] for message in messages if message["status"] == "pending"]
    assert returned_pending == pending_ids[-2:]


def test_websocket_subscribe_snapshot_live_event_and_idempotent_cancel(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    monkeypatch.setattr(server, "kill_proc_by_msg_id", lambda _msg_id: 0)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {"client_id": CLIENT_ID, "scopes": [{"topic": "squid", "agent": "codex"}]},
        })
        assert ws.receive_json()["type"] == "subscribed"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert any(message["id"] == msg_id for message in snapshot["payload"]["conversations"][0]["messages"])

        stats_db.insert_run_event(msg_id, 0, "text", "live")
        event = ws.receive_json()
        assert event["type"] == "chat.text"
        assert event["payload"] == {"text": "live"}

        cancel = {"v": 1, "type": "chat.cancel", "request_id": "cancel-1", "payload": {"msg_id": msg_id}}
        ws.send_json(cancel)
        first = ws.receive_json()
        while first["type"] != "command.result":
            first = ws.receive_json()
        ws.send_json(cancel)
        second = ws.receive_json()
        while second["type"] != "command.result":
            second = ws.receive_json()
        assert first["payload"] == second["payload"]
        assert first["payload"]["cancelled"] is True


def test_websocket_rejects_protocol_version_skew(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({"v": 99, "type": "ping", "payload": {}})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "unsupported_version"


def test_realtime_listener_runs_after_event_commit_from_worker_thread(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    observed = []

    def listener(event_id):
        with sqlite3.connect(tmp_path / "squid.db") as conn:
            observed.append(conn.execute(
                "SELECT event_type FROM realtime_events WHERE event_id=?", (event_id,),
            ).fetchone()[0])

    stats_db.set_realtime_commit_listener(listener)
    try:
        thread = threading.Thread(target=stats_db.insert_realtime_event, args=(
            "message.changed", "squid", "codex", {"id": 1},
        ))
        thread.start()
        thread.join()
    finally:
        stats_db.set_realtime_commit_listener(None)
    assert observed == ["message.changed"]


def test_pruned_cursor_rolls_over_to_snapshot(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for index in range(5):
        stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": index})
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("DELETE FROM realtime_events WHERE event_id < 4")

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "cursor": 1,
                "scopes": [{"topic": "squid", "agent": "codex"}],
            },
        })
        assert ws.receive_json()["type"] == "subscribed"
        assert ws.receive_json()["type"] == "snapshot"


def test_mutation_requires_client_identity(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "chat.cancel", "request_id": "cancel-no-client",
            "payload": {"msg_id": 1},
        })
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "client_identity_required"
