"""Local, signed, tamper-evident log of the host's ADR-0040 dispatch outcomes.

Mirrors the relay's hash chain (shore/src/index.ts's `Audit`/`auditEntry`)
but each record is additionally Ed25519-signed by the host's own identity
key, so an attacker with only the local audit database can't produce a chain
that verifies against the host's already-pinned public key. Correlates to the
relay's chain via the shared `request_id` from the ADR-0040 envelope, per
docs/shore-security-operations.md's "Relay and host events use the same
request/transition ID and hash commitment."

Only ever stores a hash of the plaintext command, never the command or
response text itself (see shore-security-operations.md's exclusion list).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric import ed25519

from .shore_crypto import b64url, canonical, unb64url, uuid7
from .shore_receipt import RECEIPT_GENESIS_HASH, ReceiptVerificationError, VerifiedRelayReceipt

GENESIS_HASH = "sha256:" + "0" * 43


def _sha256_commitment(value: Any) -> str:
    return "sha256:" + b64url(hashlib.sha256(canonical(value)).digest())


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    reason: str | None = None
    seq: int | None = None


@dataclass(frozen=True)
class AuditExportBatch:
    object_name: str
    body: bytes
    through_seq: int
    through_hash: str


def _verify_signature(key: ed25519.Ed25519PublicKey | None, signature: str, signed: dict[str, Any]) -> bool:
    if key is None:
        return False
    try:
        key.verify(unb64url(signature), canonical(signed))
        return True
    except Exception:
        return False


def verify_chain(
    events: list[dict[str, Any]], keys: Mapping[int, ed25519.Ed25519PublicKey], *,
    expected_tip: dict[str, Any] | None = None,
) -> ChainVerification:
    """Replays a host audit chain: recomputes each event's hash and signature
    and checks seq/prevHash linkage, so deletion (a seq gap), insertion/forking
    (a duplicate or out-of-order seq), content mutation (a hash or signature
    that no longer matches), and a forged/unsigned event are all detectable.

    `keys` pins the verification key per `keyEpoch` rather than trusting one
    fixed key for the whole chain, since the signing key legitimately differs
    across a host key rotation (docs/plans/adr-0039-shore-remote-access.md
    Milestone 5's key-epoch finding) -- an event whose epoch isn't in `keys`
    fails closed as `unknown_key_epoch` rather than silently passing or being
    checked against the wrong key. `expected_tip`, if given, is compared
    against the chain's actual final event: without it, deleting the last
    row (or every row) of an otherwise-consistent chain would verify clean,
    since linkage alone has nothing to compare the visible tail against.
    """
    expected_seq = 1
    expected_prev_hash = GENESIS_HASH
    for event in events:
        if event["seq"] != expected_seq:
            reason = "sequence_fork" if event["seq"] < expected_seq else "sequence_gap"
            return ChainVerification(False, reason, event["seq"])
        if event["prevHash"] != expected_prev_hash:
            return ChainVerification(False, "chain_break", event["seq"])
        unsigned = {key: value for key, value in event.items() if key not in ("hash", "signature")}
        if _sha256_commitment(unsigned) != event["hash"]:
            return ChainVerification(False, "hash_mismatch", event["seq"])
        key = keys.get(event["keyEpoch"])
        if key is None:
            return ChainVerification(False, "unknown_key_epoch", event["seq"])
        if not _verify_signature(key, event["signature"], {**unsigned, "hash": event["hash"]}):
            return ChainVerification(False, "bad_signature", event["seq"])
        expected_seq += 1
        expected_prev_hash = event["hash"]
    if expected_tip is not None and (expected_tip["seq"] != expected_seq - 1 or expected_tip["hash"] != expected_prev_hash):
        return ChainVerification(False, "chain_break", expected_tip["seq"])
    return ChainVerification(True)


class ShoreAuditLog:
    """Append-only local store for the host's half of the correlated audit chain.

    Each `record()` call appends one new chained, signed event -- it never
    rewrites a prior one, so recording an authorization decision before
    dispatch and a separate outcome afterward (see `shore_transport.py`'s
    `_handle_envelope`) are two ordinary, independently verifiable events
    correlated by the same `request_id`, not an in-place update.
    """

    def __init__(self, path: Path, *, host_id: str, key_epoch: int, host_signing: ed25519.Ed25519PrivateKey):
        self.path = path
        self.host_id = host_id
        self.key_epoch = key_epoch
        self.host_signing = host_signing
        self._provisioned = False

    def _provision(self) -> None:
        if self._provisioned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self._provisioned = True

    def _connect(self) -> sqlite3.Connection:
        self._provision()
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS audit_chain ("
            "seq INTEGER PRIMARY KEY, id TEXT NOT NULL, request_id TEXT NOT NULL, device_id TEXT NOT NULL, "
            "host_id TEXT NOT NULL, key_epoch INTEGER NOT NULL, message_type TEXT NOT NULL, "
            "command_hash TEXT NOT NULL, decision TEXT NOT NULL, outcome TEXT NOT NULL, at_ms INTEGER NOT NULL, "
            "prev_hash TEXT NOT NULL, hash TEXT NOT NULL, signature TEXT NOT NULL)"
        )
        # A single-row checkpoint outside audit_chain, signed independently of
        # any individual event, so deleting the chain's last row(s) -- or the
        # whole table -- leaves behind a pinned tip that replay no longer
        # reaches, instead of an empty table quietly verifying as "clean".
        connection.execute(
            "CREATE TABLE IF NOT EXISTS audit_tip (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "seq INTEGER NOT NULL, hash TEXT NOT NULL, key_epoch INTEGER NOT NULL, signature TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS audit_export_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "seq INTEGER NOT NULL, hash TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS relay_receipts ("
            "seq INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, receipt_hash TEXT NOT NULL UNIQUE, "
            "prev_hash TEXT NOT NULL, receipt_epoch INTEGER NOT NULL, direction TEXT NOT NULL, "
            "envelope_hash TEXT NOT NULL, receipt_json BLOB NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS relay_receipt_tip (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "seq INTEGER NOT NULL, hash TEXT NOT NULL, receipt_epoch INTEGER NOT NULL)"
        )
        return connection

    def _sign_tip(self, seq: int, hash_: str) -> str:
        return b64url(self.host_signing.sign(canonical({"seq": seq, "hash": hash_, "keyEpoch": self.key_epoch})))

    @staticmethod
    def _stage_receipt(connection: sqlite3.Connection, relay_receipt: VerifiedRelayReceipt) -> bool:
        receipt = relay_receipt.receipt
        existing = connection.execute(
            "SELECT receipt_hash, envelope_hash, direction FROM relay_receipts WHERE request_id = ?",
            (receipt["request_id"],),
        ).fetchone()
        if existing is not None:
            if existing == (receipt["receipt_hash"], receipt["envelope_hash"], receipt["direction"]):
                return False
            raise ReceiptVerificationError("shore_receipt_conflict")
        tip = connection.execute("SELECT seq, hash FROM relay_receipt_tip WHERE id = 1").fetchone()
        if tip is None:
            if connection.execute("SELECT 1 FROM relay_receipts LIMIT 1").fetchone() is not None:
                raise ReceiptVerificationError("shore_audit_continuity_unavailable")
        else:
            stored_tip = connection.execute(
                "SELECT receipt_hash FROM relay_receipts WHERE seq = ?", (tip[0],),
            ).fetchone()
            if stored_tip != (tip[1],):
                raise ReceiptVerificationError("shore_audit_continuity_unavailable")
        expected_seq = (tip[0] if tip else 0) + 1
        expected_hash = tip[1] if tip else RECEIPT_GENESIS_HASH
        if relay_receipt.seq != expected_seq or receipt["prev_hash"] != expected_hash:
            code = (
                "shore_receipt_conflict" if relay_receipt.seq <= expected_seq
                else "shore_audit_continuity_unavailable"
            )
            raise ReceiptVerificationError(code)
        connection.execute(
            "INSERT INTO relay_receipts(seq, request_id, receipt_hash, prev_hash, receipt_epoch, "
            "direction, envelope_hash, receipt_json) VALUES (?,?,?,?,?,?,?,?)",
            (relay_receipt.seq, receipt["request_id"], receipt["receipt_hash"], receipt["prev_hash"],
             receipt["receipt_epoch"], receipt["direction"], receipt["envelope_hash"], canonical(receipt)),
        )
        connection.execute(
            "INSERT INTO relay_receipt_tip(id, seq, hash, receipt_epoch) VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, hash=excluded.hash, "
            "receipt_epoch=excluded.receipt_epoch",
            (relay_receipt.seq, receipt["receipt_hash"], receipt["receipt_epoch"]),
        )
        return True

    def accept_receipt(self, relay_receipt: VerifiedRelayReceipt) -> bool:
        """Persist a verified outbound acknowledgement without application dispatch."""
        with self._connect() as connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                inserted = self._stage_receipt(connection, relay_receipt)
                connection.execute("COMMIT")
                return inserted
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def has_receipt(self, receipt: dict[str, Any]) -> bool:
        """Return whether this exact canonical receipt is already durable."""
        request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
        if not isinstance(request_id, str):
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM relay_receipts WHERE request_id = ?", (request_id,),
            ).fetchone()
        return row is not None and bytes(row[0]) == canonical(receipt)

    def record(self, *, request_id: str, device_id: str, message_type: str, frame: dict[str, Any],
               decision: str, outcome: str, now_ms: int | None = None,
               relay_receipt: VerifiedRelayReceipt | None = None) -> dict[str, Any] | None:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        command_hash = _sha256_commitment(frame)
        with self._connect() as connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if relay_receipt is not None:
                    if not self._stage_receipt(connection, relay_receipt):
                        connection.execute("ROLLBACK")
                        return None
                row = connection.execute("SELECT seq, hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
                seq = (row[0] if row else 0) + 1
                prev_hash = row[1] if row else GENESIS_HASH
                unsigned = {
                    "id": uuid7(now_ms), "seq": seq, "prevHash": prev_hash, "requestId": request_id,
                    "deviceId": device_id, "hostId": self.host_id, "keyEpoch": self.key_epoch,
                    "messageType": message_type, "commandHash": command_hash, "decision": decision,
                    "outcome": outcome, "at": now_ms,
                }
                digest = _sha256_commitment(unsigned)
                signature = b64url(self.host_signing.sign(canonical({**unsigned, "hash": digest})))
                event = {**unsigned, "hash": digest, "signature": signature}
                connection.execute(
                    "INSERT INTO audit_chain(seq, id, request_id, device_id, host_id, key_epoch, message_type, "
                    "command_hash, decision, outcome, at_ms, prev_hash, hash, signature) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seq, event["id"], request_id, device_id, self.host_id, self.key_epoch, message_type,
                     command_hash, decision, outcome, now_ms, prev_hash, digest, signature),
                )
                connection.execute(
                    "INSERT INTO audit_tip(id, seq, hash, key_epoch, signature) VALUES (1,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, hash=excluded.hash, "
                    "key_epoch=excluded.key_epoch, signature=excluded.signature",
                    (seq, digest, self.key_epoch, self._sign_tip(seq, digest)),
                )
                connection.execute("COMMIT")
                return event
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, id, request_id, device_id, host_id, key_epoch, message_type, command_hash, "
                "decision, outcome, at_ms, prev_hash, hash, signature FROM audit_chain ORDER BY seq ASC"
            ).fetchall()
        return [
            {
                "seq": seq, "id": id_, "requestId": request_id, "deviceId": device_id, "hostId": host_id,
                "keyEpoch": key_epoch, "messageType": message_type, "commandHash": command_hash,
                "decision": decision, "outcome": outcome, "at": at_ms, "prevHash": prev_hash,
                "hash": hash_, "signature": signature,
            }
            for seq, id_, request_id, device_id, host_id, key_epoch, message_type, command_hash,
                decision, outcome, at_ms, prev_hash, hash_, signature in rows
        ]

    def pending_export(self, *, limit: int = 500) -> AuditExportBatch | None:
        """Build a retry-stable, signed batch without advancing the export cursor.

        The caller must upload ``body`` under ``object_name`` with create-only
        semantics, then call :meth:`mark_exported`. A failed or interrupted
        upload therefore leaves every row pending for the next attempt.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT seq FROM audit_export_state WHERE id = 1"
            ).fetchone()
            after_seq = cursor[0] if cursor else 0
            rows = connection.execute(
                "SELECT seq, id, request_id, device_id, host_id, key_epoch, message_type, command_hash, "
                "decision, outcome, at_ms, prev_hash, hash, signature FROM audit_chain "
                "WHERE seq > ? ORDER BY seq ASC LIMIT ?", (after_seq, limit),
            ).fetchall()
        if not rows:
            return None
        events = [
            {
                "seq": seq, "id": id_, "requestId": request_id, "deviceId": device_id,
                "hostId": host_id, "keyEpoch": key_epoch, "messageType": message_type,
                "commandHash": command_hash, "decision": decision, "outcome": outcome,
                "at": at_ms, "prevHash": prev_hash, "hash": hash_, "signature": signature,
            }
            for seq, id_, request_id, device_id, host_id, key_epoch, message_type, command_hash,
                decision, outcome, at_ms, prev_hash, hash_, signature in rows
        ]
        payload_hash = _sha256_commitment(events)
        unsigned_manifest = {
            "v": 1, "stream": "host", "fromSeq": events[0]["seq"], "throughSeq": events[-1]["seq"],
            "count": len(events), "priorHash": events[0]["prevHash"], "headHash": events[-1]["hash"],
            # Derived from durable input so a retry produces byte-identical
            # content for the same create-only object name.
            "payloadHash": payload_hash, "throughAt": events[-1]["at"], "hostId": self.host_id,
            "keyEpoch": self.key_epoch,
        }
        manifest = {
            **unsigned_manifest,
            "signature": b64url(self.host_signing.sign(canonical(unsigned_manifest))),
        }
        body = canonical({"manifest": manifest, "events": events})
        # Derived only from chain coordinates and commitments: retry-stable,
        # unique, and free of usernames, account names, or other user data.
        object_name = (
            f"host/events/{events[0]['seq']:020d}-{events[-1]['seq']:020d}-"
            f"{events[-1]['hash'].removeprefix('sha256:')}.json"
        )
        return AuditExportBatch(object_name, body, events[-1]["seq"], events[-1]["hash"])

    def mark_exported(self, batch: AuditExportBatch) -> None:
        """Advance the durable cursor only if the uploaded head still exists."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT hash FROM audit_chain WHERE seq = ?", (batch.through_seq,),
                ).fetchone()
                if row is None or row[0] != batch.through_hash:
                    raise ValueError("export batch no longer matches the local chain")
                cursor = connection.execute(
                    "SELECT seq FROM audit_export_state WHERE id = 1"
                ).fetchone()
                if cursor is not None and batch.through_seq < cursor[0]:
                    raise ValueError("export cursor cannot move backwards")
                connection.execute(
                    "INSERT INTO audit_export_state(id, seq, hash) VALUES (1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, hash=excluded.hash",
                    (batch.through_seq, batch.through_hash),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def export_lag_ms(self, *, now_ms: int | None = None) -> int:
        """Age of the oldest unexported event, or zero when caught up."""
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._connect() as connection:
            cursor = connection.execute("SELECT seq FROM audit_export_state WHERE id = 1").fetchone()
            row = connection.execute(
                "SELECT at_ms FROM audit_chain WHERE seq > ? ORDER BY seq ASC LIMIT 1",
                (cursor[0] if cursor else 0,),
            ).fetchone()
        return 0 if row is None else max(0, now_ms - row[0])

    def _tip(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT seq, hash, key_epoch, signature FROM audit_tip WHERE id = 1").fetchone()
        if row is None:
            return None
        seq, hash_, key_epoch, signature = row
        return {"seq": seq, "hash": hash_, "keyEpoch": key_epoch, "signature": signature}

    def verify(self, keys: Mapping[int, ed25519.Ed25519PublicKey]) -> ChainVerification:
        tip = self._tip()
        if tip is not None:
            key = keys.get(tip["keyEpoch"])
            if key is None:
                return ChainVerification(False, "unknown_key_epoch", tip["seq"])
            if not _verify_signature(key, tip["signature"], {"seq": tip["seq"], "hash": tip["hash"], "keyEpoch": tip["keyEpoch"]}):
                return ChainVerification(False, "bad_signature", tip["seq"])
        expected_tip = {"seq": tip["seq"], "hash": tip["hash"]} if tip is not None else None
        return verify_chain(self.events(), keys, expected_tip=expected_tip)
