import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.shore_crypto import (
    DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    b64url, canonical, crockford32_decode, crockford32_encode, derive_envelope_key, derive_pair_bootstrap_key, derive_pair_key, open_envelope,
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


@pytest.mark.parametrize("override", [
    {"account_id": "not-a-uuid"}, {"request_id": "not-a-uuid"},
    {"seq": True}, {"seq": (1 << 32) + 1}, {"key_epoch": 1 << 53},
    {"issued_at": "not-a-time"}, {"nonce": b""},
    {"expires_at": "2026-09-01T12:00:00.000Z"},
    {"expires_at": "2026-09-01T12:01:01.000Z"},
])
def test_seal_envelope_rejects_noncanonical_headers(tmp_path, override):
    envelope, arguments, signing = _fixture(tmp_path)
    values = {
        "account_id": envelope["account_id"], "host_id": envelope["host_id"],
        "device_id": envelope["device_id"], "key_epoch": envelope["key_epoch"],
        "direction": envelope["direction"], "seq": int(envelope["seq"]),
        "request_id": envelope["request_id"], "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"], "nonce": unb64url(envelope["nonce"]),
        "sender_signing": signing,
        "sender_agreement": x25519.X25519PrivateKey.generate(),
        "receiver_agreement": arguments["receiver_agreement"].public_key(),
    }
    values.update(override)
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        seal_envelope({"v": 1, "type": "ping"}, **values)


@pytest.mark.parametrize("field,value", [
    ("v", 2), ("seq", str((1 << 32) + 1)), ("key_epoch", True), ("key_epoch", 1 << 53),
])
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


def test_open_envelope_rejects_wrong_signing_key(tmp_path):
    envelope, arguments, _ = _fixture(tmp_path)
    arguments["sender_signing"] = ed25519.Ed25519PrivateKey.generate().public_key()
    with pytest.raises(ShoreProtocolError, match="shore_bad_signature"):
        open_envelope(envelope, **arguments)


def test_open_envelope_rejects_substituted_receiver_key(tmp_path):
    envelope, arguments, _ = _fixture(tmp_path)
    arguments["receiver_agreement"] = x25519.X25519PrivateKey.generate()
    with pytest.raises(ShoreProtocolError, match="shore_decrypt_failed"):
        open_envelope(envelope, **arguments)


def test_open_envelope_rejects_key_epoch_mismatch(tmp_path):
    envelope, arguments, _ = _fixture(tmp_path)
    arguments["expected"] = {**arguments["expected"], "key_epoch": 2}
    with pytest.raises(ShoreProtocolError, match="shore_key_epoch_mismatch"):
        open_envelope(envelope, **arguments)


def _resign(envelope, signing):
    unsigned = {key: value for key, value in envelope.items() if key != "signature"}
    envelope["signature"] = b64url(signing.sign(canonical(unsigned)))
    return envelope


def test_open_envelope_rejects_future_issued_at_as_clock_skew(tmp_path):
    envelope, arguments, signing = _fixture(tmp_path)
    skewed = datetime.fromtimestamp((arguments["now_ms"] + 60_000) / 1000, timezone.utc)
    envelope["issued_at"] = skewed.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _resign(envelope, signing)
    with pytest.raises(ShoreProtocolError, match="shore_clock_skew"):
        open_envelope(envelope, **arguments)


def test_open_envelope_rejects_expired_frame(tmp_path):
    envelope, arguments, signing = _fixture(tmp_path)
    expired = datetime.fromtimestamp((arguments["now_ms"] - 60_000) / 1000, timezone.utc)
    envelope["expires_at"] = expired.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _resign(envelope, signing)
    with pytest.raises(ShoreProtocolError, match="shore_expired"):
        open_envelope(envelope, **arguments)


def test_open_envelope_rejects_replay_of_same_request(tmp_path):
    envelope, arguments, _ = _fixture(tmp_path)
    assert open_envelope(envelope, **arguments)["type"] == "ping"
    with pytest.raises(ShoreProtocolError, match="shore_replay"):
        open_envelope(envelope, **arguments)


def test_open_envelope_rejects_reordered_lower_sequence(tmp_path):
    # seq/request_id are bound into the AEAD's AAD, so a genuine reorder
    # attack must be a freshly, validly sealed envelope with an old sequence
    # number (e.g. a captured-and-replayed prior message), not a field-swap
    # on an already-accepted one.
    envelope, arguments, signing = _fixture(tmp_path)
    sender_agreement = x25519.X25519PrivateKey.generate()
    assert open_envelope(envelope, **arguments)["type"] == "ping"
    later = seal_envelope(
        {"payload": {}, "request_id": None, "scope": None, "type": "ping", "v": 1},
        account_id=ACCOUNT_ID, host_id=HOST_ID, device_id=DEVICE_ID, key_epoch=1,
        direction="browser_to_host", seq=1, request_id="018f1f25-c930-76f0-86e7-cb06d94e6a31",
        issued_at="2026-09-01T12:00:00.000Z", expires_at="2026-09-01T12:00:29.000Z",
        sender_signing=signing, sender_agreement=sender_agreement,
        receiver_agreement=arguments["receiver_agreement"].public_key(),
    )
    later_now_ms = int(datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    later_arguments = {**arguments, "sender_agreement": sender_agreement.public_key(), "now_ms": later_now_ms}
    with pytest.raises(ShoreProtocolError, match="shore_replay"):
        open_envelope(later, **later_arguments)


CEREMONY_ID = "018f1f25-c930-76f0-86e7-cb06d94e6a32"


def _pairing_fixture(tmp_path, *, tamper_host=False):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    coordinator = PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    offer = coordinator.begin(ceremony_id=CEREMONY_ID, now=100)
    binding = {"v": 1, "account_id": ACCOUNT_ID, "host_id": HOST_ID, "device_id": DEVICE_ID,
        "ceremony_nonce": offer["offer"]["ceremony_nonce"],
        "host_sign_fingerprint": offer["offer"]["host_sign_fingerprint"],
        "host_enc_fingerprint": offer["offer"]["host_enc_fingerprint"],
        "browser_sign_fingerprint": "", "browser_enc_fingerprint": ""}
    from agent.shore_crypto import fingerprint
    binding["browser_sign_fingerprint"] = fingerprint(browser_signing.public_key())
    binding["browser_enc_fingerprint"] = fingerprint(browser_agreement.public_key())
    if tamper_host: binding["host_sign_fingerprint"] = "sha256:" + b64url(bytes(32))
    binding_bytes = canonical(binding)
    assert len(offer["code"]) == 26
    secret, ceremony_nonce = crockford32_decode(offer["code"]), unb64url(offer["offer"]["ceremony_nonce"])
    pair_key = derive_pair_key(secret, ceremony_nonce, binding_bytes)
    plaintext = {"v": 1, "binding": binding,
        "browser_keys": {"signing": b64url(browser_signing.public_key().public_bytes_raw()),
                         "agreement": b64url(browser_agreement.public_key().public_bytes_raw())},
        "finished": b64url(pairing_finished(pair_key, "browser", binding_bytes))}
    nonce = bytes(range(12))
    packet = {"v": 1, "ceremony_id": CEREMONY_ID, "direction": "browser_to_host", "nonce": b64url(nonce)}
    packet["ciphertext"] = b64url(AESGCM(derive_pair_bootstrap_key(secret, ceremony_nonce)).encrypt(nonce, canonical(plaintext), canonical(packet)))
    return trust, coordinator, packet, pair_key, binding_bytes


def _confirmation(pair_key, binding, nonce=bytes(range(24, 36))):
    packet = {"v": 1, "ceremony_id": CEREMONY_ID, "direction": "browser_to_host", "nonce": b64url(nonce)}
    plaintext = {"v": 1, "binding": json.loads(binding),
                 "finished": b64url(pairing_finished(pair_key, "browser-confirmed", binding))}
    packet["ciphertext"] = b64url(AESGCM(pair_key).encrypt(nonce, canonical(plaintext), canonical(packet)))
    return packet


def test_host_starts_blind_and_learns_browser_identity_from_packet(tmp_path):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    assert trust.get(DEVICE_ID) is None
    response = coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    assert response["direction"] == "host_to_browser"
    plaintext = AESGCM(pair_key).decrypt(unb64url(response["nonce"]), unb64url(response["ciphertext"]), canonical({k: v for k, v in response.items() if k != "ciphertext"}))
    assert unb64url(json.loads(plaintext)["finished"]) == pairing_finished(pair_key, "host", binding)
    assert trust.get(DEVICE_ID) is None
    coordinator.accept_browser_confirmation(_confirmation(pair_key, binding), now=102)
    assert trust.get(DEVICE_ID) is not None


def test_transport_pairing_phase_selection_is_atomic_under_overlapping_frames(tmp_path, monkeypatch):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    confirmation = _confirmation(pair_key, binding)
    original_accept = coordinator.accept_browser_packet
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    raced_into_first_phase = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def paused_accept(value, *, now=None, response_nonce=None):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            # Before the atomic phase-dispatch fix, the confirmation could
            # select the first-packet path while the first frame was paused.
            raced_into_first_phase.set()
        return original_accept(value, now=now, response_nonce=response_nonce)

    monkeypatch.setattr(coordinator, "accept_browser_packet", paused_accept)

    def send_confirmation():
        second_started.set()
        return coordinator.accept_packet(confirmation, now=102)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.accept_packet, packet, now=101)
        assert first_entered.wait(2)
        second = pool.submit(send_confirmation)
        assert second_started.wait(2)
        assert not raced_into_first_phase.wait(0.2)
        release_first.set()
        assert first.result(timeout=2)["direction"] == "host_to_browser"
        assert second.result(timeout=2) is None

    assert calls == 1
    assert coordinator._failure_history == []
    assert trust.get(DEVICE_ID) is not None


def test_pairing_rejects_tampered_offer_binding_and_is_failure_bounded(tmp_path):
    trust, coordinator, packet, _, _ = _pairing_fixture(tmp_path, tamper_host=True)
    for _ in range(5):
        with pytest.raises(ShoreProtocolError, match="pairing_failed"):
            coordinator.accept_browser_packet(packet, now=101)
    assert CEREMONY_ID not in coordinator._ceremonies
    assert trust.get(DEVICE_ID) is None


def test_expired_pairing_never_creates_trust(tmp_path):
    trust, coordinator, packet, _, _ = _pairing_fixture(tmp_path)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        coordinator.accept_browser_packet(packet, now=400)
    assert trust.get(DEVICE_ID) is None


def test_expiry_callback_erases_abandoned_secret(tmp_path):
    _, coordinator, _, _, _ = _pairing_fixture(tmp_path)
    ceremony = coordinator._ceremonies[CEREMONY_ID]
    coordinator._expire(CEREMONY_ID, ceremony)
    assert bytes(ceremony.secret) == bytes(16)
    assert CEREMONY_ID not in coordinator._ceremonies


def test_persistence_failure_consumes_ceremony_but_allows_new_one(tmp_path, monkeypatch):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    monkeypatch.setattr(trust, "approve", lambda *args: (_ for _ in ()).throw(ShoreProtocolError("pairing_failed")))
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        coordinator.accept_browser_confirmation(_confirmation(pair_key, binding), now=102)


def test_paired_device_keys_cannot_be_replaced(tmp_path):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    coordinator.accept_browser_confirmation(_confirmation(pair_key, binding), now=102)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        trust.approve(DEVICE_ID, b"x" * 32, b"a" * 32, 1)


def test_completed_repairing_advances_same_device_keys_to_new_epoch(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    signing, agreement = b"s" * 32, b"a" * 32
    trust.approve(DEVICE_ID, signing, agreement, 1, now_ms=100)
    advanced = trust.approve(DEVICE_ID, signing, agreement, 2, now_ms=200)
    assert advanced.key_epoch == 2
    assert trust.get(DEVICE_ID).key_epoch == 2
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        trust.approve(DEVICE_ID, signing, agreement, 1, now_ms=300)


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
    assert b64url(derive_pair_bootstrap_key(secret, nonce)) == pairing["bootstrap_key"]
    key = derive_pair_key(secret, nonce, binding)
    assert b64url(key) == pairing["pair_key"]
    assert b64url(pairing_finished(key, "browser", binding)) == pairing["browser_finished"]
    assert b64url(pairing_finished(key, "host", binding)) == pairing["host_finished"]
    for role, packet_key in (("browser", derive_pair_bootstrap_key(secret, nonce)), ("host", key)):
        plaintext = canonical(pairing[f"{role}_plaintext"])
        aad = pairing[f"{role}_aad_jcs"].encode()
        assert canonical(json.loads(aad)) == aad
        packet_nonce = unb64url(pairing[f"{role}_packet_nonce"])
        assert b64url(AESGCM(packet_key).encrypt(packet_nonce, plaintext, aad)) == pairing[f"{role}_ciphertext_and_tag"]
    assert b64url(pairing_finished(key, "browser-confirmed", binding)) == pairing["browser_confirmation_finished"]
    confirmation_plaintext = canonical(pairing["browser_confirmation_plaintext"])
    confirmation_aad = pairing["browser_confirmation_aad_jcs"].encode()
    assert b64url(AESGCM(key).encrypt(unb64url(pairing["browser_confirmation_nonce"]), confirmation_plaintext,
                                      confirmation_aad)) == pairing["browser_confirmation_ciphertext_and_tag"]


def test_pairing_rejects_reused_nonce_and_noncanonical_base64url(tmp_path):
    trust, coordinator, packet, _, _ = _pairing_fixture(tmp_path)
    corrupt = dict(packet)
    corrupt["ciphertext"] = ("A" if corrupt["ciphertext"][0] != "A" else "B") + corrupt["ciphertext"][1:]
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        coordinator.accept_browser_packet(corrupt, now=101)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        coordinator.accept_browser_packet(packet, now=101)
    assert trust.get(DEVICE_ID) is None
    canonical_key = b64url(bytes(32))
    with pytest.raises(ValueError, match="non-canonical"):
        unb64url(canonical_key[:-1] + "B")


def test_crockford_pairing_code_is_case_insensitive(tmp_path):
    _, coordinator, _, _, _ = _pairing_fixture(tmp_path)
    ceremony = coordinator._ceremonies[CEREMONY_ID]
    assert crockford32_decode(crockford32_encode(bytes(ceremony.secret)).lower()) == bytes(ceremony.secret)


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
    trust, pairing, packet, pair_key, binding = _pairing_fixture(tmp_path)
    pairing.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    confirmation = _confirmation(pair_key, binding)

    def attempt(_index):
        try:
            return ("ok", pairing.accept_browser_confirmation(confirmation, now=102))
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


def test_completed_pairing_confirmation_cannot_be_replayed(tmp_path):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    confirmation = _confirmation(pair_key, binding)
    coordinator.accept_browser_confirmation(confirmation, now=102)
    with pytest.raises(ShoreProtocolError, match="pairing_failed"):
        coordinator.accept_browser_confirmation(confirmation, now=103)


def test_revoked_device_trust_is_not_returned(tmp_path):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    coordinator.accept_browser_confirmation(_confirmation(pair_key, binding), now=102)
    assert trust.get(DEVICE_ID) is not None
    assert trust.revoke(DEVICE_ID) is True
    assert trust.get(DEVICE_ID) is None
    assert trust.revoke(DEVICE_ID) is False


def _uuid7ish(index: int) -> str:
    return f"018f1f25-c930-76f0-86e7-{index:012x}"


def _new_coordinator(tmp_path):
    trust = DeviceTrustStore(tmp_path / "trust.db")
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    return PairingCoordinator(trust, account_id=ACCOUNT_ID, host_id=HOST_ID,
        host_signing_key=host_signing.public_key(), host_agreement_key=host_agreement.public_key(), key_epoch=1)


def _garbage_packet(ceremony_id: str, index: int) -> dict:
    nonce = bytes([index % 256]) * 12
    return {"v": 1, "ceremony_id": ceremony_id, "direction": "browser_to_host",
            "nonce": b64url(nonce), "ciphertext": b64url(bytes(32))}


def test_pairing_begin_is_rate_limited_after_max_ceremonies(tmp_path):
    # An attacker (or a runaway client) starting unbounded ceremonies is a
    # resource-exhaustion vector even though the 128-bit secret itself makes
    # guessing infeasible; this is the "rate-limited at every relevant
    # identity layer" requirement for the host-side pairing coordinator.
    coordinator = _new_coordinator(tmp_path)
    limit = coordinator._MAX_BEGINS_PER_WINDOW
    for index in range(limit):
        coordinator.begin(ceremony_id=_uuid7ish(index), now=100)
    with pytest.raises(ShoreProtocolError, match="pairing_rate_limited"):
        coordinator.begin(ceremony_id=_uuid7ish(limit), now=100)
    # The window is a sliding one: once it elapses, begin() works again.
    revived = coordinator.begin(ceremony_id=_uuid7ish(limit), now=100 + coordinator._BEGIN_WINDOW_SECONDS + 1)
    assert revived["offer"]["ceremony_id"] == _uuid7ish(limit)


def test_pairing_begin_limit_does_not_block_open_ceremony(tmp_path):
    trust, coordinator, packet, pair_key, binding = _pairing_fixture(tmp_path)
    for index in range(coordinator._MAX_BEGINS_PER_WINDOW - 1):
        coordinator.begin(ceremony_id=_uuid7ish(index), now=100)

    coordinator.accept_browser_packet(packet, now=101, response_nonce=bytes(range(12, 24)))
    coordinator.accept_browser_confirmation(_confirmation(pair_key, binding), now=102)
    assert trust.get(DEVICE_ID) is not None


def test_pairing_begin_is_rate_limited_after_aggregate_failures_across_ceremonies(tmp_path):
    # Each ceremony independently tolerates 5 failed attempts before it
    # erases itself, but nothing stops an attacker from resetting that
    # budget for free by starting a new ceremony each time; the aggregate
    # failure lockout closes that bypass.
    coordinator = _new_coordinator(tmp_path)
    assert coordinator._MAX_FAILURES_PER_WINDOW < coordinator._MAX_BEGINS_PER_WINDOW * 5
    ceremonies = coordinator._MAX_FAILURES_PER_WINDOW // 4
    assert ceremonies < coordinator._MAX_BEGINS_PER_WINDOW
    for ceremony_index in range(ceremonies):
        ceremony_id = _uuid7ish(ceremony_index)
        coordinator.begin(ceremony_id=ceremony_id, now=100)
        for attempt in range(4):
            with pytest.raises(ShoreProtocolError, match="pairing_failed"):
                coordinator.accept_browser_packet(_garbage_packet(ceremony_id, attempt), now=100)
    with pytest.raises(ShoreProtocolError, match="pairing_rate_limited"):
        coordinator.begin(ceremony_id=_uuid7ish(ceremonies), now=100)


def test_pairing_accept_is_rate_limited_once_aggregate_failure_budget_is_exhausted(tmp_path):
    # Regression test: the aggregate lockout must also be enforced inside
    # accept_browser_packet/accept_browser_confirmation, not only in begin().
    # Otherwise an attacker holding several already-open ceremonies (each
    # with its own untouched 5-attempt budget) can keep guessing without
    # ever calling begin() again, burning attempts past the aggregate cap.
    coordinator = _new_coordinator(tmp_path)
    ceremonies = coordinator._MAX_FAILURES_PER_WINDOW // 4
    still_open = _uuid7ish(0)
    for ceremony_index in range(ceremonies):
        ceremony_id = _uuid7ish(ceremony_index)
        coordinator.begin(ceremony_id=ceremony_id, now=100)
        for attempt in range(4):
            with pytest.raises(ShoreProtocolError, match="pairing_failed"):
                coordinator.accept_browser_packet(_garbage_packet(ceremony_id, attempt), now=100)
    # still_open has used only 4 of its own 5-attempt budget and needs no new
    # begin(); the aggregate 20-failure cap must still block it.
    with pytest.raises(ShoreProtocolError, match="pairing_rate_limited"):
        coordinator.accept_browser_packet(_garbage_packet(still_open, 99), now=100)
