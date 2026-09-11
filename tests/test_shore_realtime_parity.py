"""Milestone 4.7: transport-parity test harness.

Proves the acceptance-gate claim "identical scenarios over /ws/v1 and Shore
produce equivalent normalized state" by driving the same fixed scenario
(subscribe -> snapshot -> N published events across replayable types -> ack)
through both a direct `/ws/v1` TestClient and a `ShoreChannel` in-process, and
asserting the resulting frames -- and, separately, which catch-up mode
(replay vs. snapshot, per `_realtime_replay_rollover_reason`) each side chose
-- match. Both sides read and write the same `stats_db` (as they do in
production: agent/shore_transport.py calls straight into agent/server.py's
`_realtime_catchup`/`_realtime_snapshot`, not a parallel copy), so this is not
two independently-seeded databases compared after the fact -- it is one
shared event log observed through two transports. See
docs/plans/adr-0039-shore-remote-access.md Milestone 4.7.
"""

import asyncio
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from fastapi.testclient import TestClient

from agent import server, stats_db
from agent.shore_crypto import ReplayStore, ShoreProtocolError, canonical
from agent.shore_transport import ShoreChannel, ShoreHostConnection

from tests.test_shore_transport import ACCOUNT, HOST, NOW, browser_frame, open_response, pair

DIRECT_CLIENT_ID = "paritydirectclient00000001"
GLOBAL_SCOPE = [{"lifecycle": "global"}]


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(value)


def _shore_setup(tmp_path):
    """Pair one Shore device against the shared stats_db and return helpers
    that send an ADR-0040 frame (`send`) or run a push sweep (`push`), each
    returning the *opened* (envelope-stripped) frame(s) -- directly comparable
    to what `ws.receive_json()` returns on the direct path."""
    host_signing = ed25519.Ed25519PrivateKey.generate()
    host_agreement = x25519.X25519PrivateKey.generate()
    browser_signing = ed25519.Ed25519PrivateKey.generate()
    browser_agreement = x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path / "shore-state", account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    asyncio.run(pair(channel, browser_signing, browser_agreement))
    replay = ReplayStore(tmp_path / "shore-state" / "browser-replay.db")
    connection = ShoreHostConnection(channel, relay="http://127.0.0.1:8787", username="alice",
        host_id=HOST, signing_key=host_signing)
    seq = {"n": 0}

    def send(kind, payload):
        seq["n"] += 1
        request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(),
                                 seq["n"], kind, payload)
        sealed = asyncio.run(channel.handle(canonical(request), now_ms=NOW))
        return [open_response(frame, host_signing.public_key(), browser_agreement,
                               host_agreement.public_key(), replay, now_ms=NOW) for frame in sealed]

    def push():
        socket = _RecordingSocket()
        asyncio.run(connection._push_sweep(socket))
        return [open_response(frame, host_signing.public_key(), browser_agreement,
                               host_agreement.public_key(), replay, now_ms=None) for frame in socket.sent]

    return channel, connection, send, push


def _direct_hello_and_subscribe(ws, *, cursor=None):
    assert ws.receive_json()["type"] == "hello"
    payload = {"client_id": DIRECT_CLIENT_ID, "scopes": GLOBAL_SCOPE}
    if cursor is not None:
        payload["cursor"] = cursor
    ws.send_json({"v": 1, "type": "subscribe", "payload": payload})
    subscribed = ws.receive_json()
    assert subscribed["type"] == "subscribed"
    return subscribed


def test_transport_parity_subscribe_snapshot_events_and_ack(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    _channel, _connection, shore_send, shore_push = _shore_setup(tmp_path)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        subscribed_direct = _direct_hello_and_subscribe(ws)
        snapshot_direct = ws.receive_json()
        assert snapshot_direct["type"] == "snapshot"

        subscribed_shore, snapshot_shore = shore_send("subscribe", {"scopes": GLOBAL_SCOPE})

        # Same shared db, same instant -> both chose the fresh-snapshot catch-up
        # mode and computed byte-identical content from it.
        assert subscribed_shore == subscribed_direct
        assert snapshot_shore == snapshot_direct
        assert snapshot_shore["payload"]["cursor_reset"] is True

        # N published events across replayable types (chat.text, message.changed).
        stats_db.insert_run_event(msg_id, 0, "text", "live one")
        stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": msg_id})

        direct_events = [ws.receive_json(), ws.receive_json()]
        shore_events = shore_push()
        assert [event["type"] for event in direct_events] == ["chat.text", "message.changed"]
        assert direct_events == shore_events

        # ack: fire-and-forget on both sides, no reply frame either way.
        last_event_id = direct_events[-1]["event_id"]
        ws.send_json({"v": 1, "type": "ack", "payload": {"event_id": last_event_id}})
        ws.send_json({"v": 1, "type": "ping", "payload": {}})
        assert ws.receive_json()["type"] == "pong"
        assert shore_send("ack", {"event_id": last_event_id}) == []


def test_transport_parity_replay_gap_rollover_chooses_snapshot_mode_on_both(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for index in range(5):
        stats_db.insert_realtime_event("message.changed", "squid", "codex", {"id": index})
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("DELETE FROM realtime_events WHERE event_id < 4")

    _channel, _connection, shore_send, _shore_push = _shore_setup(tmp_path)

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _direct_hello_and_subscribe(ws, cursor=1)
        catchup_direct = ws.receive_json()

    subscribed_shore, catchup_shore = shore_send("subscribe", {"scopes": GLOBAL_SCOPE, "cursor": 1})

    # A pruned/incontinuous cursor forces "replay_gap" -> both sides roll over
    # to a snapshot rather than replaying individual events, and it's not the
    # future_cursor variant, so cursor_reset stays False on both.
    assert catchup_direct["type"] == catchup_shore["type"] == "snapshot"
    assert catchup_direct["payload"]["cursor_reset"] is False
    assert catchup_shore["payload"]["cursor_reset"] is False
    assert catchup_direct["payload"] == catchup_shore["payload"]


def test_shore_capability_denial_is_stricter_than_direct_path_by_design(tmp_path, monkeypatch):
    """4.2's registry only grants `dashboard.read.v1` (subscribe/unsubscribe/
    ack/ping/pong) by default, so `chat.cancel` -- a real ADR-0040 type the
    direct path dispatches into the shared core -- is denied by Shore
    pre-dispatch. This is the intentional, narrower Shore surface documented
    in 4.2's open question 2, not a parity bug: prove it denies before
    `_dispatch_adr0040` runs (no side effect), while the identical command
    over /ws/v1 reaches real dispatch and mutates state.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "kill_proc_by_msg_id", lambda _msg_id: 0)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    channel, _connection, shore_send, _shore_push = _shore_setup(tmp_path)
    shore_send("subscribe", {"scopes": GLOBAL_SCOPE})

    dispatched = []
    original_dispatch = channel._dispatch_adr0040

    async def spying_dispatch(*args, **kwargs):
        dispatched.append(True)
        return await original_dispatch(*args, **kwargs)

    monkeypatch.setattr(channel, "_dispatch_adr0040", spying_dispatch)

    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        shore_send("chat.cancel", {"msg_id": msg_id})
    assert dispatched == []
    assert stats_db.get_message(msg_id)["status"] != "cancelled"

    with TestClient(server.app).websocket_connect("/ws/v1") as ws:
        _direct_hello_and_subscribe(ws)
        ws.receive_json()  # snapshot
        ws.send_json({
            "v": 1, "type": "chat.cancel", "request_id": "cancel-1",
            "payload": {"msg_id": msg_id},
        })
        result = ws.receive_json()
        while result["type"] != "command.result":
            result = ws.receive_json()
        assert result["payload"]["ok"] is True
    assert stats_db.get_message(msg_id)["status"] == "cancelled"
