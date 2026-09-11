import hashlib
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from agent.shore_audit import GENESIS_HASH, ShoreAuditLog, verify_chain
from agent.shore_capabilities import ShoreProtocolError
from agent.shore_crypto import b64url, canonical, unb64url
from agent.shore_receipt import (
    RECEIPT_GENESIS_HASH, ReceiptVerificationError, envelope_commitment,
    verify_relay_receipt,
)
from agent.shore_transport import ShoreChannel

from .test_shore_transport import ACCOUNT, DEVICE, HOST, NOW, browser_frame, pair

HOST_ID = "018f1f24-e9ec-7f12-b20a-67fc03679f32"
REQUEST_ID = "018f1f25-c930-76f0-86e7-0000000000a1"
KEY_EPOCH = 1


def _log(tmp_path, signing=None, key_epoch=KEY_EPOCH):
    signing = signing or ed25519.Ed25519PrivateKey.generate()
    return ShoreAuditLog(tmp_path / "audit.sqlite3", host_id=HOST_ID, key_epoch=key_epoch, host_signing=signing), signing


def _keys(signing, epoch=KEY_EPOCH):
    return {epoch: signing.public_key()}


def _receipt(signing, *, seq, prev_hash, request_id, epoch=1):
    envelope = f"envelope-{seq}".encode()
    fields = {
        "v": 1, "type": "relay_receipt", "host_id": HOST_ID,
        "request_id": request_id, "direction": "browser_to_host",
        "disposition": "accepted", "receipt_epoch": epoch, "seq": str(seq),
        "prev_hash": prev_hash, "envelope_hash": envelope_commitment(envelope),
    }
    receipt_hash = b64url(hashlib.sha256(canonical(fields)).digest())
    receipt = {
        **fields, "receipt_hash": receipt_hash,
        "signature": b64url(signing.sign(canonical({**fields, "receipt_hash": receipt_hash}))),
    }
    return verify_relay_receipt(
        receipt, envelope, host_id=HOST_ID, direction="browser_to_host",
        keys={epoch: signing.public_key()},
    )


def test_records_chain_recomputably_and_verifies_clean(tmp_path):
    log, signing = _log(tmp_path)
    first = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
                        frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    second = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                         frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW + 1)
    assert first["seq"] == 1 and first["prevHash"] == GENESIS_HASH and first["keyEpoch"] == KEY_EPOCH
    assert second["seq"] == 2 and second["prevHash"] == first["hash"]
    result = log.verify(_keys(signing))
    assert result.valid is True


def test_export_batches_are_signed_retry_stable_and_advance_only_after_ack(tmp_path):
    log, signing = _log(tmp_path)
    for offset in range(3):
        log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                   frame={"v": 1, "type": "ping", "payload": {"offset": offset}},
                   decision="granted", outcome="ok", now_ms=NOW + offset)

    first = log.pending_export(limit=2)
    assert first is not None
    retry = log.pending_export(limit=2)
    assert retry == first
    document = json.loads(first.body)
    manifest = document["manifest"]
    signature = manifest.pop("signature")
    signing.public_key().verify(unb64url(signature), canonical(manifest))
    assert manifest["count"] == 2 and manifest["throughSeq"] == 2
    assert manifest["payloadHash"] == "sha256:" + b64url(hashlib.sha256(canonical(document["events"])).digest())
    assert "offset" not in first.body.decode()

    log.mark_exported(first)
    second = log.pending_export(limit=2)
    assert second is not None and second.through_seq == 3
    log.mark_exported(second)
    assert log.pending_export() is None


def test_export_cursor_rejects_a_stale_or_mismatched_ack(tmp_path):
    log, _ = _log(tmp_path)
    for offset in range(2):
        log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                   frame={"v": 1, "type": "ping", "payload": {}},
                   decision="granted", outcome="ok", now_ms=NOW + offset)
    first = log.pending_export(limit=1)
    assert first is not None
    log.mark_exported(first)
    second = log.pending_export(limit=1)
    assert second is not None
    log.mark_exported(second)
    with pytest.raises(ValueError, match="backwards"):
        log.mark_exported(first)


def test_fresh_or_missing_receipt_checkpoint_rejects_a_non_genesis_shore_tip(tmp_path):
    log, _ = _log(tmp_path)
    shore_signing = ed25519.Ed25519PrivateKey.generate()
    first = _receipt(
        shore_signing, seq=1, prev_hash=RECEIPT_GENESIS_HASH,
        request_id=REQUEST_ID,
    )
    second = _receipt(
        shore_signing, seq=2, prev_hash=first.receipt["receipt_hash"],
        request_id="018f1f25-c930-76f0-86e7-0000000000a2",
    )

    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        log.accept_receipt(second)


def test_restored_receipt_database_cannot_silently_adopt_the_current_shore_tip(tmp_path):
    log, _ = _log(tmp_path)
    shore_signing = ed25519.Ed25519PrivateKey.generate()
    first = _receipt(
        shore_signing, seq=1, prev_hash=RECEIPT_GENESIS_HASH,
        request_id=REQUEST_ID,
    )
    second = _receipt(
        shore_signing, seq=2, prev_hash=first.receipt["receipt_hash"],
        request_id="018f1f25-c930-76f0-86e7-0000000000a2",
    )
    third = _receipt(
        shore_signing, seq=3, prev_hash=second.receipt["receipt_hash"],
        request_id="018f1f25-c930-76f0-86e7-0000000000a3",
    )
    assert log.accept_receipt(first) and log.accept_receipt(second)

    # Model restoring a coherent older backup: both its receipt row and tip
    # stop at sequence 1 while Shore's durable chain has already reached 2.
    with sqlite3.connect(log.path) as connection:
        connection.execute("DELETE FROM relay_receipts WHERE seq > 1")
        connection.execute(
            "UPDATE relay_receipt_tip SET seq = 1, hash = ?, receipt_epoch = 1 WHERE id = 1",
            (first.receipt["receipt_hash"],),
        )

    with pytest.raises(ReceiptVerificationError, match="shore_audit_continuity_unavailable"):
        log.accept_receipt(third)


def test_receipt_key_rotation_preserves_old_and_new_epoch_evidence(tmp_path):
    log, _ = _log(tmp_path)
    old_signing = ed25519.Ed25519PrivateKey.generate()
    new_signing = ed25519.Ed25519PrivateKey.generate()
    first = _receipt(
        old_signing, seq=1, prev_hash=RECEIPT_GENESIS_HASH,
        request_id=REQUEST_ID, epoch=1,
    )
    second = _receipt(
        new_signing, seq=2, prev_hash=first.receipt["receipt_hash"],
        request_id="018f1f25-c930-76f0-86e7-0000000000a2", epoch=2,
    )
    assert log.accept_receipt(first) and log.accept_receipt(second)

    with sqlite3.connect(log.path) as connection:
        epochs = connection.execute(
            "SELECT seq, receipt_epoch FROM relay_receipts ORDER BY seq"
        ).fetchall()
        tip = connection.execute(
            "SELECT seq, hash, receipt_epoch FROM relay_receipt_tip WHERE id = 1"
        ).fetchone()
    assert epochs == [(1, 1), (2, 2)]
    assert tip == (2, second.receipt["receipt_hash"], 2)


def test_detects_deletion_of_a_middle_event_as_a_sequence_gap(tmp_path):
    log, signing = _log(tmp_path)
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
               frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
               frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW + 1)
    third = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                        frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW + 2)
    with sqlite3.connect(log.path) as connection:
        connection.execute("DELETE FROM audit_chain WHERE seq = 2")
    result = log.verify(_keys(signing))
    assert result.valid is False and result.reason == "sequence_gap" and result.seq == third["seq"]


def test_detects_deletion_of_the_final_event_via_the_persisted_tip(tmp_path):
    # Deleting only the last row leaves the remaining rows perfectly linked
    # to each other -- without an independent, separately-signed checkpoint
    # of what the tip should be, replaying just the survivors would verify
    # as a "clean" (shorter) chain instead of catching the missing tail.
    log, signing = _log(tmp_path)
    first = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
                        frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    second = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                         frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW + 1)
    with sqlite3.connect(log.path) as connection:
        connection.execute("DELETE FROM audit_chain WHERE seq = 2")
    result = log.verify(_keys(signing))
    assert result.valid is False and result.reason == "chain_break" and result.seq == second["seq"]
    # verify_chain alone (no expected_tip) has nothing to compare against and
    # correctly reports the truncated chain as internally consistent -- the
    # tip check, not linkage, is what catches this.
    assert verify_chain([first], _keys(signing)).valid is True


def test_detects_deletion_of_every_event_via_the_persisted_tip(tmp_path):
    log, signing = _log(tmp_path)
    only = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
                       frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    with sqlite3.connect(log.path) as connection:
        connection.execute("DELETE FROM audit_chain")
    result = log.verify(_keys(signing))
    assert result.valid is False and result.reason == "chain_break" and result.seq == only["seq"]


def test_detects_mutation_of_an_events_own_content(tmp_path):
    log, signing = _log(tmp_path)
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
               frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    with sqlite3.connect(log.path) as connection:
        connection.execute("UPDATE audit_chain SET outcome = 'forged' WHERE seq = 1")
    result = log.verify(_keys(signing))
    assert result.valid is False and result.reason == "hash_mismatch" and result.seq == 1


def test_detects_a_forged_signature_over_otherwise_consistent_content(tmp_path):
    log, signing = _log(tmp_path)
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
               frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    attacker_signing = ed25519.Ed25519PrivateKey.generate()
    events = log.events()
    unsigned = {key: value for key, value in events[0].items() if key not in ("hash", "signature")}
    forged_signature = b64url(attacker_signing.sign(canonical({**unsigned, "hash": events[0]["hash"]})))
    with sqlite3.connect(log.path) as connection:
        connection.execute("UPDATE audit_chain SET signature = ? WHERE seq = 1", (forged_signature,))
    result = log.verify(_keys(signing))
    assert result.valid is False and result.reason == "bad_signature" and result.seq == 1


def test_detects_a_forked_sequence_number(tmp_path):
    # seq is the SQLite primary key, so ShoreAuditLog.record() can never write
    # two rows at the same seq -- exercise verify_chain's fork detection
    # directly against a hand-built pair of otherwise self-consistent events,
    # as would only be reachable via raw file-level tampering bypassing SQLite
    # entirely, or another host chain implementation sharing this format.
    log, signing = _log(tmp_path)
    first = log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
                        frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)

    def branch(message_type):
        unsigned = {"id": f"018f1f25-c930-76f0-86e7-{message_type[:8]:0<12}", "seq": 2, "prevHash": first["hash"],
                    "requestId": REQUEST_ID, "deviceId": DEVICE, "hostId": HOST_ID, "keyEpoch": KEY_EPOCH,
                    "messageType": message_type, "commandHash": "sha256:" + "1" * 43, "decision": "granted",
                    "outcome": "ok", "at": NOW + 5}
        digest = "sha256:" + b64url(hashlib.sha256(canonical(unsigned)).digest())
        signed = {**unsigned, "hash": digest}
        return {**signed, "signature": b64url(signing.sign(canonical(signed)))}

    result = verify_chain([first, branch("ping"), branch("pong")], _keys(signing))
    assert result.valid is False and result.reason == "sequence_fork" and result.seq == 2


def test_verification_fails_closed_for_an_event_signed_under_an_unpinned_epoch(tmp_path):
    log, signing = _log(tmp_path, key_epoch=2)
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
               frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    # A verifier that only has epoch 1's key pinned (e.g. hasn't learned about
    # a host key rotation yet) must fail closed, not silently skip or
    # misattribute an event signed under a different epoch.
    other_epoch_key = ed25519.Ed25519PrivateKey.generate().public_key()
    result = log.verify({1: other_epoch_key})
    assert result.valid is False and result.reason == "unknown_key_epoch" and result.seq == 1


def test_a_key_rotation_across_the_same_log_verifies_against_a_pinned_key_history(tmp_path):
    # Reusing one audit.sqlite3 across a host key rotation (a fresh
    # ShoreChannel with a new host_signing key but the same state_dir) must
    # not require -- or silently accept -- one fixed key for the whole log.
    path = tmp_path / "audit.sqlite3"
    epoch1_signing = ed25519.Ed25519PrivateKey.generate()
    epoch2_signing = ed25519.Ed25519PrivateKey.generate()
    log1 = ShoreAuditLog(path, host_id=HOST_ID, key_epoch=1, host_signing=epoch1_signing)
    log1.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="subscribe",
                frame={"v": 1, "type": "subscribe", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW)
    log2 = ShoreAuditLog(path, host_id=HOST_ID, key_epoch=2, host_signing=epoch2_signing)
    log2.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
                frame={"v": 1, "type": "ping", "payload": {}}, decision="granted", outcome="ok", now_ms=NOW + 1)

    result = log2.verify({1: epoch1_signing.public_key(), 2: epoch2_signing.public_key()})
    assert result.valid is True
    # Missing the epoch-2 key: the epoch-1 event still checks out, but the
    # epoch-2 event fails closed instead of being checked against the wrong key.
    partial = log2.verify({1: epoch1_signing.public_key()})
    assert partial.valid is False and partial.reason == "unknown_key_epoch" and partial.seq == 2


@pytest.mark.asyncio
async def test_dispatch_records_a_pre_dispatch_authorization_and_a_separate_outcome(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
                            host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)

    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}]})
    await channel.handle(canonical(request), now_ms=NOW)

    denied = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 2, "chat.start", {})
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        await channel.handle(canonical(denied), now_ms=NOW)

    events = channel.audit.events()
    # subscribe: a pre-dispatch "pending" authorization record, then a
    # separate post-dispatch "ok" outcome record -- two chained events, not
    # one record mutated in place. chat.start: denied before dispatch ever runs.
    assert [(event["decision"], event["outcome"]) for event in events] == [
        ("granted", "pending"), ("granted", "ok"), ("denied", "denied:shore_capability_denied"),
    ]
    assert events[0]["requestId"] == events[1]["requestId"] == request["request_id"]
    assert events[0]["messageType"] == events[1]["messageType"] == "subscribe"
    assert events[2]["messageType"] == "chat.start"
    assert events[0]["deviceId"] == DEVICE and events[0]["hostId"] == HOST and events[0]["keyEpoch"] == 1
    # The command's own content is never stored, only a fixed-shape record
    # committing to it -- no "payload"/"scopes"/frame content field exists at all.
    expected_fields = {"seq", "id", "requestId", "deviceId", "hostId", "keyEpoch", "messageType",
                        "commandHash", "decision", "outcome", "at", "prevHash", "hash", "signature"}
    for event in events:
        assert set(event) == expected_fields
    result = channel.audit.verify(_keys(host_signing))
    assert result.valid is True
