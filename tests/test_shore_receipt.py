import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.shore_audit import ShoreAuditLog
from agent.shore_crypto import b64url, canonical
from agent.shore_receipt import (
    RECEIPT_GENESIS_HASH, ReceiptVerificationError, envelope_commitment,
    verify_relay_receipt,
)

HOST = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
REQUEST_1 = "018f1f25-c930-76f0-86e7-cb06d94e6a30"
REQUEST_2 = "018f1f25-c930-76f0-86e7-cb06d94e6a31"


def test_normative_relay_receipt_vector():
    vectors = json.loads((Path(__file__).parents[1] / "docs" / "shore-protocol-v1-vectors.json").read_text())
    vector = vectors["receipt"]
    envelope = json.loads(vector["envelope_jcs"])
    assert set(envelope) == {
        "v", "account_id", "host_id", "device_id", "key_epoch", "direction",
        "seq", "request_id", "issued_at", "expires_at", "nonce", "ciphertext", "signature",
    }
    assert vector["fields"]["type"] == "relay_receipt"
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(vector["shore_ed25519_seed_hex"]))
    public = key.public_key()

    public_bytes = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert b64url(public_bytes) == vector["shore_ed25519_public"]
    for receipt in (vector, vector["next"]):
        assert "sha256:" + b64url(hashlib.sha256(receipt["envelope_jcs"].encode()).digest()) == receipt["fields"]["envelope_hash"]
        assert b64url(hashlib.sha256(canonical(receipt["fields"])).digest()) == receipt["receipt_hash"]
        signed = {**receipt["fields"], "receipt_hash": receipt["receipt_hash"]}
        signature = base64.urlsafe_b64decode(receipt["signature"] + "==")
        public.verify(signature, canonical(signed))

    assert vector["next"]["fields"]["direction"] == "host_to_browser"
    assert vector["next"]["fields"]["prev_hash"] == vector["receipt_hash"]


def _receipt(key, envelope, *, seq=1, previous=RECEIPT_GENESIS_HASH, request_id=REQUEST_1):
    fields = {
        "v": 1, "type": "relay_receipt", "host_id": HOST, "request_id": request_id,
        "direction": "browser_to_host", "disposition": "accepted", "receipt_epoch": 1,
        "seq": str(seq), "prev_hash": previous, "envelope_hash": envelope_commitment(envelope),
    }
    digest = b64url(hashlib.sha256(canonical(fields)).digest())
    return {**fields, "receipt_hash": digest,
            "signature": b64url(key.sign(canonical({**fields, "receipt_hash": digest})))}


def test_host_receipt_verification_and_atomic_audit_checkpoint(tmp_path):
    relay_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    envelope = canonical({"request_id": REQUEST_1, "ciphertext": "opaque"})
    receipt = _receipt(relay_key, envelope)
    verified = verify_relay_receipt(
        receipt, envelope, host_id=HOST, direction="browser_to_host",
        keys={1: relay_key.public_key()},
    )
    audit = ShoreAuditLog(tmp_path / "audit.sqlite3", host_id=HOST, key_epoch=1, host_signing=host_key)
    event = audit.record(
        request_id=REQUEST_1, device_id="device-1", message_type="subscribe",
        frame={"v": 1}, decision="granted", outcome="pending", now_ms=1,
        relay_receipt=verified,
    )
    assert event is not None
    assert audit.has_receipt(receipt)
    assert not audit.has_receipt({**receipt, "unexpected": True})
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        assert connection.execute("SELECT seq, hash FROM relay_receipt_tip").fetchone() == (1, receipt["receipt_hash"])
        assert connection.execute("SELECT count(*) FROM audit_chain").fetchone() == (1,)
    assert audit.record(
        request_id=REQUEST_1, device_id="device-1", message_type="subscribe",
        frame={"v": 1}, decision="granted", outcome="pending", now_ms=1,
        relay_receipt=verified,
    ) is None
    assert len(audit.events()) == 1


def test_host_receipt_rejects_mutation_and_gap(tmp_path):
    relay_key = Ed25519PrivateKey.generate()
    envelope = b"opaque"
    first = _receipt(relay_key, envelope)
    with pytest.raises(ReceiptVerificationError, match="shore_receipt_conflict"):
        verify_relay_receipt(first, b"changed", host_id=HOST, direction="browser_to_host",
                             keys={1: relay_key.public_key()})
    audit = ShoreAuditLog(
        tmp_path / "audit.sqlite3", host_id=HOST, key_epoch=1,
        host_signing=Ed25519PrivateKey.generate(),
    )
    gap = verify_relay_receipt(
        _receipt(relay_key, envelope, seq=2, request_id=REQUEST_2), envelope,
        host_id=HOST, direction="browser_to_host", keys={1: relay_key.public_key()},
    )
    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        audit.accept_receipt(gap)


def test_forged_receipt_is_unavailable_not_confirmed_conflict():
    relay_key = Ed25519PrivateKey.generate()
    envelope = b"opaque"
    forged = _receipt(Ed25519PrivateKey.generate(), envelope)
    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        verify_relay_receipt(
            forged, envelope, host_id=HOST, direction="browser_to_host",
            keys={1: relay_key.public_key()},
        )


def test_signed_wrong_previous_tip_is_confirmed_conflict(tmp_path):
    relay_key = Ed25519PrivateKey.generate()
    audit = ShoreAuditLog(
        tmp_path / "audit.sqlite3", host_id=HOST, key_epoch=1,
        host_signing=Ed25519PrivateKey.generate(),
    )
    first_bytes = b"first"
    first = verify_relay_receipt(
        _receipt(relay_key, first_bytes), first_bytes, host_id=HOST,
        direction="browser_to_host", keys={1: relay_key.public_key()},
    )
    assert audit.accept_receipt(first)
    second_bytes = b"second"
    fork = verify_relay_receipt(
        _receipt(relay_key, second_bytes, seq=2, previous=RECEIPT_GENESIS_HASH, request_id=REQUEST_2),
        second_bytes, host_id=HOST, direction="browser_to_host", keys={1: relay_key.public_key()},
    )
    with pytest.raises(ReceiptVerificationError, match="shore_receipt_conflict"):
        audit.accept_receipt(fork)


def test_missing_durable_tip_is_continuity_unavailable(tmp_path):
    relay_key = Ed25519PrivateKey.generate()
    audit = ShoreAuditLog(
        tmp_path / "audit.sqlite3", host_id=HOST, key_epoch=1,
        host_signing=Ed25519PrivateKey.generate(),
    )
    first_bytes = b"first"
    first_receipt = _receipt(relay_key, first_bytes)
    first = verify_relay_receipt(
        first_receipt, first_bytes, host_id=HOST, direction="browser_to_host",
        keys={1: relay_key.public_key()},
    )
    audit.accept_receipt(first)
    with sqlite3.connect(tmp_path / "audit.sqlite3") as connection:
        connection.execute("DELETE FROM relay_receipt_tip")
    second_bytes = b"second"
    second = verify_relay_receipt(
        _receipt(relay_key, second_bytes, seq=2, previous=first_receipt["receipt_hash"], request_id=REQUEST_2),
        second_bytes, host_id=HOST, direction="browser_to_host", keys={1: relay_key.public_key()},
    )
    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        audit.accept_receipt(second)
