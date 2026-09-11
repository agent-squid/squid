import asyncio
import json
import sqlite3
import stat
import time as time_module
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
from agent import shore_transport as shore_transport_mod
from agent import shore_receipt as shore_receipt_mod
from agent.shore_receipt import ReceiptVerificationError
from agent.shore import ShoreRuntimeConfig, _new_identity, _write_runtime_config
from agent import server as server_mod
from agent import stats_db

ACCOUNT = "018f1f25-3f6b-7d75-a4d1-62d771381b20"
HOST = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
DEVICE = "018f1f25-8614-7e41-8c5c-fc0b6eefad62"
DEVICE2 = "018f1f25-8614-7e41-8c5c-fc0b6eefad64"
CEREMONY = "018f1f25-c930-76f0-86e7-cb06d94e6a32"
CEREMONY2 = "018f1f25-c930-76f0-86e7-cb06d94e6a34"
NOW = int(datetime(2026, 9, 3, 12, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _isolate_release_receipt_pins(monkeypatch):
    """Keep transport tests independent of the keys shipped by a release."""
    monkeypatch.setattr(shore_transport_mod, "PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN", {})


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


def test_non_loopback_broker_ignores_environment_receipt_trust_root(tmp_path, monkeypatch):
    injected = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    monkeypatch.setenv("SHORE_RECEIPT_PUBLIC_KEYS", json.dumps({"1": b64url(injected)}))
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())
    production = ShoreHostConnection(channel, broker="https://agentsquid.ai", username="alice",
        host_id=HOST, signing_key=host_signing)
    development = ShoreHostConnection(channel, broker="http://127.0.0.1:8787", username="alice",
        host_id=HOST, signing_key=host_signing)
    assert production.receipt_keys == {}
    assert set(development.receipt_keys) == {1}


def test_release_contains_independent_dev_and_production_receipt_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shore_transport_mod, "PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN",
        shore_receipt_mod.PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN,
    )
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())
    development = ShoreHostConnection(channel, broker="https://dev.agentsquid.ai",
        username="alice", host_id=HOST, signing_key=host_signing)
    production = ShoreHostConnection(channel, broker="https://agentsquid.ai",
        username="alice", host_id=HOST, signing_key=host_signing)

    assert set(development.receipt_keys) == {1}
    assert set(production.receipt_keys) == {1}
    assert development.receipt_keys[1].public_bytes_raw() != production.receipt_keys[1].public_bytes_raw()


def test_release_receipt_keys_are_scoped_to_canonical_broker_origin(tmp_path, monkeypatch):
    dev = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    prod = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    monkeypatch.setattr(shore_transport_mod, "PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN", {
        "https://preprod.agentsquid.ai": {1: b64url(dev)},
        "https://agentsquid.ai": {2: b64url(prod)},
    })
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())

    development = ShoreHostConnection(channel,
        broker="https://PREPROD.agentsquid.ai:443/base", username="alice",
        host_id=HOST, signing_key=host_signing)
    production = ShoreHostConnection(channel, broker="https://agentsquid.ai", username="alice",
        host_id=HOST, signing_key=host_signing)
    assert development.receipt_keys[1].public_bytes_raw() == dev
    assert set(development.receipt_keys) == {1}
    assert production.receipt_keys[2].public_bytes_raw() == prod
    assert set(production.receipt_keys) == {2}
    with pytest.raises(ValueError, match="release-pinned Shore receipt keys"):
        ShoreHostConnection(channel, broker="https://other.example", username="alice",
            host_id=HOST, signing_key=host_signing)


@pytest.mark.asyncio
async def test_host_audit_batch_cursor_advances_only_after_verified_ack(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())
    channel.audit.record(request_id=CEREMONY, device_id=DEVICE, message_type="ping",
        frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    shore_signing = ed25519.Ed25519PrivateKey.generate()
    connection.receipt_keys = {1: shore_signing.public_key()}

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    assert await connection._send_audit_batch(socket)
    wrapper = json.loads(socket.sent[0])
    assert set(wrapper) == {"v", "type", "body"} and wrapper["type"] == "host_audit_batch"
    batch = connection._pending_audit_batch
    document = json.loads(batch.body)
    fields = {"v": 1, "type": "host_audit_batch_ack", "hostId": HOST,
        "throughSeq": batch.through_seq, "headHash": batch.through_hash,
        "payloadHash": document["manifest"]["payloadHash"], "receiptEpoch": 1}
    ack = {**fields, "signature": b64url(shore_signing.sign(canonical(fields)))}
    await connection._accept_audit_batch_ack(ack)
    assert connection._pending_audit_batch is None
    assert channel.audit.pending_export() is None


@pytest.mark.asyncio
async def test_host_audit_batch_rejects_forged_ack_without_advancing_cursor(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=x25519.X25519PrivateKey.generate())
    channel.audit.record(request_id=CEREMONY, device_id=DEVICE, message_type="ping",
        frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)
    trusted = ed25519.Ed25519PrivateKey.generate()
    connection.receipt_keys = {1: trusted.public_key()}

    class Socket:
        async def send(self, _value): pass

    await connection._send_audit_batch(Socket())
    batch = connection._pending_audit_batch
    manifest = json.loads(batch.body)["manifest"]
    fields = {"v": 1, "type": "host_audit_batch_ack", "hostId": HOST,
        "throughSeq": batch.through_seq, "headHash": batch.through_hash,
        "payloadHash": manifest["payloadHash"], "receiptEpoch": 1}
    forged = {**fields, "signature": b64url(ed25519.Ed25519PrivateKey.generate().sign(canonical(fields)))}
    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        await connection._accept_audit_batch_ack(forged)
    assert channel.audit.pending_export() == batch


def timestamp(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def browser_frame(browser_signing, browser_agreement, host_agreement_public, sequence, kind, payload, device_id=DEVICE):
    """Seal one browser_to_host ADR-0040 frame for the fixed ACCOUNT/HOST.

    request_id is globally unique (ReplayStore's dedup table has no device
    column), so it's derived from both device_id and sequence -- reusing the
    plain sequence alone would collide across two devices sharing a sequence
    number in the same test.
    """
    device_suffix = device_id.replace("-", "")[-4:]
    return seal_envelope({"v": 1, "type": kind, "payload": payload},
        account_id=ACCOUNT, host_id=HOST, device_id=device_id, key_epoch=1,
        direction="browser_to_host", seq=sequence,
        request_id=f"018f1f25-c930-76f0-86e7-{device_suffix}{sequence:08x}",
        issued_at=timestamp(NOW), expires_at=timestamp(NOW + 30_000),
        sender_signing=browser_signing, sender_agreement=browser_agreement,
        receiver_agreement=host_agreement_public)


def live_browser_frame(browser_signing, browser_agreement, host_agreement_public, sequence, kind, payload, device_id=DEVICE):
    """Like `browser_frame`, but stamped with the real wall clock.

    `ShoreHostConnection._serve` calls `channel.handle(message)` with no
    `now_ms`, so `open_envelope` validates expiry against real time, not the
    fixed historical `NOW` the rest of this file pins for direct
    `channel.handle(..., now_ms=NOW)` calls -- a frame built with `NOW` would
    look expired by the time a `_serve`-driven test actually runs.
    """
    now_ms = int(time_module.time() * 1000)
    device_suffix = device_id.replace("-", "")[-4:]
    return seal_envelope({"v": 1, "type": kind, "payload": payload},
        account_id=ACCOUNT, host_id=HOST, device_id=device_id, key_epoch=1,
        direction="browser_to_host", seq=sequence,
        request_id=f"018f1f25-c930-76f0-86e7-{device_suffix}{sequence:08x}",
        issued_at=timestamp(now_ms), expires_at=timestamp(now_ms + 30_000),
        sender_signing=browser_signing, sender_agreement=browser_agreement,
        receiver_agreement=host_agreement_public)


def open_response(response_bytes, host_signing_public, browser_agreement, host_agreement_public, replay, device_id=DEVICE, now_ms=NOW):
    """Decrypt one host_to_browser envelope for the fixed ACCOUNT/HOST.

    `now_ms=None` for envelopes sealed by ShoreHostConnection._push_sweep,
    which stamps its own frames with the real wall clock (there's no pinned
    `now_ms` on that path, unlike channel.handle's), not the fixed NOW used
    to pin inbound-triggered responses in these tests.
    """
    return open_envelope(json.loads(response_bytes),
        expected={"account_id": ACCOUNT, "host_id": HOST, "device_id": device_id,
                  "key_epoch": 1, "direction": "host_to_browser"},
        sender_signing=host_signing_public, receiver_agreement=browser_agreement,
        sender_agreement=host_agreement_public, replay=replay, now_ms=now_ms)


async def pair(channel, browser_signing, browser_agreement, device_id=DEVICE, ceremony_id=CEREMONY):
    started = channel.begin_pairing(ceremony_id)
    offer = started["offer"]
    binding = {"v": 1, "account_id": ACCOUNT, "host_id": HOST, "device_id": device_id,
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
    packet = {"v": 1, "ceremony_id": ceremony_id, "direction": "browser_to_host", "nonce": b64url(bytes(12))}
    packet["ciphertext"] = b64url(AESGCM(derive_pair_bootstrap_key(secret, nonce)).encrypt(bytes(12), canonical(plaintext), canonical(packet)))
    responses = await channel.handle(canonical(packet))
    assert json.loads(responses[0])["direction"] == "host_to_browser"
    confirmation = {"v": 1, "ceremony_id": ceremony_id, "direction": "browser_to_host", "nonce": b64url(bytes(range(12, 24)))}
    confirmed = {"v": 1, "binding": binding, "finished": b64url(pairing_finished(key, "browser-confirmed", binding_bytes))}
    confirmation["ciphertext"] = b64url(AESGCM(key).encrypt(bytes(range(12, 24)), canonical(confirmed), canonical(confirmation)))
    assert await channel.handle(canonical(confirmation)) == []


@pytest.mark.asyncio
async def test_live_channel_pairs_persists_trust_and_probe_round_trips(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)

    def request(sequence, kind="shore.probe", payload=None):
        return seal_envelope({"v": 1, "type": kind, "payload": payload if payload is not None else {"nonce": "round-trip"}},
            account_id=ACCOUNT, host_id=HOST, device_id=DEVICE, key_epoch=1,
            direction="browser_to_host", seq=sequence,
            request_id=f"018f1f25-c930-76f0-86e7-{sequence:012x}",
            issued_at=timestamp(NOW), expires_at=timestamp(NOW + 30_000),
            sender_signing=browser_signing, sender_agreement=browser_agreement,
            receiver_agreement=host_agreement.public_key())

    responses = await channel.handle(canonical(request(1)), now_ms=NOW)
    response = json.loads(responses[0])
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "outbound.sqlite3").stat().st_mode) == 0o600
    opened = open_envelope(response,
        expected={"account_id": ACCOUNT, "host_id": HOST, "device_id": DEVICE,
                  "key_epoch": 1, "direction": "host_to_browser"},
        sender_signing=host_signing.public_key(), receiver_agreement=browser_agreement,
        sender_agreement=host_agreement.public_key(), replay=ReplayStore(tmp_path / "browser-replay.db"), now_ms=NOW)
    assert opened == {"v": 1, "type": "shore.probe.result", "payload": {"nonce": "round-trip"}}
    probe_audit = channel.audit.events()
    assert [(event["messageType"], event["decision"], event["outcome"]) for event in probe_audit] == [
        ("shore.probe", "protocol", "ok"),
    ]

    restarted = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)
    second = await restarted.handle(canonical(request(2)), now_ms=NOW)
    assert json.loads(second[0])["seq"] == "2"
    # A real ADR-0040 type this device isn't granted (the default capability
    # is dashboard.read.v1 only) fails closed with the capability-registry's
    # own error, not the retired Milestone-3 "only probe" framing.
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        await restarted.handle(canonical(request(3, "chat.start", payload={})), now_ms=NOW)
    # A type that isn't real ADR-0040 at all fails closed distinctly.
    with pytest.raises(ShoreProtocolError, match="shore_unsupported_type"):
        await restarted.handle(canonical(request(4, "not-a-real-type", payload={})), now_ms=NOW)
    with pytest.raises(ShoreProtocolError, match="shore_replay"):
        await restarted.handle(canonical(request(2)), now_ms=NOW)

    with sqlite3.connect(tmp_path / "outbound.sqlite3") as connection:
        connection.execute("UPDATE sequences SET value=?", (1 << 32,))
    with pytest.raises(ShoreProtocolError, match="shore_sequence_exhausted"):
        await restarted.handle(canonical(request(5)), now_ms=NOW)
    with sqlite3.connect(tmp_path / "outbound.sqlite3") as connection:
        assert connection.execute("SELECT value FROM sequences").fetchone()[0] == 1 << 32


@pytest.mark.asyncio
async def test_broker_injected_frames_fail_before_application_dispatch(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    host_agreement = x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)

    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        await channel.handle(b"broker-controlled plaintext", now_ms=NOW)

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
        await channel.handle(canonical(injected), now_ms=NOW)


@pytest.mark.asyncio
async def test_host_key_epoch_change_does_not_inherit_old_device_trust(tmp_path):
    old_host_signing, old_host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    old_channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=old_host_signing, host_agreement=old_host_agreement, key_epoch=1)
    await pair(old_channel, browser_signing, browser_agreement)

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
        await rotated.handle(canonical(envelope), now_ms=NOW)


@pytest.mark.asyncio
async def test_host_connection_signs_challenge_heartbeats_and_dispatches(monkeypatch, tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    handled = []

    async def fake_handle(payload, **_kwargs):
        handled.append(payload)
        return [b"response"]

    channel.handle = fake_handle

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
async def test_receipt_mode_preserves_pairing_packets_and_broker_heartbeats(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(
        tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing,
        host_agreement=x25519.X25519PrivateKey.generate(),
    )
    connection = ShoreHostConnection(
        channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing,
    )
    connection.receipt_keys = {1: ed25519.Ed25519PrivateKey.generate().public_key()}
    pairing_packet = canonical({
        "v": 1, "ceremony_id": CEREMONY, "direction": "browser_to_host",
        "nonce": "nonce", "ciphertext": "ciphertext",
    })
    handled = []

    async def handle(payload, **_kwargs):
        handled.append(payload)
        return [b"pairing-response"]

    channel.handle = handle

    class Socket:
        def __init__(self): self.receives = 0; self.sent = []
        async def recv(self):
            self.receives += 1
            if self.receives == 1: return b""
            if self.receives == 2: return pairing_packet
            raise asyncio.CancelledError
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    with pytest.raises(asyncio.CancelledError):
        await connection._serve(socket, asyncio.Event())
    assert handled == [pairing_packet]
    assert socket.sent == [b"pairing-response"]
    assert connection._pending_receipt_envelopes == {}


@pytest.mark.asyncio
async def test_receipt_mode_closes_on_unwrapped_ordinary_frame(tmp_path):
    host_signing = ed25519.Ed25519PrivateKey.generate()
    channel = ShoreChannel(
        tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing,
        host_agreement=x25519.X25519PrivateKey.generate(),
    )
    connection = ShoreHostConnection(
        channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing,
    )
    connection.receipt_keys = {1: ed25519.Ed25519PrivateKey.generate().public_key()}

    class Socket:
        def __init__(self): self.closed = None; self.receives = 0
        async def recv(self):
            self.receives += 1
            if self.receives == 1: return b"ordinary-envelope"
            await asyncio.Future()
        async def send(self, _value): pass
        async def close(self, **kwargs): self.closed = kwargs

    socket = Socket()
    await connection._serve(socket, asyncio.Event())
    assert socket.closed == {"code": 1008, "reason": "shore_audit_continuity_unavailable"}


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


@pytest.mark.asyncio
async def test_channel_wraps_pairing_status_list_devices_and_revoke(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST, host_signing=host_signing, host_agreement=host_agreement)

    assert channel.pairing_status(CEREMONY) == {"status": "unknown"}
    assert channel.list_devices() == []

    await pair(channel, browser_signing, browser_agreement)

    assert channel.pairing_status(CEREMONY) == {"status": "paired", "device_id": DEVICE}
    devices = channel.list_devices()
    assert [device.device_id for device in devices] == [DEVICE]
    assert devices[0].capabilities == ("dashboard.read.v1",)

    assert channel.revoke_device(DEVICE) is True
    assert channel.list_devices() == []
    assert channel.revoke_device(DEVICE) is False


def _fresh_stats_db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()


@pytest.mark.asyncio
async def test_subscribe_dispatches_through_capability_registry_into_shared_realtime_core(tmp_path, monkeypatch):
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}]})
    responses = await channel.handle(canonical(request), now_ms=NOW)
    assert len(responses) == 2
    subscribed = open_response(responses[0], host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay)
    assert subscribed == {"v": 1, "type": "subscribed", "payload": {"scopes": [{"lifecycle": "global"}]}}
    snapshot = open_response(responses[1], host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay)
    assert snapshot["type"] == "snapshot"
    assert snapshot["payload"]["cursor_reset"] is True
    assert channel.sessions[DEVICE].scopes == [{"lifecycle": "global"}]

    # A real ADR-0040 type this device isn't granted still fails closed,
    # proving the capability registry -- not a hardcoded type check -- gates
    # dispatch now that probe is no longer the only supported frame.
    denied = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 2, "chat.start", {})
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        await channel.handle(canonical(denied), now_ms=NOW)


@pytest.mark.asyncio
async def test_serve_survives_unexpected_dispatch_error_and_keeps_serving(tmp_path, monkeypatch):
    """Security review finding: `_dispatch_adr0040` calls into agent.server's
    reused local realtime core (`_realtime_snapshot`/`_realtime_catchup`),
    which was written for the fully-trusted local session and isn't
    guaranteed to fail closed with `ShoreProtocolError` for every
    internal-state edge. A raw exception from that core for one authorized
    frame must not tear down `_serve`'s multiplexed relay loop for every
    other paired device -- it must be dropped like a malformed peer frame,
    proven here by a later frame still getting served afterward.
    """
    _fresh_stats_db(tmp_path, monkeypatch)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    async def boom(_scopes):
        raise RuntimeError("unexpected core failure")
    monkeypatch.setattr(server_mod, "_realtime_snapshot", boom)

    # No cursor -> _dispatch_adr0040's subscribe branch calls the now-broken
    # _realtime_snapshot; a plain ping does not, so it must still succeed
    # afterward if the loop survived. _serve validates envelopes against real
    # time (it calls channel.handle with no now_ms), so these must be
    # stamped live, not with the fixed historical NOW other tests use.
    subscribe = live_browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                                    "subscribe", {"scopes": [{"lifecycle": "global"}]})
    ping = live_browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 2, "ping", {})
    inbound = [canonical(subscribe), canonical(ping)]

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing, heartbeat_seconds=100)
    stop = asyncio.Event()

    class Socket:
        def __init__(self): self.sent = []
        async def recv(self):
            await asyncio.sleep(0.001)
            if inbound:
                return inbound.pop(0)
            stop.set()
            return b"malformed"
        async def send(self, value):
            self.sent.append(value)

    socket = Socket()
    await connection._serve(socket, stop)

    # The subscribe frame's snapshot call raised, so it produced no response
    # at all -- but the loop must not have died: the ping that followed it
    # still got a real pong reply.
    assert len(socket.sent) == 1
    pong = open_response(socket.sent[0], host_signing.public_key(), browser_agreement, host_agreement.public_key(),
                          replay, now_ms=None)
    assert pong == {"v": 1, "type": "pong", "payload": {}}


@pytest.mark.asyncio
async def test_push_sweep_delivers_new_event_with_no_inbound_frame(tmp_path, monkeypatch):
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": 0})
    await channel.handle(canonical(request), now_ms=NOW)
    cursor_before = channel.sessions[DEVICE].cursor

    stats_db.insert_run_event(msg_id, 0, "text", "live")

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    sent_count = await connection._push_sweep(socket)
    assert sent_count == 1
    pushed = open_response(socket.sent[0], host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay, now_ms=None)
    assert pushed["type"] == "chat.text"
    assert pushed["payload"] == {"text": "live"}
    assert channel.sessions[DEVICE].cursor > cursor_before


@pytest.mark.asyncio
async def test_push_sweep_overflow_sends_slow_consumer_and_isolates_other_devices(tmp_path, monkeypatch):
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser2_signing, browser2_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    await pair(channel, browser2_signing, browser2_agreement, device_id=DEVICE2, ceremony_id=CEREMONY2)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    for signing, agreement, device_id in ((browser_signing, browser_agreement, DEVICE),
                                           (browser2_signing, browser2_agreement, DEVICE2)):
        request = browser_frame(signing, agreement, host_agreement.public_key(), 1,
                                 "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": 0}, device_id=device_id)
        await channel.handle(canonical(request), now_ms=NOW)

    # Three non-coalescible events overflow a queue limited to one slot.
    # Both devices share the same global scope and would otherwise both see
    # the same backlog and both overflow -- to prove isolation (one device's
    # overflow doesn't touch another's session), fast-forward DEVICE2 past
    # the backlog first, as if it had already caught up via an earlier sweep.
    monkeypatch.setattr(server_mod, "_REALTIME_OUTBOUND_QUEUE_LIMIT", 1)
    for index in range(3):
        stats_db.insert_run_event(msg_id, index, "text", f"live-{index}")
    channel.sessions[DEVICE2].cursor = stats_db.get_realtime_cursor()

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    await connection._push_sweep(socket)

    assert DEVICE not in channel.sessions
    assert DEVICE2 in channel.sessions

    by_device = {DEVICE: [], DEVICE2: []}
    for frame in socket.sent:
        by_device[json.loads(frame)["device_id"]].append(frame)

    error = open_response(by_device[DEVICE][0], host_signing.public_key(), browser_agreement,
                           host_agreement.public_key(), replay, device_id=DEVICE, now_ms=None)
    assert error == {"v": 1, "type": "error", "payload": {"code": "slow_consumer", "resumable": True}}

    # DEVICE2 was fast-forwarded past the backlog, so it has nothing new to
    # catch up on and no ping was due yet -- the isolation being proven is
    # that DEVICE's overflow produced no frame at all for DEVICE2, positive
    # or negative, and left its session in place.
    assert by_device[DEVICE2] == []


@pytest.mark.asyncio
async def test_push_sweep_survives_unexpected_error_and_isolates_other_devices(tmp_path, monkeypatch):
    """Security review finding: `_push_sweep` calls the same reused local
    realtime core as `_dispatch_adr0040` (`_realtime_catchup`), which can
    raise a plain exception outside the already-handled `_RealtimeSlowConsumer`
    case. One device's unexpected failure must not abort the sweep for every
    other subscribed device sharing the same host socket.
    """
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser2_signing, browser2_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    await pair(channel, browser2_signing, browser2_agreement, device_id=DEVICE2, ceremony_id=CEREMONY2)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    for signing, agreement, device_id in ((browser_signing, browser_agreement, DEVICE),
                                           (browser2_signing, browser2_agreement, DEVICE2)):
        request = browser_frame(signing, agreement, host_agreement.public_key(), 1,
                                 "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": 0}, device_id=device_id)
        await channel.handle(canonical(request), now_ms=NOW)

    stats_db.insert_run_event(msg_id, 0, "text", "live")

    real_catchup = server_mod._realtime_catchup

    async def flaky_catchup(outbound, from_cursor, scopes, principal, last_acked_cursor):
        if principal == f"shore:{DEVICE}":
            raise RuntimeError("unexpected core failure")
        return await real_catchup(outbound, from_cursor, scopes, principal, last_acked_cursor)
    monkeypatch.setattr(server_mod, "_realtime_catchup", flaky_catchup)

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    sent_count = await connection._push_sweep(socket)

    # DEVICE's session doesn't survive an unexpected core error (the same
    # fail-safe posture as overflow/ping-timeout eviction), but the sweep
    # itself must not abort -- DEVICE2 still gets its live event.
    assert DEVICE not in channel.sessions
    assert DEVICE2 in channel.sessions
    assert sent_count == 1
    pushed = open_response(socket.sent[0], host_signing.public_key(), browser2_agreement,
                            host_agreement.public_key(), replay, device_id=DEVICE2, now_ms=None)
    assert pushed["type"] == "chat.text"
    assert pushed["payload"] == {"text": "live"}


@pytest.mark.asyncio
async def test_push_sweep_evicts_on_ping_timeout(tmp_path, monkeypatch):
    _fresh_stats_db(tmp_path, monkeypatch)
    stats_db.insert_user_message("squid", "codex", "hello")

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)

    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": 0})
    await channel.handle(canonical(request), now_ms=NOW)
    assert DEVICE in channel.sessions

    # No frame of any kind (ack/pong/command) for two full heartbeat
    # intervals is treated as no-longer-live and evicted locally -- the
    # per-device equivalent of the direct path's heartbeat-timeout close.
    # last_inbound_at is a time.monotonic() value (seconds), so this pushes
    # it comfortably past the 40s (2 x 20s) timeout.
    channel.sessions[DEVICE].last_inbound_at -= 100

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    await connection._push_sweep(Socket())
    assert DEVICE not in channel.sessions


@pytest.mark.asyncio
async def test_session_state_survives_host_broker_reconnect(tmp_path, monkeypatch):
    """Milestone 4.6: a host<->broker socket reconnect must not force a
    resubscribe. `ShoreHostConnection.run()` constructs `ShoreChannel` once
    and reuses it across every reconnect attempt -- only the `socket` local
    is replaced each iteration -- so `channel.sessions` (a device's scopes,
    cursor, and last-acked cursor) is untouched by a reconnect. This closes
    4.3's own flagged gap: its plan text had assumed reconnect drops session
    state "by design" (mirroring the direct /ws/v1 path's per-connection
    state), which doesn't match this constructor's actual lifetime. Proven
    directly by driving `_push_sweep` against two distinct fake sockets
    standing in for two separate `run()` connection attempts, with events
    published in the gap between them (as if the host were offline).
    """
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": 0})
    await channel.handle(canonical(request), now_ms=NOW)
    cursor_before = channel.sessions[DEVICE].cursor

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    first_socket = Socket()
    assert await connection._push_sweep(first_socket) == 0
    assert DEVICE in channel.sessions

    # The host<->broker socket now drops and reconnects. Two events publish
    # while nothing is sweeping -- the same as the host being briefly offline.
    stats_db.insert_run_event(msg_id, 1, "text", "live-1")
    stats_db.insert_run_event(msg_id, 2, "text", "live-2")

    second_socket = Socket()
    sent_count = await connection._push_sweep(second_socket)
    assert sent_count == 2
    replayed = [
        open_response(frame, host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay, now_ms=None)
        for frame in second_socket.sent
    ]
    assert [event["payload"]["text"] for event in replayed] == ["live-1", "live-2"]
    assert channel.sessions[DEVICE].cursor > cursor_before
    # Delivered on the reconnected socket without the device ever resending
    # `subscribe` -- proving continuity, not a forced resubscribe.
    assert first_socket.sent == []


@pytest.mark.asyncio
async def test_resubscribe_after_dormancy_replays_full_backlog_no_loss(tmp_path, monkeypatch):
    """Milestone 4.6's own named acceptance test: starve a device of
    `subscribe` across several published events, then confirm the next
    `subscribe` -- as `ShoreDashboardSession.attempt()` sends on a
    browser-side reconnect, carrying its persisted cursor -- produces a
    complete, correctly-ordered replay rather than silently losing events
    from the gap.
    """
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    replay = ReplayStore(tmp_path / "browser-replay.db")

    # No `cursor` field: the fresh-subscribe snapshot path, not a cursor=0
    # catchup request (0 is itself a valid catchup starting point -- "replay
    # everything since the beginning" -- so it takes the other branch).
    first_subscribe = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                                     "subscribe", {"scopes": [{"lifecycle": "global"}]})
    responses = await channel.handle(canonical(first_subscribe), now_ms=NOW)
    snapshot = open_response(responses[1], host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay)
    last_acked_cursor = snapshot["payload"]["cursor"]

    # The device goes dormant (browser navigates away / loses connectivity)
    # without ever unsubscribing -- three events publish with nobody polling.
    for index, text in enumerate(("live-1", "live-2", "live-3"), start=1):
        stats_db.insert_run_event(msg_id, index, "text", text)

    # Browser reconnects and resubscribes from its persisted cursor, exactly
    # as ShoreDashboardSession.attempt() does after a reconnect.
    resubscribe = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 2,
                                 "subscribe", {"scopes": [{"lifecycle": "global"}], "cursor": last_acked_cursor})
    responses = await channel.handle(canonical(resubscribe), now_ms=NOW)
    subscribed = open_response(responses[0], host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay)
    assert subscribed == {"v": 1, "type": "subscribed", "payload": {"scopes": [{"lifecycle": "global"}]}}
    replayed = [
        open_response(frame, host_signing.public_key(), browser_agreement, host_agreement.public_key(), replay)
        for frame in responses[1:]
    ]
    assert [event["payload"]["text"] for event in replayed] == ["live-1", "live-2", "live-3"]
    assert all(event["type"] == "chat.text" for event in replayed)


@pytest.mark.asyncio
async def test_push_sweep_backlog_sequence_numbers_are_strictly_increasing_and_durable(tmp_path, monkeypatch):
    """Milestone 4.6's other named concern: the durable per-device outbound
    sequence counter (`ShoreChannel._next_sequence`, `outbound.sqlite3`) must
    not race or duplicate now that a single `_push_sweep` call can seal many
    `host_to_browser` frames back to back for one device, and must survive a
    `ShoreChannel` rebuild (e.g. a daemon restart) the same way the probe
    path already proves in
    test_live_channel_pairs_persists_trust_and_probe_round_trips -- this is
    the same durability guarantee, exercised through the push path instead.
    """
    _fresh_stats_db(tmp_path, monkeypatch)
    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)

    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)

    # Fresh-snapshot subscribe (no `cursor`): the subscribe dispatch itself
    # only seals one "subscribed" + one "snapshot" frame regardless of how
    # many events already exist, keeping this test's own baseline small and
    # independent of the fixture's pre-existing message.changed events.
    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}]})
    await channel.handle(canonical(request), now_ms=NOW)
    # Read the durable counter directly rather than via _next_sequence(),
    # which -- unlike every real call site -- would itself consume a real
    # sequence number just to report one, leaving a gap no envelope was ever
    # sealed for.
    with sqlite3.connect(tmp_path / "outbound.sqlite3") as raw:
        row = raw.execute("SELECT value FROM sequences WHERE scope=?", (f"{DEVICE}:1:host_to_browser",)).fetchone()
    sequence_before = row[0] if row else 0

    for index, text in enumerate(("live-1", "live-2", "live-3", "live-4", "live-5"), start=1):
        stats_db.insert_run_event(msg_id, index, "text", text)

    connection = ShoreHostConnection(channel, broker="https://broker.example", username="alice",
        host_id=HOST, signing_key=host_signing)

    class Socket:
        def __init__(self): self.sent = []
        async def send(self, value): self.sent.append(value)

    socket = Socket()
    assert await connection._push_sweep(socket) == 5
    sequences = [int(json.loads(frame)["seq"]) for frame in socket.sent]
    assert sequences == list(range(sequence_before + 1, sequence_before + 6))

    # A rebuilt ShoreChannel against the same state_dir (e.g. daemon restart)
    # must continue the durable sequence, not reset or collide with it.
    restarted = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    assert restarted._next_sequence(channel.trust.get(DEVICE)) == sequences[-1] + 1
