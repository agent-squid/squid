"""Build and verify signed daily correlation manifests for Shore audit streams."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric import ed25519

from .shore_audit import GENESIS_HASH, ChainVerification, verify_chain
from .shore_crypto import b64url, canonical, unb64url

BROKER_GENESIS_HASH = "0" * 43
MANIFEST_GENESIS_HASH = "sha256:" + "0" * 43


def _manifest_hash(manifest: dict[str, Any]) -> str:
    return "sha256:" + b64url(hashlib.sha256(canonical(manifest)).digest())


def _heads_do_not_regress(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    for stream in ("broker", "host"):
        old, new = previous[stream], current[stream]
        if new["headSeq"] < old["headSeq"]:
            return False
        if new["headSeq"] == old["headSeq"] and new["headHash"] != old["headHash"]:
            return False
    return True


def _chains_extend_previous(
    previous: dict[str, Any], broker_events: list[dict[str, Any]], host_events: list[dict[str, Any]],
) -> bool:
    for stream, events, genesis in (
        ("broker", broker_events, BROKER_GENESIS_HASH),
        ("host", host_events, GENESIS_HASH),
    ):
        head = previous[stream]
        seq, hash_ = head["headSeq"], head["headHash"]
        if seq == 0:
            if hash_ != genesis:
                return False
        elif seq > len(events) or events[seq - 1].get("seq") != seq or events[seq - 1].get("hash") != hash_:
            return False
    return True


def _valid_manifest_signature(manifest: dict[str, Any], key: ed25519.Ed25519PublicKey) -> bool:
    try:
        signature = manifest["signature"]
        unsigned = {name: value for name, value in manifest.items() if name != "signature"}
        key.verify(unb64url(signature), canonical(unsigned))
        return True
    except Exception:
        return False


def verify_broker_chain(events: list[dict[str, Any]], expected_tip: dict[str, Any]) -> ChainVerification:
    expected_seq, previous = 1, BROKER_GENESIS_HASH
    for event in events:
        seq = event.get("seq")
        if seq != expected_seq:
            return ChainVerification(False, "sequence_fork" if isinstance(seq, int) and seq < expected_seq else "sequence_gap", seq)
        if event.get("prevHash") != previous:
            return ChainVerification(False, "chain_break", seq)
        unsigned = {key: value for key, value in event.items() if key != "hash"}
        digest = b64url(hashlib.sha256(canonical(unsigned)).digest())
        if event.get("hash") != digest:
            return ChainVerification(False, "hash_mismatch", seq)
        expected_seq, previous = expected_seq + 1, digest
    if expected_tip.get("seq") != expected_seq - 1 or expected_tip.get("hash") != previous:
        return ChainVerification(False, "chain_break", expected_tip.get("seq"))
    return ChainVerification(True)


def build_daily_manifest(
    *, day: str, broker_events: list[dict[str, Any]], broker_tip: dict[str, Any],
    host_events: list[dict[str, Any]], host_tip: dict[str, Any],
    host_keys: Mapping[int, ed25519.Ed25519PublicKey],
    manifest_signing: ed25519.Ed25519PrivateKey, generated_at: int | None = None,
    previous_manifest: dict[str, Any] | None = None, genesis: bool = False,
) -> dict[str, Any]:
    """Validate both streams and sign their heads plus request-ID correlation gaps."""
    if genesis == (previous_manifest is not None):
        raise ValueError("provide exactly one of genesis=True or previous_manifest")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("day must be YYYY-MM-DD")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError as exc:
        raise ValueError("day must be a valid YYYY-MM-DD date") from exc
    broker_status = verify_broker_chain(broker_events, broker_tip)
    if not broker_status.valid:
        raise ValueError(f"invalid broker chain: {broker_status.reason}:{broker_status.seq}")
    host_status = verify_chain(host_events, host_keys, expected_tip=host_tip)
    if not host_status.valid:
        raise ValueError(f"invalid host chain: {host_status.reason}:{host_status.seq}")

    broker_requests = {
        event["requestId"] for event in broker_events
        if event.get("type") == "relay_frame_outcome"
        and event.get("direction") == "browser_to_host"
        and event.get("outcome") == "forwarded"
        and isinstance(event.get("requestId"), str)
    }
    host_requests = {event["requestId"] for event in host_events if isinstance(event.get("requestId"), str)}
    missing_host = sorted(broker_requests - host_requests)
    missing_broker = sorted(host_requests - broker_requests)
    heads = {
        "broker": {"eventCount": len(broker_events), "headSeq": broker_tip["seq"], "headHash": broker_tip["hash"]},
        "host": {"eventCount": len(host_events), "headSeq": host_tip["seq"], "headHash": host_tip["hash"]},
    }
    if previous_manifest is not None:
        if not _valid_manifest_signature(previous_manifest, manifest_signing.public_key()):
            raise ValueError("invalid previous manifest")
        if previous_manifest["day"] >= day:
            raise ValueError("manifest day must advance")
        if not _heads_do_not_regress(previous_manifest, heads):
            raise ValueError("manifest chain head regressed")
        if not _chains_extend_previous(previous_manifest, broker_events, host_events):
            raise ValueError("audit chain does not extend previous manifest")
    unsigned = {
        "v": 1, "day": day,
        "generatedAt": int(time.time() * 1000) if generated_at is None else generated_at,
        **heads,
        "previousManifestHash": _manifest_hash(previous_manifest) if previous_manifest is not None else MANIFEST_GENESIS_HASH,
        "correlation": {
            "brokerRequestCount": len(broker_requests), "hostRequestCount": len(host_requests),
            "matchedCount": len(broker_requests & host_requests),
            "missingHostRequestIds": missing_host, "missingBrokerRequestIds": missing_broker,
        },
    }
    return {**unsigned, "signature": b64url(manifest_signing.sign(canonical(unsigned)))}


def verify_daily_manifest(
    manifest: dict[str, Any], key: ed25519.Ed25519PublicKey,
    previous_manifest: dict[str, Any] | None = None, *, genesis: bool = False,
) -> bool:
    try:
        if genesis == (previous_manifest is not None):
            return False
        if not _valid_manifest_signature(manifest, key):
            return False
        expected_previous = _manifest_hash(previous_manifest) if previous_manifest is not None else MANIFEST_GENESIS_HASH
        if manifest.get("previousManifestHash") != expected_previous:
            return False
        if previous_manifest is not None:
            if not _valid_manifest_signature(previous_manifest, key):
                return False
            if previous_manifest["day"] >= manifest["day"] or not _heads_do_not_regress(previous_manifest, manifest):
                return False
        return True
    except Exception:
        return False
