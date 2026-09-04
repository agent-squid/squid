"""Runtime boundary for Shore pairing and encrypted transport probes.

This deliberately does not dispatch ADR-0040 commands.  It is the narrow
Milestone-3 transport slice used to prove the authenticated encrypted channel.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .shore_crypto import (
    DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    TrustedDevice, canonical, open_envelope, seal_envelope, uuid7,
)


class ShoreChannel:
    """Processes opaque relay payloads after WebSocket authentication."""

    def __init__(self, state_dir: Path, *, account_id: str, host_id: str,
                 host_signing: ed25519.Ed25519PrivateKey,
                 host_agreement: x25519.X25519PrivateKey, key_epoch: int = 1):
        self.state_dir = state_dir
        self.account_id = account_id
        self.host_id = host_id
        self.host_signing = host_signing
        self.host_agreement = host_agreement
        self.key_epoch = key_epoch
        self.trust = DeviceTrustStore(state_dir / "devices.sqlite3")
        self.replay = ReplayStore(state_dir / "replay.sqlite3")
        self.pairing = PairingCoordinator(
            self.trust, account_id=account_id, host_id=host_id,
            host_signing_key=host_signing.public_key(),
            host_agreement_key=host_agreement.public_key(), key_epoch=key_epoch,
        )

    def begin_pairing(self, ceremony_id: str) -> dict[str, Any]:
        return self.pairing.begin(ceremony_id=ceremony_id)

    def handle(self, payload: bytes, *, now_ms: int | None = None) -> bytes | None:
        """Handle one binary relay frame; malformed input always fails closed."""
        try:
            value = json.loads(payload)
            if canonical(value) != payload or not isinstance(value, dict):
                raise ValueError
        except Exception as exc:
            raise ShoreProtocolError("shore_invalid_frame") from exc

        if set(value) == {"v", "ceremony_id", "direction", "nonce", "ciphertext"}:
            if value.get("direction") != "browser_to_host":
                raise ShoreProtocolError("pairing_failed")
            response = self.pairing.accept_packet(value, now=None if now_ms is None else now_ms / 1000)
            return None if response is None else canonical(response)

        return self._handle_envelope(value, now_ms=now_ms)

    def _handle_envelope(self, envelope: dict[str, Any], *, now_ms: int | None) -> bytes:
        device_id = envelope.get("device_id")
        trusted = self.trust.get(device_id) if isinstance(device_id, str) else None
        # Device approval is scoped to the host-key epoch in which pairing
        # occurred. Merely relabeling an old device's envelope with the new
        # epoch must never carry trust across a host-key rotation.
        if not trusted or trusted.key_epoch != self.key_epoch:
            raise ShoreProtocolError("shore_untrusted_device")
        frame = open_envelope(
            envelope,
            expected={"account_id": self.account_id, "host_id": self.host_id,
                      "device_id": trusted.device_id, "key_epoch": self.key_epoch,
                      "direction": "browser_to_host"},
            sender_signing=ed25519.Ed25519PublicKey.from_public_bytes(trusted.signing_key),
            receiver_agreement=self.host_agreement,
            sender_agreement=x25519.X25519PublicKey.from_public_bytes(trusted.agreement_key),
            replay=self.replay, now_ms=now_ms, validate_frame=self._validate_probe,
        )
        sequence = self._next_sequence(trusted)
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        response = seal_envelope(
            {"v": 1, "type": "shore.probe.result", "payload": frame["payload"]},
            account_id=self.account_id, host_id=self.host_id, device_id=trusted.device_id,
            key_epoch=self.key_epoch, direction="host_to_browser", seq=sequence,
            request_id=uuid7(now_ms), issued_at=self._timestamp(now_ms),
            expires_at=self._timestamp(now_ms + 30_000), sender_signing=self.host_signing,
            sender_agreement=self.host_agreement,
            receiver_agreement=x25519.X25519PublicKey.from_public_bytes(trusted.agreement_key),
        )
        return canonical(response)

    @staticmethod
    def _validate_probe(frame: dict[str, Any]) -> None:
        if set(frame) != {"v", "type", "payload"} or frame.get("v") != 1 or frame.get("type") != "shore.probe":
            raise ShoreProtocolError("shore_unsupported_frame")
        payload = frame.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"nonce"} or not isinstance(payload["nonce"], str) or not (1 <= len(payload["nonce"]) <= 128):
            raise ShoreProtocolError("shore_invalid_frame")

    def _next_sequence(self, device: TrustedDevice) -> int:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        path = self.state_dir / "outbound.sqlite3"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with sqlite3.connect(path, isolation_level=None) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sequences (scope TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            scope = f"{device.device_id}:{self.key_epoch}:host_to_browser"
            row = connection.execute("SELECT value FROM sequences WHERE scope=?", (scope,)).fetchone()
            value = (row[0] if row else 0) + 1
            connection.execute("INSERT INTO sequences(scope,value) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET value=excluded.value", (scope, value))
            connection.execute("COMMIT")
        return value

    @staticmethod
    def _timestamp(value: int) -> str:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
