import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.shore_crypto import b64url, canonical


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
