import asyncio
import base64
import json
import os
import sqlite3
import threading

from fastapi.testclient import TestClient

from agent import auth_sessions, server, stats_db


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


def test_process_and_queue_changes_are_replayable_global_state(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    processes = [{"pid": 12, "topic": "squid", "agent": "codex", "state": "running"}]
    queue = [{"msg_id": 34, "topic": "other", "agent": "claude", "position": 1}]

    server._publish_process_changed(processes)
    server._publish_queue_changed(queue)

    events = stats_db.get_realtime_events(0, [{"lifecycle": "global"}])
    assert [(event["event_type"], event["payload"]) for event in events] == [
        ("process.changed", {"processes": processes}),
        ("queue.changed", {"queue": queue}),
    ]
    assert stats_db.get_realtime_events(0, [{"topic": "squid", "agent": "codex"}]) == []


def test_process_and_queue_publication_failure_does_not_escape(monkeypatch, caplog):
    def fail_publish(*_args, **_kwargs):
        raise sqlite3.OperationalError("database busy")

    monkeypatch.setattr(server, "insert_realtime_event", fail_publish)
    server._publish_process_changed([{"pid": 12}])
    server._publish_queue_changed([{"msg_id": 34}])

    assert "Failed to publish realtime process state" in caplog.text
    assert "Failed to publish realtime queue state" in caplog.text


def test_server_startup_publishes_authoritative_process_and_queue_state(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    with TestClient(server.app):
        pass

    events = stats_db.get_realtime_events(0, [{"lifecycle": "global"}])
    lifecycle = [event for event in events if event["event_type"] in {"process.changed", "queue.changed"}]
    assert [event["event_type"] for event in lifecycle[:2]] == ["process.changed", "queue.changed"]
    assert lifecycle[0]["payload"] == {"processes": []}
    assert lifecycle[1]["payload"] == {"queue": []}


def test_realtime_notifier_stop_wakes_existing_waiter():
    async def run():
        notifier = server._RealtimeNotifier()
        notifier.start(asyncio.get_running_loop())
        waiter = asyncio.create_task(notifier.wait(notifier.generation))
        await asyncio.sleep(0)
        notifier.stop()
        return await asyncio.wait_for(waiter, timeout=0.5)

    assert asyncio.run(run()) == 1


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


def test_websocket_rejects_unauthorized_scope(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "scopes": [{"lifecycle": "other"}],
            },
        })
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "unauthorized_scope"
        ws.send_json({
            "v": 1, "type": "chat.cancel", "request_id": "rejected-scope",
            "payload": {"msg_id": 1},
        })
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "client_identity_required"


def test_event_racing_snapshot_is_delivered_immediately(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    original_snapshot = server._realtime_snapshot
    raced_event_id = None

    async def snapshot_then_publish(scopes):
        nonlocal raced_event_id
        snapshot = await original_snapshot(scopes)
        raced_event_id = stats_db.insert_realtime_event(
            "message.changed", "squid", "codex", {"id": "raced"},
        )
        return snapshot

    monkeypatch.setattr(server, "_realtime_snapshot", snapshot_then_publish)
    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "scopes": [{"topic": "squid", "agent": "codex"}],
            },
        })
        assert ws.receive_json()["type"] == "subscribed"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["event_id"] < raced_event_id
        raced = ws.receive_json()
        assert raced["type"] == "message.changed"
        assert raced["event_id"] == raced_event_id


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


def test_linked_flow_step_publishes_after_linkage_commit(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message(
        "squid", "codex", "origin", flow_run_id="run-1", flow_step_id="origin",
    )
    msg_id = stats_db.insert_assistant_message(
        "squid", "codex", user_id, flow_run_id="run-1", flow_step_id="origin",
    )
    stats_db.create_flow_run("run-1", "#squid@codex>@claude", user_id, [{
        "step_id": "origin", "topic": "squid", "agent": "codex",
    }], execution_mode="durable")
    observed = []

    def listener(event_id):
        with sqlite3.connect(tmp_path / "squid.db") as conn:
            conn.row_factory = sqlite3.Row
            event = conn.execute(
                "SELECT event_type FROM realtime_events WHERE event_id=?", (event_id,),
            ).fetchone()
            step = conn.execute(
                "SELECT assistant_msg_id FROM flow_steps WHERE flow_run_id='run-1' AND step_id='origin'",
            ).fetchone()
            observed.append((event["event_type"], step["assistant_msg_id"]))

    stats_db.set_realtime_commit_listener(listener)
    try:
        assert stats_db.link_flow_step_messages("run-1", "origin", user_id, msg_id)
    finally:
        stats_db.set_realtime_commit_listener(None)

    assert observed == [("flow.step.created", msg_id)]
    events = stats_db.get_realtime_events(0, [{"lifecycle": "global"}])
    created = next(event for event in events if event["event_type"] == "flow.step.created")
    assert created["msg_id"] == msg_id
    assert created["payload"] == {
        "flow_run_id": "run-1",
        "step_id": "origin",
        "user_msg_id": user_id,
        "assistant_msg_id": msg_id,
        "route": "#squid@codex>@claude",
        "status": "pending",
    }


def test_realtime_snapshot_includes_linked_flow_step_state(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message(
        "squid", "codex", "origin", flow_run_id="run-1", flow_step_id="origin",
    )
    msg_id = stats_db.insert_assistant_message(
        "squid", "codex", user_id, flow_run_id="run-1", flow_step_id="origin",
    )
    stats_db.create_flow_run("run-1", "#squid@codex>@claude", user_id, [
        {"step_id": "origin", "topic": "squid", "agent": "codex"},
        {"step_id": "target", "topic": "squid", "agent": "claude", "dependencies": ["origin"]},
    ], execution_mode="durable")
    stats_db.link_flow_step_messages("run-1", "origin", user_id, msg_id)

    snapshot = stats_db.get_realtime_snapshot([{"lifecycle": "global"}], 20)

    assert [run["flow_run_id"] for run in snapshot["flow_runs"]] == ["run-1"]
    assert [step["step_id"] for step in snapshot["flow_steps"]] == ["origin", "target"]
    assert snapshot["flow_steps"][0]["assistant_msg_id"] == msg_id
    assert snapshot["flow_steps"][1]["assistant_msg_id"] is None


def test_scoped_realtime_snapshot_includes_only_authorized_active_flow_steps(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    stats_db.create_flow_run("run-1", "#squid@codex>@claude", None, [
        {"step_id": "origin", "topic": "squid", "agent": "codex"},
        {"step_id": "target", "topic": "private", "agent": "claude", "dependencies": ["origin"]},
    ], execution_mode="durable")

    snapshot = stats_db.get_realtime_snapshot([{"topic": "squid", "agent": "codex"}], 20)

    assert [run["flow_run_id"] for run in snapshot["flow_runs"]] == ["run-1"]
    assert [(step["topic"], step["agent"]) for step in snapshot["flow_steps"]] == [("squid", "codex")]


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


def test_internal_realtime_log_gap_rolls_over_to_snapshot(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for index in range(4):
        stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": index})
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("DELETE FROM realtime_events WHERE event_id=3")

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


def test_replay_rolls_over_when_serialized_events_exceed_byte_limit(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_REPLAY_BYTE_LIMIT", 64)
    stats_db.insert_realtime_event(
        "message.changed", "squid", "codex", {"content": "x" * 100},
    )

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "cursor": 0,
                "scopes": [{"topic": "squid", "agent": "codex"}],
            },
        })
        assert ws.receive_json()["type"] == "subscribed"
        assert ws.receive_json()["type"] == "snapshot"


def test_replay_rolls_over_for_old_or_incompatible_events(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    event_id = stats_db.insert_realtime_event("future.event", "squid", "codex", {})
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE realtime_events SET created_at=datetime('now', '-2 days') WHERE event_id=?",
            (event_id,),
        )
    window, events = stats_db.get_realtime_replay(
        0, [{"topic": "squid", "agent": "codex"}],
    )
    assert server._realtime_replay_rollover_reason(0, window, events) == "event_age"

    monkeypatch.setattr(server, "_REALTIME_REPLAY_MAX_AGE_SECONDS", 3 * 24 * 60 * 60)
    assert server._realtime_replay_rollover_reason(0, window, events) == "incompatible_event"


def test_future_realtime_cursor_rolls_over():
    assert server._realtime_replay_rollover_reason(
        5, {
            "current": 4, "global_event_count": 0,
            "scoped_event_count": 0, "oldest_created_at": None,
        }, [],
    ) == "future_cursor"


def test_future_cursor_snapshot_explicitly_resets_client_cursor(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "cursor": 50,
                "scopes": [{"lifecycle": "global"}],
            },
        })
        assert ws.receive_json()["type"] == "subscribed"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["event_id"] < 50
        assert snapshot["payload"]["cursor_reset"] is True


def test_unrelated_events_do_not_force_scoped_replay_rollover(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    old_id = stats_db.insert_realtime_event(
        "message.changed", "other", "codex", {"id": "old"},
    )
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE realtime_events SET created_at=datetime('now', '-2 days') WHERE event_id=?",
            (old_id,),
        )
    for index in range(server._REALTIME_REPLAY_LIMIT):
        stats_db.insert_realtime_event("message.changed", "other", "codex", {"id": index})
    expected_id = stats_db.insert_realtime_event(
        "message.changed", "squid", "codex", {"id": "expected"},
    )

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {
                "client_id": CLIENT_ID,
                "cursor": 0,
                "scopes": [{"topic": "squid", "agent": "codex"}],
            },
        })
        assert ws.receive_json()["type"] == "subscribed"
        replay = ws.receive_json()
        assert replay["type"] == "message.changed"
        assert replay["event_id"] == expected_id


def test_realtime_replay_is_bounded_by_its_atomic_watermark(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    first_id = stats_db.insert_realtime_event(
        "message.changed", "squid", "codex", {"id": 1},
    )
    window, events = stats_db.get_realtime_replay(
        0, [{"topic": "squid", "agent": "codex"}],
    )
    assert window["current"] == first_id
    assert [event["event_id"] for event in events] == [first_id]
    assert window["global_event_count"] == window["scoped_event_count"] == 1


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


def _subscribe_auth(ws):
    """Subscribe a scope to establish principal; drain hello/subscribed/snapshot."""
    assert ws.receive_json()["type"] == "hello"
    ws.send_json({
        "v": 1, "type": "subscribe",
        "payload": {"client_id": CLIENT_ID, "scopes": [{"topic": "squid", "agent": "codex"}]},
    })
    assert ws.receive_json()["type"] == "subscribed"
    assert ws.receive_json()["type"] == "snapshot"


def _receive_command_result(ws):
    frame = ws.receive_json()
    while frame["type"] != "command.result":
        frame = ws.receive_json()
    return frame["payload"]


def _fake_cancel(counter):
    async def cancel(session_id):
        counter["n"] += 1
        return True
    return cancel


def test_auth_start_streams_live_output_and_done(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # Real create_session with a pipe standing in for the PTY master fd, so the
    # reader/broadcast/pump path is exercised without forking a login CLI.
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(auth_sessions, "_spawn_pty", lambda argv, env, cols, rows: (99999, read_fd))
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    async def fake_finalize(session):
        session.mark_exited(0)
    monkeypatch.setattr(auth_sessions, "_finalize", fake_finalize)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "auth-start-1",
            "payload": {"harness": "claudecode", "mode": "login", "cols": 80, "rows": 24},
        })
        result = _receive_command_result(ws)
        assert result["ok"] is True
        assert result["session_id"]

        os.write(write_fd, b"live")
        frame = ws.receive_json()
        assert frame["type"] == "auth.output"
        assert frame["payload"]["session_id"] == result["session_id"]
        assert base64.b64decode(frame["payload"]["data"]) == b"live"

        os.close(write_fd)
        frame = ws.receive_json()
        assert frame["type"] == "auth.done"
        assert frame["payload"]["session_id"] == result["session_id"]
        assert frame["payload"]["returncode"] == 0


def test_auth_done_reports_failure_when_session_already_reaped(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(auth_sessions, "_spawn_pty", lambda argv, env, cols, rows: (99999, read_fd))
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    async def reap_then_finalize(session):
        # cancel_session (idle reaper / server-side cancel) pops the session from
        # _sessions synchronously right after _closed fires, racing the pump's
        # drain of the done sentinel. Reproduce that: mark exited — even with a
        # clean 0 — then drop it before the pump reads the code. A reaped session
        # must not be reported as success (returncode null would coerce to 0).
        session.mark_exited(0)
        auth_sessions._sessions.pop(session.id, None)
    monkeypatch.setattr(auth_sessions, "_finalize", reap_then_finalize)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "auth-start-1",
            "payload": {"harness": "claudecode", "mode": "login", "cols": 80, "rows": 24},
        })
        result = _receive_command_result(ws)
        assert result["ok"] is True

        os.close(write_fd)
        frame = ws.receive_json()
        assert frame["type"] == "auth.done"
        assert frame["payload"]["session_id"] == result["session_id"]
        assert frame["payload"]["returncode"] == -1


def test_auth_start_resend_replays_ring_without_second_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    read_fd, write_fd = os.pipe()
    spawn_calls = {"n": 0}

    def counting_spawn(argv, env, cols, rows):
        spawn_calls["n"] += 1
        return (99999, read_fd)
    monkeypatch.setattr(auth_sessions, "_spawn_pty", counting_spawn)
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        payload = {"harness": "claudecode", "mode": "login", "cols": 80, "rows": 24}
        ws.send_json({"v": 1, "type": "auth.start", "request_id": "auth-start-1", "payload": payload})
        result = _receive_command_result(ws)
        assert result["ok"] is True
        assert spawn_calls["n"] == 1

        # Emit some output so the ring buffer is non-empty, then resend the same
        # auth.start (reconnect replay) — it must not spawn again and must
        # replay the buffered bytes first.
        os.write(write_fd, b"buffered")
        frame = ws.receive_json()
        assert frame["type"] == "auth.output"
        assert base64.b64decode(frame["payload"]["data"]) == b"buffered"

        ws.send_json({"v": 1, "type": "auth.start", "request_id": "auth-start-1", "payload": payload})
        replayed = _receive_command_result(ws)
        assert replayed["session_id"] == result["session_id"]
        assert spawn_calls["n"] == 1

        frame = ws.receive_json()
        assert frame["type"] == "auth.output"
        assert frame["payload"]["session_id"] == result["session_id"]
        assert base64.b64decode(frame["payload"]["data"]) == b"buffered"


def test_auth_input_resize_cancel_fire_and_forget(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    writes = []
    resizes = []
    cancels = {"n": 0}

    async def fake_create_session(target_id, cols, rows, mode="login", model=None):
        session = auth_sessions.AuthSession("fake-session", target_id, 99999, -1, f"{mode}: {target_id}")
        auth_sessions._sessions[session.id] = session
        return session
    monkeypatch.setattr(auth_sessions, "create_session", fake_create_session)
    monkeypatch.setattr(auth_sessions, "write_input", lambda session, data: (writes.append((session.id, data)), session.touch()))
    monkeypatch.setattr(auth_sessions, "resize", lambda session, cols, rows: resizes.append((session.id, cols, rows)))
    monkeypatch.setattr(auth_sessions, "cancel_session", _fake_cancel(cancels))

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "auth-start-1",
            "payload": {"harness": "claudecode", "mode": "login", "cols": 80, "rows": 24},
        })
        result = _receive_command_result(ws)
        assert result["ok"] is True and result["session_id"] == "fake-session"

        ws.send_json({"v": 1, "type": "auth.input", "request_id": "input-1", "payload": {"session_id": "fake-session", "data": "hi"}})
        assert _receive_command_result(ws) == {"ok": True}
        assert writes == [("fake-session", b"hi")]

        ws.send_json({"v": 1, "type": "auth.resize", "request_id": "resize-1", "payload": {"session_id": "fake-session", "cols": 100, "rows": 30}})
        assert _receive_command_result(ws) == {"ok": True}
        assert resizes == [("fake-session", 100, 30)]

        # Fire-and-forget types must not persist idempotency rows.
        principal = f"local:{CLIENT_ID}"
        assert stats_db.get_realtime_request(principal, "input-1") is None
        assert stats_db.get_realtime_request(principal, "resize-1") is None

        # auth.cancel is fire-and-forget like input/resize, not an idempotent
        # mutation: the client (closeAuthPanel) never reads its result and
        # mints a fresh request_id every call, so there is nothing to replay
        # and persisting a row for it was pure dead weight. Each send re-runs
        # cancel_session (safe: a real cancel_session on an already-gone
        # session just returns False, it doesn't error).
        ws.send_json({"v": 1, "type": "auth.cancel", "request_id": "cancel-1", "payload": {"session_id": "fake-session"}})
        first = _receive_command_result(ws)
        ws.send_json({"v": 1, "type": "auth.cancel", "request_id": "cancel-1", "payload": {"session_id": "fake-session"}})
        second = _receive_command_result(ws)
        assert first == second == {"ok": True, "cancelled": True, "session_id": "fake-session"}
        assert cancels["n"] == 2
        assert stats_db.get_realtime_request(principal, "cancel-1") is None


def test_create_session_unlock_mode_spawns_fixed_argv_on_darwin(monkeypatch):
    # See docs/plans/cursor-keychain-unlock-remediation.md — mode="unlock" is
    # the allowlisted `security unlock-keychain` PTY session, never built from
    # user input, gated to darwin.
    monkeypatch.setattr(auth_sessions.sys, "platform", "darwin")
    read_fd, write_fd = os.pipe()
    spawned = {}

    def fake_spawn(argv, env, cols, rows):
        spawned["argv"] = argv
        return 99999, read_fd
    monkeypatch.setattr(auth_sessions, "_spawn_pty", fake_spawn)
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    async def fake_finalize(session):
        session.mark_exited(0)
    monkeypatch.setattr(auth_sessions, "_finalize", fake_finalize)

    async def run():
        return await auth_sessions.create_session("keychain", 80, 24, mode="unlock")
    session = None
    try:
        session = asyncio.run(run())
        assert spawned["argv"] == ["security", "unlock-keychain"]
        assert session.display_command == "security unlock-keychain"
    finally:
        os.close(write_fd)
        if session:
            auth_sessions._sessions.pop(session.id, None)


def test_create_session_unlock_mode_raises_on_non_darwin(monkeypatch):
    monkeypatch.setattr(auth_sessions.sys, "platform", "linux")

    async def run():
        await auth_sessions.create_session("keychain", 80, 24, mode="unlock")

    try:
        asyncio.run(run())
        assert False, "expected AuthSessionError"
    except auth_sessions.AuthSessionError:
        pass


def test_resize_touches_idle_timer():
    # resize() must refresh last_activity so the idle reaper doesn't reap a
    # session a client is actively resizing (window drags / rotation produce
    # resizes with no input). Regression for the WS and HTTP resize paths.
    master_fd, slave_fd = os.openpty()
    try:
        session = auth_sessions.AuthSession("s", "claudecode", 99999, master_fd, "claude auth login")
        session.last_activity = 0.0
        auth_sessions.resize(session, 100, 30)
        assert session.last_activity > 0.0
    finally:
        os.close(slave_fd)
        os.close(master_fd)


def test_keychain_unlock_allowed_gate(monkeypatch):
    # Direct unit coverage of the gate itself (see the fail-closed rationale
    # in its docstring): a raw loopback peer with no forwarding markers is
    # the only thing that passes. Presence of a forwarding header must deny
    # regardless of its value, since X-Forwarded-For is attacker-controlled
    # input a remote client can set to anything, including "127.0.0.1".
    monkeypatch.setattr(server, "ALLOW_REMOTE_KEYCHAIN_UNLOCK", False)
    allowed = server._keychain_unlock_allowed

    assert allowed({}, "127.0.0.1") is True
    assert allowed({}, "::1") is True
    assert allowed({}, "testclient") is False  # not a loopback address at all
    assert allowed({}, None) is False
    # The spoof this gate exists to stop: a remote client simply sets XFF to
    # a loopback-looking value itself.
    assert allowed({"x-forwarded-for": "127.0.0.1"}, "127.0.0.1") is False
    assert allowed({"x-forwarded-for": "100.101.102.103"}, "127.0.0.1") is False
    assert allowed({"tailscale-user-login": "someone@example.com"}, "127.0.0.1") is False
    # Fail-closed must hold for every common forwarding marker, not just XFF —
    # a proxy that sets only X-Real-IP or RFC 7239 Forwarded is still remote.
    assert allowed({"x-real-ip": "127.0.0.1"}, "127.0.0.1") is False
    assert allowed({"forwarded": "for=127.0.0.1"}, "127.0.0.1") is False

    monkeypatch.setattr(server, "ALLOW_REMOTE_KEYCHAIN_UNLOCK", True)
    assert allowed({"x-forwarded-for": "100.101.102.103"}, "127.0.0.1") is True


def test_auth_start_unlock_loopback_gate(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_sessions.sys, "platform", "darwin")
    monkeypatch.setattr(server, "ALLOW_REMOTE_KEYCHAIN_UNLOCK", False)
    # A real PTY pair, not a pipe — write_input() writes to the master fd
    # (simulating typing the keychain password at the prompt), which a
    # one-directional pipe can't support.
    master_fd, slave_fd = os.openpty()
    spawn_calls = {"n": 0}

    def counting_spawn(argv, env, cols, rows):
        spawn_calls["n"] += 1
        assert argv == ["security", "unlock-keychain"]
        return 99999, master_fd
    monkeypatch.setattr(auth_sessions, "_spawn_pty", counting_spawn)
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        # TestClient's own peer address ("testclient") isn't a loopback IP and
        # no X-Forwarded-For is set, so the default-deny path is exercised
        # without needing to fabricate a real remote address.
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "unlock-remote",
            "payload": {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24},
        })
        refused = _receive_command_result(ws)
        assert refused == {
            "ok": False, "error": "unlock_requires_local",
            "detail": "Keychain unlock is only available from a loopback client "
                      "unless auth.allow_remote_keychain_unlock is enabled.",
        }
        assert spawn_calls["n"] == 0

    with TestClient(server.app).websocket_connect(
        "/ws/v1", headers={"x-forwarded-for": "127.0.0.1"},
    ) as ws:
        _subscribe_auth(ws)
        # The spoof this gate exists to stop: a remote (tailnet) client can
        # set X-Forwarded-For to a loopback-looking value itself. The raw TCP
        # peer being loopback proves nothing either — tailscale serve
        # reverse-proxies to this server over loopback, so every tailnet
        # request also arrives with a 127.0.0.1 peer. The mere presence of
        # the header must deny, regardless of its value.
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "unlock-spoofed",
            "payload": {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24},
        })
        refused_spoof = _receive_command_result(ws)
        assert refused_spoof["ok"] is False
        assert refused_spoof["error"] == "unlock_requires_local"
        assert spawn_calls["n"] == 0

    # The only case that's actually allowed: a direct connection (no
    # forwarding headers at all) whose raw TCP peer is genuinely loopback —
    # simulated here via TestClient's own `client` address, since a proxy
    # never sits in front of a truly direct connection to add headers.
    with TestClient(server.app, client=("127.0.0.1", 54321)).websocket_connect("/ws/v1") as ws:
        _subscribe_auth(ws)
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "unlock-local",
            "payload": {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24},
        })
        allowed = _receive_command_result(ws)
        assert allowed["ok"] is True
        assert allowed["command"] == "security unlock-keychain"
        assert spawn_calls["n"] == 1

        # Same fire-and-forget guarantee as other auth input: the keychain
        # password never gets persisted as part of a realtime_request row.
        principal = f"local:{CLIENT_ID}"
        ws.send_json({
            "v": 1, "type": "auth.input", "request_id": "unlock-input-1",
            "payload": {"session_id": allowed["session_id"], "data": "hunter2\n"},
        })
        assert _receive_command_result(ws) == {"ok": True}
        assert stats_db.get_realtime_request(principal, "unlock-input-1") is None

    os.close(slave_fd)


def test_auth_start_unlock_allowed_remotely_when_opted_in(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_sessions.sys, "platform", "darwin")
    monkeypatch.setattr(server, "ALLOW_REMOTE_KEYCHAIN_UNLOCK", True)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(auth_sessions, "_spawn_pty", lambda argv, env, cols, rows: (99999, read_fd))
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)

    with TestClient(server.app).websocket_connect(
        "/ws/v1", headers={"x-forwarded-for": "100.101.102.103"},
    ) as ws:
        _subscribe_auth(ws)
        ws.send_json({
            "v": 1, "type": "auth.start", "request_id": "unlock-opt-in",
            "payload": {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24},
        })
        result = _receive_command_result(ws)
        assert result["ok"] is True

    os.close(write_fd)
