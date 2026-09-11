"""Verification and durable continuity state for Shore relay receipts."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric import ed25519

from .shore_crypto import b64url, canonical, unb64url

RECEIPT_GENESIS_HASH = "0" * 43
_FIELDS = {
    "v", "type", "host_id", "request_id", "direction", "disposition",
    "receipt_epoch", "seq", "prev_hash", "envelope_hash",
}
_RECEIPT_KEYS = _FIELDS | {"receipt_hash", "signature"}
_HASH_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_RECEIPT_SEQ = (1 << 63) - 1


@dataclass(frozen=True)
class VerifiedRelayReceipt:
    receipt: dict[str, Any]
    seq: int


class ReceiptVerificationError(Exception):
    """A receipt is invalid or cannot extend the locally trusted chain."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def envelope_commitment(envelope: bytes) -> str:
    return "sha256:" + b64url(hashlib.sha256(envelope).digest())


def _canonical_uuid7(value: Any) -> bool:
    try:
        parsed = uuid.UUID(value)
        return isinstance(value, str) and str(parsed) == value and parsed.version == 7
    except (AttributeError, TypeError, ValueError):
        return False


def verify_relay_receipt(
    receipt: Any, envelope: bytes, *, host_id: str, direction: str,
    keys: Mapping[int, ed25519.Ed25519PublicKey],
) -> VerifiedRelayReceipt:
    unavailable = "shore_audit_continuity_unavailable"
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise ReceiptVerificationError(unavailable)
    fields = {key: receipt[key] for key in _FIELDS}
    seq_text = receipt.get("seq")
    if (
        receipt.get("v") != 1 or receipt.get("type") != "relay_receipt"
        or not _canonical_uuid7(receipt.get("host_id"))
        or not _canonical_uuid7(receipt.get("request_id"))
        or receipt.get("disposition") != "accepted"
        or not isinstance(receipt.get("receipt_epoch"), int)
        or isinstance(receipt.get("receipt_epoch"), bool) or receipt["receipt_epoch"] < 1
        or not isinstance(seq_text, str) or not seq_text.isascii() or not seq_text.isdigit()
        or seq_text.startswith("0") or len(seq_text) > 19
        or not isinstance(receipt.get("prev_hash"), str) or not _HASH_RE.fullmatch(receipt["prev_hash"])
        or not isinstance(receipt.get("envelope_hash"), str)
        or not receipt["envelope_hash"].startswith("sha256:")
        or not _HASH_RE.fullmatch(receipt["envelope_hash"][7:])
        or not isinstance(receipt.get("receipt_hash"), str) or not _HASH_RE.fullmatch(receipt["receipt_hash"])
        or not isinstance(receipt.get("signature"), str)
    ):
        raise ReceiptVerificationError(unavailable)
    seq = int(seq_text)
    if seq > _MAX_RECEIPT_SEQ:
        raise ReceiptVerificationError(unavailable)
    expected_hash = b64url(hashlib.sha256(canonical(fields)).digest())
    if receipt["receipt_hash"] != expected_hash:
        raise ReceiptVerificationError(unavailable)
    key = keys.get(receipt["receipt_epoch"])
    if key is None:
        raise ReceiptVerificationError(unavailable)
    try:
        signature = unb64url(receipt["signature"])
        if len(signature) != 64 or b64url(signature) != receipt["signature"]:
            raise ValueError
        key.verify(signature, canonical({**fields, "receipt_hash": expected_hash}))
    except Exception as exc:
        raise ReceiptVerificationError(unavailable) from exc
    # Only authenticated contradictory evidence is a confirmed conflict.
    if (
        receipt["host_id"] != host_id or receipt["direction"] != direction
        or receipt["envelope_hash"] != envelope_commitment(envelope)
    ):
        raise ReceiptVerificationError("shore_receipt_conflict")
    return VerifiedRelayReceipt(dict(receipt), seq)
