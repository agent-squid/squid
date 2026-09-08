import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from agent.shore_audit import GENESIS_HASH, ShoreAuditLog
from agent.shore_audit_manifest import (
    BROKER_GENESIS_HASH, build_daily_manifest, verify_broker_chain, verify_daily_manifest,
)
from agent.shore_crypto import b64url, canonical

from .test_shore_audit import HOST_ID, REQUEST_ID
from .test_shore_transport import DEVICE, NOW


def _broker_event(request_id=REQUEST_ID):
    unsigned = {
        "id": "018f1f25-c930-76f0-86e7-0000000000b1", "type": "relay_frame_outcome",
        "at": NOW, "seq": 1, "prevHash": BROKER_GENESIS_HASH, "requestId": request_id,
        "hostId": HOST_ID, "deviceId": DEVICE, "direction": "browser_to_host",
        "ciphertextHash": "A" * 43, "outcome": "forwarded",
    }
    return {**unsigned, "hash": b64url(hashlib.sha256(canonical(unsigned)).digest())}


def _host_event(tmp_path, request_id=REQUEST_ID):
    signing = ed25519.Ed25519PrivateKey.generate()
    log = ShoreAuditLog(tmp_path / "audit.sqlite3", host_id=HOST_ID, key_epoch=1, host_signing=signing)
    event = log.record(request_id=request_id, device_id=DEVICE, message_type="ping",
                       frame={"v": 1, "type": "ping", "payload": {}},
                       decision="granted", outcome="ok", now_ms=NOW)
    return event, signing


def test_builds_signed_manifest_with_matching_chain_heads(tmp_path):
    broker = _broker_event()
    host, host_signing = _host_event(tmp_path)
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    manifest = build_daily_manifest(
        day="2026-09-08", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
        host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
        host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing, generated_at=NOW,
        genesis=True,
    )
    assert manifest["correlation"] == {
        "brokerRequestCount": 1, "hostRequestCount": 1, "matchedCount": 1,
        "missingHostRequestIds": [], "missingBrokerRequestIds": [],
    }
    assert verify_daily_manifest(manifest, manifest_signing.public_key(), genesis=True)


def test_manifest_surfaces_missing_correlation_and_rejects_tampering(tmp_path):
    broker = _broker_event("018f1f25-c930-76f0-86e7-0000000000b2")
    host, host_signing = _host_event(tmp_path)
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    manifest = build_daily_manifest(
        day="2026-09-08", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
        host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
        host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing,
        genesis=True,
    )
    assert manifest["correlation"]["missingHostRequestIds"] == [broker["requestId"]]
    assert manifest["correlation"]["missingBrokerRequestIds"] == [host["requestId"]]
    manifest["correlation"]["matchedCount"] = 99
    assert not verify_daily_manifest(manifest, manifest_signing.public_key(), genesis=True)


def test_manifest_does_not_expect_host_evidence_for_an_unforwarded_frame():
    broker = _broker_event()
    unsigned = {**{key: value for key, value in broker.items() if key != "hash"}, "outcome": "peer_offline"}
    broker = {**unsigned, "hash": b64url(hashlib.sha256(canonical(unsigned)).digest())}
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    manifest = build_daily_manifest(
        day="2026-09-08", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
        host_events=[], host_tip={"seq": 0, "hash": GENESIS_HASH}, host_keys={},
        manifest_signing=manifest_signing, genesis=True,
    )
    assert manifest["correlation"] == {
        "brokerRequestCount": 0, "hostRequestCount": 0, "matchedCount": 0,
        "missingHostRequestIds": [], "missingBrokerRequestIds": [],
    }


def test_manifest_rejects_an_impossible_calendar_day(tmp_path):
    broker = _broker_event()
    host, host_signing = _host_event(tmp_path)
    with pytest.raises(ValueError, match="valid YYYY-MM-DD"):
        build_daily_manifest(
            day="2026-02-30", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
            host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
            host_keys={1: host_signing.public_key()}, manifest_signing=ed25519.Ed25519PrivateKey.generate(),
            genesis=True,
        )


def test_manifest_requires_explicit_genesis_or_predecessor(tmp_path):
    broker = _broker_event()
    host, host_signing = _host_event(tmp_path)
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    arguments = {
        "day": "2026-09-08", "broker_events": [broker],
        "broker_tip": {"seq": 1, "hash": broker["hash"]}, "host_events": [host],
        "host_tip": {"seq": 1, "hash": host["hash"]},
        "host_keys": {1: host_signing.public_key()}, "manifest_signing": manifest_signing,
    }
    with pytest.raises(ValueError, match="exactly one"):
        build_daily_manifest(**arguments)

    manifest = build_daily_manifest(**arguments, genesis=True)
    assert not verify_daily_manifest(manifest, manifest_signing.public_key())
    assert verify_daily_manifest(manifest, manifest_signing.public_key(), genesis=True)


def test_refuses_invalid_broker_or_forged_host_chain(tmp_path):
    broker = _broker_event()
    host, host_signing = _host_event(tmp_path)
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    forged = {**host, "outcome": "forged"}
    with pytest.raises(ValueError, match="invalid host chain"):
        build_daily_manifest(
            day="2026-09-08", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
            host_events=[forged], host_tip={"seq": 1, "hash": host["hash"]},
            host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing,
            genesis=True,
        )
    assert not verify_broker_chain([{**broker, "outcome": "forged"}], {"seq": 1, "hash": broker["hash"]}).valid


def test_manifest_chains_to_prior_signed_heads_and_refuses_rollback(tmp_path):
    broker = _broker_event()
    host, host_signing = _host_event(tmp_path)
    manifest_signing = ed25519.Ed25519PrivateKey.generate()
    previous = build_daily_manifest(
        day="2026-09-08", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
        host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
        host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing,
        genesis=True,
    )
    current = build_daily_manifest(
        day="2026-09-09", broker_events=[broker], broker_tip={"seq": 1, "hash": broker["hash"]},
        host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
        host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing,
        previous_manifest=previous,
    )
    assert verify_daily_manifest(current, manifest_signing.public_key(), previous)
    assert not verify_daily_manifest(current, manifest_signing.public_key(), {**previous, "day": "2026-09-07"})

    with pytest.raises(ValueError, match="head regressed"):
        build_daily_manifest(
            day="2026-09-09", broker_events=[], broker_tip={"seq": 0, "hash": BROKER_GENESIS_HASH},
            host_events=[], host_tip={"seq": 0, "hash": GENESIS_HASH}, host_keys={},
            manifest_signing=manifest_signing, previous_manifest=previous,
        )

    alternate_first = _broker_event("018f1f25-c930-76f0-86e7-0000000000c1")
    second_unsigned = {
        **{key: value for key, value in alternate_first.items() if key != "hash"},
        "id": "018f1f25-c930-76f0-86e7-0000000000c2", "seq": 2,
        "prevHash": alternate_first["hash"], "requestId": "018f1f25-c930-76f0-86e7-0000000000c2",
    }
    alternate_second = {**second_unsigned, "hash": b64url(hashlib.sha256(canonical(second_unsigned)).digest())}
    with pytest.raises(ValueError, match="does not extend"):
        build_daily_manifest(
            day="2026-09-09", broker_events=[alternate_first, alternate_second],
            broker_tip={"seq": 2, "hash": alternate_second["hash"]},
            host_events=[host], host_tip={"seq": 1, "hash": host["hash"]},
            host_keys={1: host_signing.public_key()}, manifest_signing=manifest_signing,
            previous_manifest=previous,
        )
