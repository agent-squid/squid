import asyncio
import base64
import json
import logging
import os
import sqlite3
import threading
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.browser_client import PairedBrowserClient
from starlette.websockets import WebSocketDisconnect

from agent import auth_sessions, server, stats_db
from agent import providers as providers_mod
from agent.providers import Provider


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
    assert all(set(message) == {"id", "topic", "agent", "role", "status"}
               for message in snapshot["conversations"][0]["messages"])


def test_scoped_realtime_snapshot_omits_history_only_metadata(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.insert_run_event(msg_id, 0, "text", "partial")
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE chat_messages SET context=?, status_raw=? WHERE id=?",
            ('[{"name":"large tool trace"}]', "verbose status trace", msg_id),
        )

    snapshot = stats_db.get_realtime_snapshot([{"topic": "squid", "agent": "codex"}], 20)
    message = next(item for item in snapshot["conversations"][0]["messages"] if item["id"] == msg_id)

    assert message["content"] == "partial"
    assert message["run_seq"] == 0
    assert "context" not in message
    assert "status_raw" not in message


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


def test_worktree_state_and_reverts_are_replayable_across_clients(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "change files")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.save_worktree("squid", str(msg_id), "/repo", "/repo-wt", "turn-branch")
    cursor = stats_db.get_realtime_cursor()

    stats_db.mark_worktree_status("squid", str(msg_id), "/repo", "resolved")
    stats_db.record_git_diff_revert(msg_id, "/repo", ["ui/app.js", "ui/style.css"])

    events = stats_db.get_realtime_events(cursor, [{"topic": "squid", "agent": "codex"}])
    assert [(event["event_type"], event["msg_id"], event["payload"]) for event in events] == [
        ("worktree.changed", msg_id, {"repo": "/repo", "status": "resolved"}),
        ("diff.reverted", msg_id, {
            "repo": "/repo", "files": ["ui/app.js", "ui/style.css"],
        }),
    ]

    cursor = stats_db.get_realtime_cursor()
    stats_db.mark_worktree_status("squid", str(msg_id), "/repo", "resolved")
    stats_db.record_git_diff_revert(msg_id, "/repo", ["ui/app.js", "ui/style.css"])
    assert stats_db.get_realtime_events(cursor, [{"lifecycle": "global"}]) == []


def test_clean_worktree_deletion_publishes_synced_state(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "change files")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.save_worktree("squid", str(msg_id), "/repo", "/repo-wt", "turn-branch")
    cursor = stats_db.get_realtime_cursor()

    stats_db.delete_worktree("squid", str(msg_id), "/repo")

    events = stats_db.get_realtime_events(cursor, [{"lifecycle": "global"}])
    assert [(event["event_type"], event["msg_id"], event["payload"]) for event in events] == [
        ("worktree.changed", msg_id, {"repo": "/repo", "status": "synced"}),
    ]


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

    with PairedBrowserClient(server.app):
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

        cancel = {"v": 1, "type": "chat.cancel", "request_id": "cancel-1", "payload": {"msg_id": msg_id, "source": "live_bubble_stop"}}
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
        assert first["payload"]["source"] == "live_bubble_stop"


def test_websocket_dispatches_worktree_auto_resolve_command(tmp_path, monkeypatch):
    # Regression test: the top-level frame dispatch must route
    # "worktree.auto_resolve" to _handle_realtime_mutation like the other
    # mutation types, or it silently falls through to the catch-all
    # "unsupported_type" error — which carries no request_id, so the client
    # never matches it to the pending command and just times out.
    _fresh_db(tmp_path, monkeypatch)

    async def fake_auto_resolve(payload):
        assert payload == {"msg_id": 42, "topic": "squid", "repo": "/repo"}
        return {"ok": True, "msg_id": 84, "worktree_msg_id": 42, "agent": "claude", "provider": "anthropic"}

    monkeypatch.setattr(server, "_realtime_worktree_auto_resolve", fake_auto_resolve)

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {"client_id": CLIENT_ID, "scopes": [{"topic": "squid", "agent": "claude"}]},
        })
        assert ws.receive_json()["type"] == "subscribed"
        ws.receive_json()  # snapshot

        ws.send_json({
            "v": 1, "type": "worktree.auto_resolve", "request_id": "auto-resolve-1",
            "payload": {"msg_id": 42, "topic": "squid", "repo": "/repo"},
        })
        result = ws.receive_json()
        while result["type"] != "command.result":
            result = ws.receive_json()
        assert result["request_id"] == "auto-resolve-1"
        assert result["payload"]["ok"] is True
        assert result["payload"]["msg_id"] == 84


def test_websocket_rejects_protocol_version_skew(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        hello = ws.receive_json()
        assert hello["payload"]["supported_versions"] == [1]
        ws.send_json({"v": 99, "type": "ping", "payload": {}})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "unsupported_version"
        assert error["payload"]["supported_versions"] == [1]


def test_websocket_rejects_unauthorized_scope(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
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


def test_ollama_native_base_strips_v1_suffix():
    assert providers_mod._ollama_native_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert providers_mod._ollama_native_base("http://gpu1.internal:11434/v1/") == "http://gpu1.internal:11434"
    assert providers_mod._ollama_native_base(None) == "http://localhost:11434"


class _FakeOllamaResponse:
    def __init__(self, lines=None, error=None):
        self._lines = lines or []
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeOllamaClient:
    """Stands in for httpx.AsyncClient so pull/remove tests never hit a
    real Ollama daemon. `calls` records (method, url, json) for assertions;
    `pull_lines`/`error` control what the session sees."""

    def __init__(self, calls, pull_lines=None, error=None):
        self.calls = calls
        self.pull_lines = pull_lines or []
        self.error = error

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None):
        self.calls.append((method, url, json))
        return _FakeStreamCtx(_FakeOllamaResponse(lines=self.pull_lines, error=self.error))

    async def request(self, method, url, json=None):
        self.calls.append((method, url, json))
        return _FakeOllamaResponse(error=self.error)


def _fake_ollama_provider(monkeypatch):
    provider = Provider(id="ollama", auth_type="none", base_url="http://localhost:11434/v1")
    monkeypatch.setitem(providers_mod.PROVIDERS, "ollama", provider)
    return provider


async def _run_and_await(target_id, mode, model):
    session = await auth_sessions.create_session(target_id, 80, 24, mode=mode, model=model)
    await session.api_task
    return session


def test_create_session_pull_streams_ollama_api_progress(monkeypatch):
    _fake_ollama_provider(monkeypatch)
    calls = []
    fake_client = _FakeOllamaClient(calls, pull_lines=[
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "completed": 50, "total": 100}),
        json.dumps({"status": "success"}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(auth_sessions, "_expire_session", AsyncMock())

    session = None
    try:
        session = asyncio.run(_run_and_await("ollama", "pull", "llama3.2:3b"))
        assert calls == [("POST", "http://localhost:11434/api/pull",
                           {"name": "llama3.2:3b", "stream": True})]
        assert session.state == "exited"
        assert session.returncode == 0
        output = bytes(session.buffer).decode()
        assert "pulling manifest" in output
        assert "downloading 50/100" in output
        assert "success" in output
    finally:
        if session:
            auth_sessions._sessions.pop(session.id, None)


def test_create_session_pull_reports_api_error(monkeypatch):
    _fake_ollama_provider(monkeypatch)
    calls = []
    # raise_for_status() fires before aiter_lines() is ever consumed.
    not_found = httpx.HTTPStatusError(
        "404", request=httpx.Request("POST", "http://x"), response=httpx.Response(404))
    fake_client = _FakeOllamaClient(calls, error=not_found)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(auth_sessions, "_expire_session", AsyncMock())

    session = None
    try:
        session = asyncio.run(_run_and_await("ollama", "pull", "nonexistent:1b"))
        assert session.state == "exited"
        assert session.returncode == 1
        assert b"Ollama API error" in bytes(session.buffer)
    finally:
        if session:
            auth_sessions._sessions.pop(session.id, None)


def test_create_session_remove_rejects_uninstalled_model(monkeypatch):
    _fake_ollama_provider(monkeypatch)
    monkeypatch.setattr(providers_mod, "_installed_ollama_models", lambda base_url=None: {"qwen3:8b"})

    async def run():
        await auth_sessions.create_session("ollama", 80, 24, mode="remove", model="qwen3:8b-not-installed")

    try:
        asyncio.run(run())
        assert False, "expected AuthSessionError"
    except auth_sessions.AuthSessionError as exc:
        assert "not an installed ollama model" in str(exc)


def test_create_session_remove_calls_delete_api(monkeypatch):
    _fake_ollama_provider(monkeypatch)
    monkeypatch.setattr(providers_mod, "_installed_ollama_models", lambda base_url=None: {"qwen3:8b"})
    calls = []
    fake_client = _FakeOllamaClient(calls)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(auth_sessions, "_expire_session", AsyncMock())

    session = None
    try:
        session = asyncio.run(_run_and_await("ollama", "remove", "qwen3:8b"))
        assert calls == [("DELETE", "http://localhost:11434/api/delete", {"name": "qwen3:8b"})]
        assert session.state == "exited"
        assert session.returncode == 0
        assert b"removed qwen3:8b" in bytes(session.buffer)
    finally:
        if session:
            auth_sessions._sessions.pop(session.id, None)


def test_create_session_pull_unknown_provider_raises(monkeypatch):
    monkeypatch.delitem(providers_mod.PROVIDERS, "ollama", raising=False)

    async def run():
        await auth_sessions.create_session("ollama", 80, 24, mode="pull", model="llama3.2:3b")

    try:
        asyncio.run(run())
        assert False, "expected AuthSessionError"
    except auth_sessions.AuthSessionError as exc:
        assert "Unknown Ollama provider" in str(exc)


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


def test_remote_keychain_unlock_is_available_and_bound_to_its_browser(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_sessions.sys, "platform", "darwin")
    master_fd, slave_fd = os.openpty()

    def spawn(argv, env, cols, rows):
        assert argv == ["security", "unlock-keychain"]
        return 99999, master_fd

    monkeypatch.setattr(auth_sessions, "_spawn_pty", spawn)
    monkeypatch.setattr(auth_sessions, "_register_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_deregister_proc", lambda *a, **k: None)
    monkeypatch.setattr(auth_sessions, "_signal_process_group", lambda *a, **k: None)
    origin = "https://squid.example"
    owner = PairedBrowserClient(server.app, base_url=origin, headers={"x-forwarded-for": "100.64.0.2"})
    other = PairedBrowserClient(server.app, base_url=origin)
    try:
        with owner.websocket_connect("/ws/v1") as ws:
            _subscribe_auth(ws)
            ws.send_json({
                "v": 1, "type": "auth.start", "request_id": "unlock",
                "payload": {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24},
            })
            result = _receive_command_result(ws)
            assert result["ok"] is True
            assert result["command"] == "security unlock-keychain"
            path = f"/auth/session/{result['session_id']}"
            assert owner.get(path).status_code == 200
            assert other.get(path).status_code == 404
            assert other.get(f"{path}/events").status_code == 404
            assert other.post(f"{path}/input", json={"data": "test\n"}).status_code == 404
            assert other.post(f"{path}/resize", json={"cols": 80, "rows": 24}).status_code == 404
            assert other.post(f"{path}/cancel").json() == {"ok": False}
            frame = {
                "v": 1, "type": "auth.input", "request_id": "unlock-input",
                "payload": {"session_id": result["session_id"], "data": "test\n"},
            }
            with other.websocket_connect("/ws/v1") as other_ws:
                _subscribe_auth(other_ws)
                other_ws.send_json(frame)
                assert _receive_command_result(other_ws)["error"] == "unknown_session"
            ws.send_json(frame)
            assert _receive_command_result(ws) == {"ok": True}
            assert stats_db.get_realtime_request(f"local:{CLIENT_ID}", "unlock-input") is None
    finally:
        os.close(slave_fd)


def test_outbound_queue_coalesces_process_changed():
    async def run():
        outbound = server._RealtimeOutbound(4)
        assert outbound.enqueue({"type": "chat.text", "event_id": 1}) is True
        assert outbound.enqueue({"type": "process.changed", "payload": {"processes": [1]}}) is True
        assert outbound.enqueue({"type": "process.changed", "payload": {"processes": [2]}}) is True
        assert outbound.enqueue({"type": "queue.changed", "payload": {"queue": [1]}}) is True
        assert outbound.enqueue({"type": "queue.changed", "payload": {"queue": [2]}}) is True
        assert len(outbound) == 3  # chat.text + one process.changed + one queue.changed
        # Non-coalescible frames overflow at the limit.
        assert outbound.enqueue({"type": "chat.text", "event_id": 2}) is True
        assert len(outbound) == 4
        assert outbound.enqueue({"type": "chat.text", "event_id": 3}) is False
        # Coalescible frames never overflow — they replace in place.
        assert outbound.enqueue({"type": "process.changed", "payload": {"processes": [3]}}) is True
        assert len(outbound) == 4
        n = len(outbound)
        frames = [await outbound.get() for _ in range(n)]
        types = [frame["type"] for frame in frames]
        assert types.count("process.changed") == 1
        assert types.count("queue.changed") == 1
        process = next(frame for frame in frames if frame["type"] == "process.changed")
        assert process["payload"] == {"processes": [3]}
        return True

    assert asyncio.run(run()) is True


def test_slow_consumer_closes_with_resumable_error(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_OUTBOUND_QUEUE_LIMIT", 4)

    async def stalled_sender(_websocket, _outbound):
        await asyncio.sleep(30)

    monkeypatch.setattr(server, "_ws_sender", stalled_sender)

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        # Each inbound ping enqueues one non-coalescible pong; the stalled
        # sender never drains, so the 5th overflows the bounded queue.
        for _ in range(5):
            ws.send_json({"v": 1, "type": "ping", "payload": {}})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"] == {"code": "slow_consumer", "resumable": True}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


def test_frame_too_large_closes_connection(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_MAX_FRAME_BYTES", 64)
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_text("x" * 65)
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "frame_too_large"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_malformed_frames_get_invalid_frame(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_text("not json")
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "invalid_frame"
        ws.send_text("[1, 2]")
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "invalid_frame"
        # The connection stays open after malformed frames.
        ws.send_json({"v": 1, "type": "ping", "payload": {}})
        assert ws.receive_json()["type"] == "pong"


def test_server_initiated_heartbeat_and_dead_peer_close(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_HEARTBEAT_SECONDS", 1)
    monkeypatch.setattr(server, "_REALTIME_HEARTBEAT_MISS_LIMIT", 2)

    # A peer that never sends is closed after the miss limit.
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "ping"
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_json()
        assert exc.value.code == 1001

    # A peer that answers pings stays open and keeps receiving pings.
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        for _ in range(2):
            assert ws.receive_json()["type"] == "ping"
            ws.send_json({"v": 1, "type": "pong", "payload": {}})


def test_ack_cursor_is_recorded_and_clamped(tmp_path, monkeypatch, caplog):
    _fresh_db(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="agent.server"):
        with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"v": 1, "type": "ack", "payload": {"event_id": 0}})
            ws.send_json({"v": 1, "type": "ack", "payload": {"event_id": 999999}})
            # Valid acks produce no error frame; the connection stays open.
            ws.send_json({"v": 1, "type": "ping", "payload": {}})
            assert ws.receive_json()["type"] == "pong"
            # An invalid ack yields invalid_frame.
            ws.send_json({"v": 1, "type": "ack", "payload": {"event_id": "nope"}})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["payload"]["code"] == "invalid_frame"
    assert "clamped" in caplog.text


def test_subscribe_replay_under_outbound_pressure_preserves_order(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_OUTBOUND_QUEUE_LIMIT", 8)
    ids = [
        stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": index})
        for index in range(30)
    ]

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {"client_id": CLIENT_ID, "cursor": 0, "scopes": [{"topic": "squid", "agent": "codex"}]},
        })
        assert ws.receive_json()["type"] == "subscribed"
        received = []
        for _ in range(30):
            frame = ws.receive_json()
            assert frame["type"] == "message.changed"
            received.append(frame["event_id"])
    assert received == ids
    assert received == sorted(received)


def test_reconnect_replays_from_last_applied_cursor(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    subscribe = {
        "v": 1, "type": "subscribe",
        "payload": {"client_id": CLIENT_ID, "scopes": [{"topic": "squid", "agent": "codex"}]},
    }

    # Disconnect before chat metadata: snapshot at cursor 0, then drop the
    # socket before the turn's meta/text are published.
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json(subscribe)
        assert ws.receive_json()["type"] == "subscribed"
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        first_cursor = snapshot["event_id"]

    stats_db.insert_run_event(msg_id, 0, "meta", None)
    stats_db.insert_run_event(msg_id, 1, "text", "partial")

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        subscribe["payload"]["cursor"] = first_cursor
        ws.send_json(subscribe)
        assert ws.receive_json()["type"] == "subscribed"
        replayed = [ws.receive_json(), ws.receive_json()]
        assert [(frame["type"], frame["run_seq"]) for frame in replayed] == [
            ("chat.meta", 0), ("chat.text", 1),
        ]
        last_cursor = replayed[-1]["event_id"]

    # Disconnect after chat metadata: reconnect from the last applied cursor;
    # the already-applied meta/text are not re-delivered, only the new done.
    stats_db.insert_run_event(msg_id, 2, "done", None)
    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        subscribe["payload"]["cursor"] = last_cursor
        ws.send_json(subscribe)
        assert ws.receive_json()["type"] == "subscribed"
        replay = ws.receive_json()
        assert replay["type"] == "chat.done"
        assert replay["event_id"] > last_cursor


def test_multiple_simultaneous_clients_receive_live_events(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    second_id = "testclient0000000000000002"

    with PairedBrowserClient(server.app) as client, \
         client.websocket_connect("/ws/v1") as first, \
         client.websocket_connect("/ws/v1") as second:
        for client_id, ws in ((CLIENT_ID, first), (second_id, second)):
            ws.receive_json()
            ws.send_json({
                "v": 1, "type": "subscribe",
                "payload": {"client_id": client_id, "scopes": [{"topic": "squid", "agent": "codex"}]},
            })
            assert ws.receive_json()["type"] == "subscribed"
            assert ws.receive_json()["type"] == "snapshot"

        stats_db.insert_run_event(msg_id, 0, "text", "both see this")

        for ws in (first, second):
            event = ws.receive_json()
            assert event["type"] == "chat.text"
            assert event["payload"] == {"text": "both see this"}


def test_safety_poll_recovers_a_lost_notification(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_REALTIME_SAFETY_POLL_SECONDS", 0.1)

    with PairedBrowserClient(server.app).websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({
            "v": 1, "type": "subscribe",
            "payload": {"client_id": CLIENT_ID, "scopes": [{"topic": "squid", "agent": "codex"}]},
        })
        assert ws.receive_json()["type"] == "subscribed"
        assert ws.receive_json()["type"] == "snapshot"

        # Drop the commit listener so the commit below does not wake the
        # socket, then commit directly to the durable log. The 20s safety poll
        # (patched to 0.1s) must still drain it after its timeout.
        stats_db.set_realtime_commit_listener(None)
        try:
            stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": "polled"})
        finally:
            stats_db.set_realtime_commit_listener(server._realtime_notifier.notify_committed)

        event = ws.receive_json()
        assert event["type"] == "message.changed"
        assert event["payload"] == {"id": "polled"}


def test_ws_sender_reports_send_after_close_as_disconnect():
    class ClosedSocket:
        async def send_json(self, _frame):
            raise RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'.")

    async def run():
        outbound = server._RealtimeOutbound(8)
        await server._realtime_send(outbound, {"v": 1, "type": "ping"}, None, -1)
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(server._ws_sender(ClosedSocket(), outbound), timeout=1)

    asyncio.run(run())


@pytest.mark.parametrize("state,returncode", [("running", None), ("exited", 0), ("exited", 1)])
def test_auth_status_recovers_completion_without_event_stream(monkeypatch, state, returncode):
    session = auth_sessions.AuthSession("status-test", "codex", 99999, -1, "codex login --device-auth")
    session.state = state
    session.returncode = returncode
    monkeypatch.setattr(auth_sessions, "_sessions", {session.id: session})
    client = PairedBrowserClient(server.app)
    response = client.get(f"/auth/session/{session.id}")
    assert response.status_code == 200
    assert response.json() == {"state": state, "returncode": returncode}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/auth/session/missing").status_code == 404


def test_history_check_requires_identity_and_returns_exact_existing_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(server, '_REALTIME_OUTBOUND_QUEUE_LIMIT', 8)
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message('squid', 'codex', 'hello')
    msg_id = stats_db.insert_assistant_message('squid', 'codex', user_id)
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        query = {'v': 1, 'type': 'history.check', 'request_id': 'history-test',
                 'payload': {'ids': [msg_id, 999999]}}
        ws.send_json(query)
        assert ws.receive_json()['payload']['error'] == 'client_identity_required'
        ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
            'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
        assert ws.receive_json()['type'] == 'subscribed'
        assert ws.receive_json()['type'] == 'snapshot'
        ws.send_json(query)
        result = _receive_command_result(ws)
        assert result['ok'] is True
        assert [row['id'] for row in result['items']] == [msg_id]
        stats_db.update_assistant_message(msg_id, 'updated content', None)
        ws.send_json(query)
        assert _receive_command_result(ws)['items'][0]['content'] == 'updated content'
        monkeypatch.setattr(server, '_REALTIME_MAX_FRAME_BYTES', 512)
        content = ('𐍈"' + chr(92) + chr(10)) * 280
        stats_db.update_assistant_message(msg_id, content, None)
        ws.send_json(query)
        assert _receive_command_result(ws)['error'] == 'history_result_too_large'
        ws.send_json({**query, 'payload': {'ids': [msg_id], 'chunked': True}})
        chunks = []
        while True:
            frame = ws.receive_json()
            if frame.get('request_id') != query['request_id']:
                continue
            assert len(json.dumps(frame, ensure_ascii=False, separators=(',', ':')).encode()) <= 512
            if frame['type'] == 'command.result':
                assert frame['payload'] == {'ok': True, 'chunk_count': len(chunks)}
                break
            assert frame['type'] == 'history.chunk'
            assert frame['payload']['index'] == len(chunks)
            chunks.append(frame['payload']['text'])
        assert len(chunks) > 1
        item, = json.loads(''.join(chunks))['items']
        assert item['id'] == msg_id and item['content'] == content
        stats_db.delete_topic('squid')
        ws.send_json(query)
        assert _receive_command_result(ws)['items'] == []
        for ids in [[True], ['1'], [0], [2**64], list(range(1, 102))]:
            ws.send_json({**query, 'payload': {'ids': ids}})
            assert _receive_command_result(ws)['error'] == 'invalid_ids'


@pytest.mark.parametrize("name", ["Diff", "Bash"])
def test_oversized_tool_patches_are_deferred_in_realtime_history(tmp_path, monkeypatch, name):
    _fresh_db(tmp_path, monkeypatch)
    limit = server._REALTIME_MAX_FRAME_BYTES
    user_id = stats_db.insert_user_message("squid", "codex", "change a file")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    patch = "+old tool payload\n" * (limit // 18 + 1)
    tool = {"name": "Diff", "file": "app.py", "diff": patch}
    summary = {"name": "Diff", "file": "app.py", "detail_index": 0}
    if name == "Bash":
        tool = {"name": "Bash", "command": "apply_patch <<'PATCH'\n" + patch + "PATCH"}
        summary = {"name": "Bash"}
    tools = [
        tool,
        {"name": "GitDiff", "diff": "", "diff_deferred": True, "files": [{"path": "app.py", "status": "M"}]},
    ]
    stats_db.update_assistant_message(msg_id, "Reply is intact", None, "done", context=json.dumps(tools))
    client = PairedBrowserClient(server.app)
    with client.websocket_connect("/ws/v1") as ws:
        ws.receive_json()
        ws.send_json({"v": 1, "type": "subscribe", "payload": {
            "client_id": CLIENT_ID, "scopes": [{"lifecycle": "global"}],
        }})
        assert ws.receive_json()["type"] == "subscribed"
        assert ws.receive_json()["type"] == "snapshot"
        for kind, payload in (
            ("history.page", {"limit": 1}),
            ("history.check", {"ids": [msg_id]}),
        ):
            ws.send_json({"v": 1, "type": kind, "request_id": kind, "payload": payload})
            result = _receive_command_result(ws)
            assert result["ok"] is True
            item, = result["items"]
            assert item["content"] == "Reply is intact"
            assert json.loads(item["context"]) == [summary, tools[1]]

    # The status response used by the thought trace still carries the full command.
    assert json.loads(client.get(f"/chat/{msg_id}/status").json()["context"]) == tools
    if name == "Bash":
        return

    url = f"/chat/{msg_id}/diff-tool/0"
    first = client.get(url).json()
    assert first["diff"] == patch[:first["next_offset"]]
    second = client.get(url, params={
        "offset": first["next_offset"], "cursor": first["next_cursor"],
    }).json()
    assert second["diff"] == patch[first["next_offset"]:second["next_offset"]]
    assert client.get(f"/chat/{msg_id}/diff-tool/1").status_code == 404


def test_history_shares_one_inline_budget_across_small_tool_patches(monkeypatch):
    monkeypatch.setattr(server, "_REALTIME_MAX_FRAME_BYTES", 256 * 1024)
    patch = "+" + "x" * 24576
    tools = [
        {"name": "Diff", "file": f"{i}.py", "diff": patch} for i in range(3)
    ]
    assert server._defer_history_patches(tools)
    assert [tool["detail_index"] for tool in tools if "detail_index" in tool] == [2]
    assert len([tool for tool in tools if tool.get("diff") == patch]) == 2


@pytest.mark.parametrize('filter_name,annotation', [('bookmarked', 'bookmark'), ('marked_bad', 'bad_response')])
def test_history_check_respects_existing_row_filter_membership(tmp_path, monkeypatch, filter_name, annotation):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message('squid', 'codex', 'hello')
    msg_id = stats_db.insert_assistant_message('squid', 'codex', user_id)
    stats_db.update_assistant_message(msg_id, 'original', None)
    stats_db.set_message_annotation(msg_id, annotation)
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
            'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
        assert ws.receive_json()['type'] == 'subscribed'
        assert ws.receive_json()['type'] == 'snapshot'
        query = {'v': 1, 'type': 'history.check', 'request_id': 'filtered-history',
                 'payload': {'ids': [msg_id], filter_name: True}}
        stats_db.update_assistant_message(msg_id, 'updated', None)
        ws.send_json(query)
        assert _receive_command_result(ws)['items'][0]['content'] == 'updated'
        stats_db.remove_message_annotation(msg_id, annotation)
        ws.send_json(query)
        assert _receive_command_result(ws)['items'] == []
        ws.send_json({**query, 'payload': {'ids': [msg_id], filter_name: 'true'}})
        assert _receive_command_result(ws)['error'] == 'invalid_ids'


def test_ws_health_query_requires_identity_and_reports_boot_version(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        query = {'v': 1, 'type': 'health.check', 'request_id': 'health-check', 'payload': {}}
        ws.send_json(query)
        assert _receive_command_result(ws) == {'ok': False, 'error': 'client_identity_required'}
        ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
            'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
        assert ws.receive_json()['type'] == 'subscribed'
        assert ws.receive_json()['type'] == 'snapshot'
        ws.send_json(query)
        result = _receive_command_result(ws)
        assert result == {'ok': True, 'status': 'ok', 'boot_time': server.BOOT_TIME, 'version': server.SQUID_VERSION}
        # Health reads do not create durable mutation/idempotency records.
        ws.send_json(query)
        assert _receive_command_result(ws) == result


@pytest.mark.parametrize('kind', ['history.page', 'history.around'])
def test_ws_history_queries_require_identity_and_match_http(tmp_path, monkeypatch, kind):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message('squid', 'codex', 'hello')
    msg_id = stats_db.insert_assistant_message('squid', 'codex', user_id)
    stats_db.update_assistant_message(msg_id, 'world', None)
    payload = {'topic': 'squid', 'limit': 5}
    if kind == 'history.around':
        payload['msg_id'] = msg_id
    with PairedBrowserClient(server.app) as client:
        expected = client.get('/history' + ('/around' if kind.endswith('around') else ''), params=payload).json()
        with client.websocket_connect('/ws/v1') as ws:
            ws.receive_json()
            query = {'v': 1, 'type': kind, 'request_id': 'history-query', 'payload': payload}
            ws.send_json(query)
            assert _receive_command_result(ws)['error'] == 'client_identity_required'
            ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
                'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
            assert ws.receive_json()['type'] == 'subscribed'
            assert ws.receive_json()['type'] == 'snapshot'
            ws.send_json(query)
            assert _receive_command_result(ws) == {'ok': True, **expected}
            for invalid in ({'limit': True}, {'limit': 101}, {'offset': -1}, {'topic': []},
                            {'bookmarked': 'true'}, {'unknown': 1}):
                ws.send_json({**query, 'payload': invalid})
                assert _receive_command_result(ws)['error'] == 'invalid_history_query'


@pytest.mark.asyncio
async def test_ws_history_queries_reject_oversized_results(monkeypatch):
    monkeypatch.setattr(server, 'history', AsyncMock(return_value=server.JSONResponse({'items': ['x' * 5000]})))
    monkeypatch.setattr(server, '_REALTIME_MAX_FRAME_BYTES', 8192)
    assert await server._realtime_history_query('history.page', {}) == {
        'ok': False, 'error': 'history_result_too_large'}


def test_history_query_frame_sizes_complete_unicode_envelope(monkeypatch):
    monkeypatch.setattr(server, '_REALTIME_MAX_FRAME_BYTES', 256)
    frame = server._history_query_frame('é' * 50, {'ok': True, 'items': ['x' * 100]})
    assert frame['payload']['error'] == 'history_result_too_large'
    assert len(json.dumps(frame, ensure_ascii=False, separators=(',', ':')).encode()) <= 256


@pytest.mark.parametrize("kind", ["history.page", "history.check"])
def test_ws_history_rejects_unbounded_request_ids(tmp_path, monkeypatch, kind):
    _fresh_db(tmp_path, monkeypatch)
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        for request_id in (None, '', 123, 'x' * 129, 'x' * 131072, '\ud800'):
            ws.send_text(json.dumps({'v': 1, 'type': kind, 'request_id': request_id, 'payload': {}}))
            assert ws.receive_json() == {'v': 1, 'type': 'error', 'payload': {'code': 'invalid_request_id'}}
        ws.send_json({'v': 1, 'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'


@pytest.mark.parametrize("kind", ["history.page", "history.check"])
def test_slow_history_queries_preserve_socket_liveness_and_admission(tmp_path, monkeypatch, kind):
    _fresh_db(tmp_path, monkeypatch)
    release = threading.Event()
    started = threading.Event()
    async def slow_query(*args):
        started.set()
        while not release.is_set():
            await asyncio.sleep(.005)
        return {'ok': True, 'items': []}
    monkeypatch.setattr(server, '_realtime_history_query', slow_query)
    monkeypatch.setattr(server, '_REALTIME_HISTORY_QUERY_LIMIT', 1)
    monkeypatch.setattr(server, '_REALTIME_HISTORY_QUERY_TIMEOUT', .1)
    try:
        with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
            ws.receive_json()
            ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
                'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
            assert ws.receive_json()['type'] == 'subscribed'
            assert ws.receive_json()['type'] == 'snapshot'
            payload = {'ids': [1], 'chunked': True} if kind == 'history.check' else {}
            query = {'v': 1, 'type': kind, 'request_id': 'slow', 'payload': payload}
            ws.send_json(query)
            assert started.wait(2)
            ws.send_json({'v': 1, 'type': 'health.check', 'request_id': 'health', 'payload': {}})
            assert _receive_command_result(ws)['status'] == 'ok'
            ws.send_json({'v': 1, 'type': 'ping'})
            assert ws.receive_json()['type'] == 'pong'
            ws.send_json({**query, 'request_id': 'busy'})
            assert _receive_command_result(ws)['error'] == 'history_query_busy'
            assert _receive_command_result(ws)['error'] == 'history_query_timeout'
            # Timeout must not release admission while the underlying read runs.
            ws.send_json({**query, 'request_id': 'still-busy'})
            assert _receive_command_result(ws)['error'] == 'history_query_busy'
            release.set()
            ws.send_json({'v': 1, 'type': 'ping'})
            assert ws.receive_json()['type'] == 'pong'
    finally:
        release.set()


def test_history_query_failure_does_not_close_socket(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, '_realtime_history_query', AsyncMock(side_effect=RuntimeError('db unavailable')))
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
            'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
        assert ws.receive_json()['type'] == 'subscribed'
        assert ws.receive_json()['type'] == 'snapshot'
        ws.send_json({'v': 1, 'type': 'history.page', 'request_id': 'failure', 'payload': {}})
        assert _receive_command_result(ws) == {'ok': False, 'error': 'history_query_failed'}
        ws.send_json({'v': 1, 'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'


@pytest.mark.asyncio
async def test_history_timeout_discards_late_result_and_disconnect_cancels_worker(monkeypatch):
    release = asyncio.Event()
    stopped = asyncio.Event()
    async def read(*args):
        try:
            await release.wait()
            return {'ok': True, 'items': ['late']}
        finally:
            stopped.set()
    send = AsyncMock()
    monkeypatch.setattr(server, '_realtime_history_query', read)
    monkeypatch.setattr(server, '_realtime_send', send)
    monkeypatch.setattr(server, '_REALTIME_HISTORY_QUERY_TIMEOUT', .01)
    frame = {'type': 'history.page', 'request_id': 'late', 'payload': {}}
    task = asyncio.create_task(server._run_history_query(frame, None, 'local:test', 0))
    for _ in range(100):
        if send.call_count:
            break
        await asyncio.sleep(.005)
    assert send.call_count == 1
    assert send.call_args.args[1]['payload']['error'] == 'history_query_timeout'
    assert not task.done()
    release.set()
    await task
    assert send.call_count == 1  # No late success after timeout.
    release.clear()
    stopped.clear()
    task = asyncio.create_task(server._run_history_query(frame, None, 'local:test', 0))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(stopped.wait(), 1)


def test_query_completion_keeps_inflight_receive(tmp_path, monkeypatch):
    """Deliver ping after wait's done snapshot, before pending-task cleanup."""
    _fresh_db(tmp_path, monkeypatch)
    original_wait = asyncio.wait
    boundary = threading.Event()
    delivered = threading.Event()
    async def wait_at_boundary(tasks, **kwargs):
        done, pending = await original_wait(tasks, **kwargs)
        if any(getattr(task.get_coro(), '__name__', '') == '_run_history_query' for task in done):
            boundary.set()
            for _ in range(1000):
                receive = next((task for task in pending
                                if getattr(task.get_coro(), '__name__', '') == 'receive_text'), None)
                if receive is not None and receive.done():
                    delivered.set()
                    break
                await asyncio.sleep(.001)
        return done, pending
    monkeypatch.setattr(server.asyncio, 'wait', wait_at_boundary)
    monkeypatch.setattr(server, '_realtime_history_query', AsyncMock(return_value={'ok': True, 'items': []}))
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        ws.receive_json()
        ws.send_json({'v': 1, 'type': 'subscribe', 'payload': {
            'client_id': CLIENT_ID, 'scopes': [{'lifecycle': 'global'}]}})
        assert ws.receive_json()['type'] == 'subscribed'
        assert ws.receive_json()['type'] == 'snapshot'
        ws.send_json({'v': 1, 'type': 'history.page', 'request_id': 'boundary', 'payload': {}})
        assert _receive_command_result(ws)['ok']
        assert boundary.wait(2)
        ws.send_json({'v': 1, 'type': 'ping'})
        assert delivered.wait(2)
        # A second ping bounds the regression: old code drops the first and
        # answers only the second, so the health result arrives before pong #2.
        ws.send_json({'v': 1, 'type': 'ping'})
        ws.send_json({'v': 1, 'type': 'health.check', 'request_id': 'after', 'payload': {}})
        assert ws.receive_json()['type'] == 'pong'
        assert ws.receive_json()['type'] == 'pong'
        assert _receive_command_result(ws)['status'] == 'ok'


def test_query_completion_keeps_inflight_auth_output(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    original_wait = asyncio.wait
    queues = []
    delivered = threading.Event()
    async def attach(*args):
        queue = asyncio.Queue()
        queues.append(queue)
        return 'test-session', queue
    async def wait_at_boundary(tasks, **kwargs):
        done, pending = await original_wait(tasks, **kwargs)
        if any(getattr(task.get_coro(), '__name__', '') == '_run_history_query' for task in done):
            getter = next((task for task in pending
                           if getattr(task.get_coro(), '__name__', '') == 'get'), None)
            assert getter is not None
            queues[0].put_nowait(b'boundary-output')
            await original_wait({getter})
            delivered.set()
        return done, pending
    monkeypatch.setattr(server.asyncio, 'wait', wait_at_boundary)
    monkeypatch.setattr(server, '_handle_realtime_mutation', attach)
    monkeypatch.setattr(server, '_realtime_history_query', AsyncMock(return_value={'ok': True, 'items': []}))
    with PairedBrowserClient(server.app).websocket_connect('/ws/v1') as ws:
        _subscribe_auth(ws)
        ws.send_json({'v': 1, 'type': 'auth.start', 'request_id': 'attach', 'payload': {}})
        ws.send_json({'v': 1, 'type': 'history.page', 'request_id': 'boundary', 'payload': {}})
        assert _receive_command_result(ws)['ok']
        assert delivered.wait(2)
        ws.send_json({'v': 1, 'type': 'ping'})
        frame = ws.receive_json()
        assert frame['type'] == 'auth.output'
        assert base64.b64decode(frame['payload']['data']) == b'boundary-output'
        assert ws.receive_json()['type'] == 'pong'
