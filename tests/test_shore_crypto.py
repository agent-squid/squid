import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from agent.shore_crypto import (
    DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    b64url, canonical, derive_envelope_key, derive_pair_key, open_envelope,
    pairing_finished, seal_envelope, unb64url,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "docs" / "shore-protocol-v1-vectors.json").read_text()
)


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


def test_local_pairing_persists_keys_only_after_finished_proof(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    pairing = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)
    signing_key, agreement_key = bytes(range(32)), bytes(range(32, 64))
    offer = pairing.begin(DEVICE_ID, signing_key, agreement_key, now=100)
    binding = canonical(offer["binding"])
    assert trust.get(DEVICE_ID) is None
    import base64
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    key = derive_pair_key(decode(offer["secret"]), decode(offer["binding"]["ceremony_nonce"]), binding)
    host_proof = pairing.finish(DEVICE_ID, pairing_finished(key, "browser", binding), now=101)
    assert host_proof == pairing_finished(key, "host", binding)
    assert trust.get(DEVICE_ID).signing_key == signing_key
    assert trust.get(DEVICE_ID).agreement_key == agreement_key


def test_pairing_is_single_use_failure_bounded_and_cannot_replace_keys(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    make_pairing = lambda: PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)
    pairing = make_pairing()
    offer = pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=100)
    binding = canonical(offer["binding"])
    for _ in range(5):
        with pytest.raises(ShoreProtocolError, match="pairing_failed"):
            pairing.finish(DEVICE_ID, b"wrong", now=101)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        pairing.finish(DEVICE_ID, b"wrong", now=101)
    assert trust.get(DEVICE_ID) is None

    pairing = make_pairing()
    offer = pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=100)
    binding = canonical(offer["binding"])
    import base64
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    key = derive_pair_key(decode(offer["secret"]), decode(offer["binding"]["ceremony_nonce"]), binding)
    pairing.finish(DEVICE_ID, pairing_finished(key, "browser", binding), now=101)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        trust.approve(DEVICE_ID, b"x" * 32, b"a" * 32, 1)
    assert trust.revoke(DEVICE_ID)
    assert trust.get(DEVICE_ID) is None


def test_expired_pairing_never_creates_trust(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    pairing = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=ed25519.Ed25519PrivateKey.generate().public_key(),
        host_agreement_key=x25519.X25519PrivateKey.generate().public_key(), key_epoch=1)
    pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=100)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        pairing.finish(DEVICE_ID, b"anything", now=400)
    assert trust.get(DEVICE_ID) is None


def test_abandoned_expired_pairing_can_be_replaced_without_restart(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    pairing = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=ed25519.Ed25519PrivateKey.generate().public_key(),
        host_agreement_key=x25519.X25519PrivateKey.generate().public_key(), key_epoch=1)
    first = pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=100)
    abandoned = pairing._ceremonies[DEVICE_ID][0]
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=399.999)
    second = pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=400)
    assert first["secret"] != second["secret"]
    assert bytes(abandoned.secret) == bytes(16)
    assert pairing._ceremonies[DEVICE_ID][0].expires_at == 700


def test_expiry_callback_erases_abandoned_secret_without_device_return(tmp_path):
    pairing = PairingCoordinator(DeviceTrustStore(tmp_path / "trust.db"),
        account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=ed25519.Ed25519PrivateKey.generate().public_key(),
        host_agreement_key=x25519.X25519PrivateKey.generate().public_key(), key_epoch=1)
    pairing.begin(DEVICE_ID, b"s" * 32, b"a" * 32, now=100)
    ceremony = pairing._ceremonies[DEVICE_ID][0]
    pairing._expire(DEVICE_ID, ceremony)
    assert bytes(ceremony.secret) == bytes(16)
    assert DEVICE_ID not in pairing._ceremonies


def test_persistence_failure_does_not_strand_used_ceremony(tmp_path, monkeypatch):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    pairing = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    raw = lambda key: key.public_bytes_raw()
    offer = pairing.begin(DEVICE_ID, raw(browser_signing.public_key()), raw(browser_agreement.public_key()), now=100)
    binding = canonical(offer["binding"])
    import base64
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    proof = pairing_finished(derive_pair_key(decode(offer["secret"]), decode(offer["binding"]["ceremony_nonce"]), binding), "browser", binding)
    monkeypatch.setattr(trust, "approve", lambda *args: (_ for _ in ()).throw(ShoreProtocolError("pairing_failed")))
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        pairing.finish(DEVICE_ID, proof, now=101)
    pairing.begin(DEVICE_ID, raw(browser_signing.public_key()), raw(browser_agreement.public_key()), now=102)


def test_reproduces_normative_shore_v1_envelope_vector():
    # shore-protocol-v1.md is the cross-language wire contract: an
    # implementation MUST reproduce these exact bytes, not merely round-trip
    # with itself. Only test/shore.test.ts pinned these before this test.
    env = VECTORS["envelope"]
    host_agreement = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(env["host_x25519_private_hex"]))
    browser_agreement = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(env["browser_x25519_private_hex"]))
    browser_signing = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(env["browser_ed25519_seed_hex"]))

    key = derive_envelope_key(browser_agreement, host_agreement.public_key(), ACCOUNT_ID, HOST_ID, 1, "browser_to_host")
    assert b64url(key) == env["aes_key"]

    envelope = seal_envelope(
        {"payload": {"client_id": "phone-01", "cursor": 17, "global": True, "resources": []},
         "request_id": None, "scope": None, "type": "subscribe", "v": 1},
        account_id=ACCOUNT_ID, host_id=HOST_ID, device_id=DEVICE_ID, key_epoch=1,
        direction="browser_to_host", seq=1, request_id=REQUEST_ID,
        issued_at="2026-09-01T12:00:00.000Z", expires_at="2026-09-01T12:00:30.000Z",
        sender_signing=browser_signing, sender_agreement=browser_agreement,
        receiver_agreement=host_agreement.public_key(), nonce=unb64url("AAECAwQFBgcICQoL"),
    )
    assert envelope["ciphertext"] == env["ciphertext_and_tag"]
    assert envelope["signature"] == env["signature"]


def test_reproduces_normative_shore_v1_pairing_vector():
    pairing = VECTORS["pairing"]
    secret, nonce = bytes.fromhex(pairing["pairing_secret_hex"]), bytes.fromhex(pairing["ceremony_nonce_hex"])
    binding = pairing["binding_jcs"].encode()
    key = derive_pair_key(secret, nonce, binding)
    assert b64url(key) == pairing["pair_key"]
    assert b64url(pairing_finished(key, "browser", binding)) == pairing["browser_finished"]


def test_reproduces_normative_shore_v1_recovery_verifier_vector():
    import hashlib
    recovery = VECTORS["recovery"]
    secret = unb64url(recovery["secret"])
    assert len(secret) == 32
    prefix = f"shore-recovery-v1\0{recovery['account_id']}".encode()
    assert b64url(hashlib.sha256(prefix + secret).digest()) == recovery["verifier"]


def test_concurrent_pairing_finish_resolves_to_exactly_one_success(tmp_path):
    # state_machine_vectors["pairing_race"]: two concurrent valid finish()
    # calls on the same ceremony must yield success_count=1 and the loser
    # gets the same generic failure as an expired/unknown ceremony.
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    pairing = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    raw = lambda key: key.public_bytes_raw()
    offer = pairing.begin(DEVICE_ID, raw(browser_signing.public_key()), raw(browser_agreement.public_key()), now=100)
    binding = canonical(offer["binding"])
    key = derive_pair_key(unb64url(offer["secret"]), unb64url(offer["binding"]["ceremony_nonce"]), binding)
    proof = pairing_finished(key, "browser", binding)

    def attempt(_index):
        try:
            return ("ok", pairing.finish(DEVICE_ID, proof, now=101))
        except ShoreProtocolError as exc:
            return ("failed", exc.code)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))

    successes = [result for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "failed"]
    assert len(successes) == 1
    assert len(failures) == 7
    assert all(code == "pairing_failed" for _, code in failures)
    assert trust.get(DEVICE_ID) is not None
