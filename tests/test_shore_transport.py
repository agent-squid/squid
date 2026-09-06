import asyncio
import json
import sqlite3
import stat
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.shore_crypto import (
    ReplayStore, ShoreProtocolError, b64url, canonical, crockford32_decode,
    derive_pair_bootstrap_key, derive_pair_key, fingerprint, open_envelope,
    pairing_finished, seal_envelope, unb64url,
)
from agent.shore_transport import ShoreChannel, ShoreHostConnection, configured_host_connection
from agent.shore import ShoreRuntimeConfig, _new_identity, _write_runtime_config

ACCOUNT = "018f1f25-3f6b-7d75-a4d1-62d771381b20"
HOST = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
DEVICE = "018f1f25-8614-7e41-8c5c-fc0b6eefad62"
CEREMONY = "018f1f25-c930-76f0-86e7-cb06d94e6a32"
NOW = int(datetime(2026, 9, 3, 12, tzinfo=timezone.utc).timestamp() * 1000)


def test_configured_host_connection_loads_persisted_login(tmp_path):
    identity = tmp_path / "shore"
    host_id, _, _ = _new_identity(identity)
    _write_runtime_config(identity, ShoreRuntimeConfig(
        "https://broker.example", "alice", ACCOUNT, 3,
    ))
    connection = configured_host_connection(identity)
    assert connection is not None
    assert connection.host_id == host_id
    assert connection.channel.account_id == ACCOUNT
    assert connection.channel.key_epoch == 3
    assert connection.relay_url == f"wss://broker.example/@alice/relay?account_id={ACCOUNT}"


def test_configured_host_connection_is_disabled_before_login(tmp_path):
    assert configured_host_connection(tmp_path / "shore") is None


def test_host_connection_allows_plaintext_only_for_loopback(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())
    local = ShoreHostConnection(channel, broker="http://127.0.0.1:8787", username="alice",
        host_id=HOST, signing_key=host_signing)
    assert local.relay_url == f"ws://127.0.0.1:8787/@alice/relay?account_id={ACCOUNT}"
    with pytest.raises(ValueError, match="HTTPS"):
        ShoreHostConnection(channel, broker="http://broker.example", username="alice",
            host_id=HOST, signing_key=host_signing)


def timestamp(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def pair(channel, browser_signing, browser_agreement):
    started = channel.begin_pairing(CEREMONY)
    offer = started["offer"]
    binding = {"v": 1, "account_id": ACCOUNT, "host_id": HOST, "device_id": DEVICE,
        "ceremony_nonce": offer["ceremony_nonce"],
        "host_sign_fingerprint": offer["host_sign_fingerprint"],
        "host_enc_fingerprint": offer["host_enc_fingerprint"],
        "browser_sign_fingerprint": fingerprint(browser_signing.public_key()),
        "browser_enc_fingerprint": fingerprint(browser_agreement.public_key())}
    binding_bytes = canonical(binding)
    secret, nonce = crockford32_decode(started["code"]), unb64url(offer["ceremony_nonce"])
    key = derive_pair_key(secret, nonce, binding_bytes)
    plaintext = {"v": 1, "binding": binding,
        "browser_keys": {"signing": b64url(browser_signing.public_key().public_bytes_raw()),
                         "agreement": b64url(browser_agreement.public_key().public_bytes_raw())},
        "finished": b64url(pairing_finished(key, "browser", binding_bytes))}
    packet = {"v": 1, "ceremony_id": CEREMONY, "direction": "browser_to_host", "nonce": b64url(bytes(12))}
    packet["ciphertext"] = b64url(AESGCM(derive_pair_bootstrap_key(secret, nonce)).encrypt(bytes(12), canonical(plaintext), canonical(packet)))
    assert json.loads(channel.handle(canonical(packet)))["direction"] == "host_to_browser"
    confirmation = {"v": 1, "ceremony_id": CEREMONY, "direction": "browser_to_host", "nonce": b64url(bytes(range(12, 24)))}
    confirmed = {"v": 1, "binding": binding, "finished": b64url(pairing_finished(key, "browser-confirmed", binding_bytes))}
    confirmation["ciphertext"] = b64url(AESGCM(key).encrypt(bytes(range(12, 24)), canonical(confirmed), canonical(confirmation)))
    assert channel.handle(canonical(confirmation)) is None


def test_live_channel_pairs_persists_trust_and_round_trips_only_probe(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)
    pair(channel, browser_signing, browser_agreement)

    def request(sequence, kind="shore.probe"):
        return seal_envelope({"v": 1, "type": kind, "payload": {"nonce": "round-trip"}},
            account_id=ACCOUNT, host_id=HOST, device_id=DEVICE, key_epoch=1,
            direction="browser_to_host", seq=sequence,
            request_id=f"018f1f25-c930-76f0-86e7-{sequence:012x}",
            issued_at=timestamp(NOW), expires_at=timestamp(NOW + 30_000),
            sender_signing=browser_signing, sender_agreement=browser_agreement,
            receiver_agreement=host_agreement.public_key())

    response = json.loads(channel.handle(canonical(request(1)), now_ms=NOW))
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "outbound.sqlite3").stat().st_mode) == 0o600
    opened = open_envelope(response,
        expected={"account_id": ACCOUNT, "host_id": HOST, "device_id": DEVICE,
                  "key_epoch": 1, "direction": "host_to_browser"},
        sender_signing=host_signing.public_key(), receiver_agreement=browser_agreement,
        sender_agreement=host_agreement.public_key(), replay=ReplayStore(tmp_path / "browser-replay.db"), now_ms=NOW)
    assert opened == {"v": 1, "type": "shore.probe.result", "payload": {"nonce": "round-trip"}}

    restarted = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)
    assert json.loads(restarted.handle(canonical(request(2)), now_ms=NOW))["seq"] == "2"
    with pytest.raises(ShoreProtocolError, match="shore_unsupported_frame"):
        restarted.handle(canonical(request(3, "subscribe")), now_ms=NOW)
    with pytest.raises(ShoreProtocolError, match="shore_replay"):
        restarted.handle(canonical(request(2)), now_ms=NOW)

    with sqlite3.connect(tmp_path / "outbound.sqlite3") as connection:
        connection.execute("UPDATE sequences SET value=?", (1 << 32,))
    with pytest.raises(ShoreProtocolError, match="shore_sequence_exhausted"):
        restarted.handle(canonical(request(4)), now_ms=NOW)
    with sqlite3.connect(tmp_path / "outbound.sqlite3") as connection:
        assert connection.execute("SELECT value FROM sequences").fetchone()[0] == 1 << 32


def test_broker_injected_frames_fail_before_application_dispatch(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    host_agreement = x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)

    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        channel.handle(b"broker-controlled plaintext", now_ms=NOW)

    untrusted_signing = ed25519.Ed25519PrivateKey.generate()
    untrusted_agreement = x25519.X25519PrivateKey.generate()
    injected = seal_envelope({"v": 1, "type": "shore.probe", "payload": {"nonce": "injected"}},
        account_id=ACCOUNT, host_id=HOST,
        device_id="018f1f25-8614-7e41-8c5c-fc0b6eefad63", key_epoch=1,
        direction="browser_to_host", seq=1,
        request_id="018f1f25-c930-76f0-86e7-000000000098",
        issued_at=timestamp(NOW), expires_at=timestamp(NOW + 30_000),
        sender_signing=untrusted_signing, sender_agreement=untrusted_agreement,
        receiver_agreement=host_agreement.public_key())
    with pytest.raises(ShoreProtocolError, match="shore_untrusted_device"):
        channel.handle(canonical(injected), now_ms=NOW)


def test_host_key_epoch_change_does_not_inherit_old_device_trust(tmp_path):
    old_host_signing, old_host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    old_channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=old_host_signing, host_agreement=old_host_agreement, key_epoch=1)
    pair(old_channel, browser_signing, browser_agreement)

    new_host_signing, new_host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    rotated = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=new_host_signing, host_agreement=new_host_agreement, key_epoch=2)
    envelope = seal_envelope({"v": 1, "type": "shore.probe", "payload": {"nonce": "old-trust"}},
        account_id=ACCOUNT, host_id=HOST, device_id=DEVICE, key_epoch=2,
        direction="browser_to_host", seq=1,
        request_id="018f1f25-c930-76f0-86e7-000000000099",
        issued_at=timestamp(NOW), expires_at=timestamp(NOW + 30_000),
        sender_signing=browser_signing, sender_agreement=browser_agreement,
        receiver_agreement=new_host_agreement.public_key())
    with pytest.raises(ShoreProtocolError, match="shore_untrusted_device"):
        rotated.handle(canonical(envelope), now_ms=NOW)


@pytest.mark.asyncio
async def test_host_connection_signs_challenge_heartbeats_and_dispatches(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    handled = []
    channel.handle = lambda payload: handled.append(payload) or b"response"

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"id": CEREMONY, "nonce": "challenge-nonce"}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, url, json):
            assert url.endswith("/@alice/host/connect-challenge")
            assert json == {"hostId": HOST}
            return Response()

    monkeypatch.setattr("agent.shore_transport.httpx.AsyncClient", Client)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, heartbeat_seconds=0.01)
    headers = await connection._connection_headers()
    proof = canonical({"challenge_id": CEREMONY, "host_id": HOST,
        "nonce": "challenge-nonce", "purpose": "websocket", "v": 1})
    from agent.shore_crypto import unb64url
    host_signing.public_key().verify(unb64url(headers["x-shore-signature"]), proof)

    class Socket:
        def __init__(self): self.sent = []; self.receives = 0
        async def recv(self):
            self.receives += 1
            if self.receives == 1:
                await asyncio.sleep(0.02)
                return b"request"
            raise asyncio.CancelledError
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    with pytest.raises(asyncio.CancelledError):
        await connection._serve(socket, asyncio.Event())
    assert socket.sent[-1] == b"response"
    assert socket.sent[:-1] and all(frame == b"" for frame in socket.sent[:-1])
    assert handled == [b"request"]


@pytest.mark.asyncio
async def test_host_connection_retries_with_fresh_challenges_bounded_backoff_and_stops(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, base_backoff=1, max_backoff=3)
    challenges, attempts, delays = [], 0, []

    async def headers():
        challenges.append(len(challenges) + 1)
        return {"x-shore-challenge-id": str(challenges[-1])}

    class Context:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                from websockets.exceptions import WebSocketException
                raise WebSocketException("offline")
            return object()
        async def __aexit__(self, *_args): pass

    def connect(*_args, **_kwargs): return Context()

    async def serve(_socket, stop): stop.set()

    original_wait_for = asyncio.wait_for
    async def wait_for(awaitable, *, timeout):
        awaitable.close()
        delays.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr(connection, "_serve", serve)
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    monkeypatch.setattr("agent.shore_transport.random.uniform", lambda *_args: 0)
    monkeypatch.setattr("agent.shore_transport.asyncio.wait_for", wait_for)
    stop = asyncio.Event()
    await connection.run(stop)
    monkeypatch.setattr("agent.shore_transport.asyncio.wait_for", original_wait_for)
    assert challenges == [1, 2, 3, 4]
    assert delays == [1, 2, 3]
    assert stop.is_set()


@pytest.mark.asyncio
async def test_malformed_successful_challenge_is_retryable_protocol_error(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)

    class Response:
        def raise_for_status(self): pass
        def json(self): raise ValueError("not json")

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr("agent.shore_transport.httpx.AsyncClient", Client)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    with pytest.raises(ShoreProtocolError, match="shore_invalid_host_challenge"):
        await connection._connection_headers()


@pytest.mark.asyncio
async def test_post_handshake_failures_back_off_until_connection_is_stable(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, base_backoff=1, max_backoff=4,
        stable_seconds=60)
    attempts, delays = 0, []

    class Context:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_args): pass

    async def headers(): return {}
    async def serve(_socket, stop):
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            stop.set()
            return
        from websockets.exceptions import WebSocketException
        raise WebSocketException("dropped")
    async def wait_for(awaitable, *, timeout):
        awaitable.close()
        delays.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr(connection, "_serve", serve)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Context())
    monkeypatch.setattr("agent.shore_transport.random.uniform", lambda *_args: 0)
    monkeypatch.setattr("agent.shore_transport.asyncio.wait_for", wait_for)
    await connection.run(asyncio.Event())
    assert delays == [1, 2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("close_code", [1008, 1009])
async def test_policy_and_oversize_closes_are_not_retried(monkeypatch, tmp_path, close_code):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    attempts = 0

    class Context:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_args): pass

    async def headers(): return {}
    async def serve(_socket, _stop):
        nonlocal attempts
        attempts += 1
        raise ConnectionClosedError(Close(close_code, "rejected"), None)

    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr(connection, "_serve", serve)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Context())
    await connection.run(asyncio.Event())
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["socket_expired", "heartbeat_expired"])
async def test_routine_transport_expiry_reconnects(monkeypatch, tmp_path, reason):
    from websockets.exceptions import ConnectionClosedOK
    from websockets.frames import Close

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, base_backoff=0)
    attempts = 0

    class Context:
        async def __aenter__(self): return object()
        async def __aexit__(self, *_args): pass

    async def headers(): return {}
    async def serve(_socket, stop):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionClosedOK(Close(1001, reason), None)
        stop.set()

    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr(connection, "_serve", serve)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Context())
    await connection.run(asyncio.Event())
    assert attempts == 2


@pytest.mark.asyncio
async def test_inbound_invalid_frames_cannot_suppress_host_heartbeat(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, heartbeat_seconds=0.01)
    stop = asyncio.Event()

    class Socket:
        def __init__(self): self.sent = []
        async def recv(self):
            await asyncio.sleep(0.001)
            return b"not-canonical-json"
        async def send(self, value):
            self.sent.append(value)
            stop.set()

    socket = Socket()
    await connection._serve(socket, stop)
    assert socket.sent == [b""]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422, 426])
async def test_terminal_upgrade_statuses_are_not_retried(monkeypatch, tmp_path, status):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    attempts = 0

    class Context:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise InvalidStatus(Response(status, "rejected", Headers()))
        async def __aexit__(self, *_args): pass

    async def headers(): return {}
    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Context())
    await connection.run(asyncio.Event())
    assert attempts == 1


@pytest.mark.asyncio
async def test_transient_upgrade_status_is_retried(monkeypatch, tmp_path):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, base_backoff=0)
    attempts = 0

    class Context:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise InvalidStatus(Response(429, "rate limited", Headers()))
            return object()
        async def __aexit__(self, *_args): pass

    async def headers(): return {}
    async def serve(_socket, stop): stop.set()
    monkeypatch.setattr(connection, "_connection_headers", headers)
    monkeypatch.setattr(connection, "_serve", serve)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Context())
    await connection.run(asyncio.Event())
    assert attempts == 2


@pytest.mark.asyncio
async def test_terminal_challenge_http_status_is_not_retried(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    attempts = 0

    async def headers():
        nonlocal attempts
        attempts += 1
        request = httpx.Request("POST", connection.challenge_url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("host not found", request=request, response=response)

    monkeypatch.setattr(connection, "_connection_headers", headers)
    await connection.run(asyncio.Event())
    assert attempts == 1


def test_channel_wraps_pairing_status_list_devices_and_revoke(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)

    assert channel.pairing_status(CEREMONY) == {"status": "unknown"}
    assert channel.list_devices() == []

    pair(channel, browser_signing, browser_agreement)

    assert channel.pairing_status(CEREMONY) == {"status": "paired", "device_id": DEVICE}
    devices = channel.list_devices()
    assert [device.device_id for device in devices] == [DEVICE]
    assert devices[0].capabilities == ("dashboard.read.v1",)

    assert channel.revoke_device(DEVICE) is True
    assert channel.list_devices() == []
    assert channel.revoke_device(DEVICE) is False
