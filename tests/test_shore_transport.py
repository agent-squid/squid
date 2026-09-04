import json
import stat
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.shore_crypto import (
    ReplayStore, ShoreProtocolError, b64url, canonical, crockford32_decode,
    derive_pair_bootstrap_key, derive_pair_key, fingerprint, open_envelope,
    pairing_finished, seal_envelope, unb64url,
)
from agent.shore_transport import ShoreChannel

ACCOUNT = "018f1f25-3f6b-7d75-a4d1-62d771381b20"
HOST = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
DEVICE = "018f1f25-8614-7e41-8c5c-fc0b6eefad62"
CEREMONY = "018f1f25-c930-76f0-86e7-cb06d94e6a32"
NOW = int(datetime(2026, 9, 3, 12, tzinfo=timezone.utc).timestamp() * 1000)


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
