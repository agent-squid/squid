from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from agent.shore_crypto import ReplayStore, ShoreProtocolError, canonical, open_envelope, seal_envelope


ACCOUNT_ID = "018f1f25-3f6b-7d75-a4d1-62d771381b20"
HOST_ID = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
DEVICE_ID = "018f1f25-8614-7e41-8c5c-fc0b6eefad62"
REQUEST_ID = "018f1f25-c930-76f0-86e7-cb06d94e6a30"


def _fixture(tmp_path):
    sender_signing = ed25519.Ed25519PrivateKey.generate()
    sender_agreement = x25519.X25519PrivateKey.generate()
    receiver_agreement = x25519.X25519PrivateKey.generate()
    now_ms = 1_788_278_400_000
    issued = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    expires = datetime.fromtimestamp((now_ms + 30_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    envelope = seal_envelope(
        {"payload": {}, "request_id": None, "scope": None, "type": "ping", "v": 1},
        account_id=ACCOUNT_ID, host_id=HOST_ID, device_id=DEVICE_ID, key_epoch=1,
        direction="browser_to_host", seq=1, request_id=REQUEST_ID, issued_at=issued,
        expires_at=expires, sender_signing=sender_signing, sender_agreement=sender_agreement,
        receiver_agreement=receiver_agreement.public_key(),
    )
    arguments = {
        "expected": {"account_id": ACCOUNT_ID, "host_id": HOST_ID, "device_id": DEVICE_ID,
                     "key_epoch": 1, "direction": "browser_to_host"},
        "sender_signing": sender_signing.public_key(),
        "receiver_agreement": receiver_agreement,
        "sender_agreement": sender_agreement.public_key(),
        "replay": ReplayStore(tmp_path / "replay.db"),
        "now_ms": now_ms,
    }
    return envelope, arguments, sender_signing


def test_authenticated_decryption_precedes_replay_commit(tmp_path):
    envelope, arguments, signing = _fixture(tmp_path)
    corrupt = dict(envelope)
    corrupt["ciphertext"] = ("A" if corrupt["ciphertext"][0] != "A" else "B") + corrupt["ciphertext"][1:]
    unsigned = {key: value for key, value in corrupt.items() if key != "signature"}
    from agent.shore_crypto import b64url
    corrupt["signature"] = b64url(signing.sign(canonical(unsigned)))

    with pytest.raises(ShoreProtocolError, match="shore_decrypt_failed"):
        open_envelope(corrupt, **arguments)

    assert open_envelope(envelope, **arguments)["type"] == "ping"


@pytest.mark.parametrize("field,value", [("v", 2), ("seq", str(1 << 64)), ("key_epoch", True)])
def test_rejects_invalid_protocol_header_before_dispatch(tmp_path, field, value):
    envelope, arguments, _ = _fixture(tmp_path)
    envelope[field] = value
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        open_envelope(envelope, **arguments)


def test_malformed_envelope_has_stable_protocol_error(tmp_path):
    envelope, arguments, _ = _fixture(tmp_path)
    envelope["ciphertext"] = float("nan")
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        open_envelope(envelope, **arguments)


def test_replay_accept_is_atomic_across_concurrent_receivers(tmp_path):
    path = tmp_path / "replay.db"

    def accept_once(_):
        return ReplayStore(path).accept("device-direction", 1, REQUEST_ID, 1_788_278_400_000)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(accept_once, range(16)))

    assert results.count(True) == 1
    assert results.count(False) == 15


def test_replay_storage_failure_is_a_stable_protocol_error(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr("agent.shore_crypto.sqlite3.connect", unavailable)
    with pytest.raises(ShoreProtocolError, match="shore_replay"):
        ReplayStore(tmp_path / "replay.db").accept("device-direction", 1, REQUEST_ID, 1_788_278_400_000)
