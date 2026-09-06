"""Runtime boundary for Shore pairing and encrypted transport probes.

This deliberately does not dispatch ADR-0040 commands.  It is the narrow
Milestone-3 transport slice used to prove the authenticated encrypted channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .shore import _load_or_new_identity, _load_runtime_config
from .shore_crypto import (
    MAX_KEY_INVOCATIONS, DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    TrustedDevice, b64url, canonical, open_envelope, seal_envelope, uuid7,
    valid_broker_url,
)


def configured_host_connection(identity_dir: Path) -> "ShoreHostConnection | None":
    """Build the daemon relay connection from a completed Shore login."""
    config = _load_runtime_config(identity_dir)
    if config is None:
        return None
    host_id, signing, agreement = _load_or_new_identity(identity_dir)
    channel = ShoreChannel(
        identity_dir, account_id=config.account_id, host_id=host_id,
        host_signing=signing, host_agreement=agreement, key_epoch=config.key_epoch,
    )
    return ShoreHostConnection(
        channel, broker=config.broker, username=config.username,
        host_id=host_id, signing_key=signing,
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
        self._outbound_provisioned = False

    def begin_pairing(self, ceremony_id: str) -> dict[str, Any]:
        return self.pairing.begin(ceremony_id=ceremony_id)

    def pairing_status(self, ceremony_id: str) -> dict[str, Any]:
        return self.pairing.status(ceremony_id)

    def list_devices(self) -> list[TrustedDevice]:
        return self.trust.list_paired()

    def revoke_device(self, device_id: str) -> bool:
        return self.trust.revoke(device_id)

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
        path = self.state_dir / "outbound.sqlite3"
        try:
            if not self._outbound_provisioned:
                self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.state_dir, 0o700)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(fd)
                os.chmod(path, 0o600)
                self._outbound_provisioned = True
            with sqlite3.connect(path, isolation_level=None) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS sequences (scope TEXT PRIMARY KEY, value INTEGER NOT NULL)")
                connection.execute("BEGIN IMMEDIATE")
                scope = f"{device.device_id}:{self.key_epoch}:host_to_browser"
                row = connection.execute("SELECT value FROM sequences WHERE scope=?", (scope,)).fetchone()
                value = (row[0] if row else 0) + 1
                if value > MAX_KEY_INVOCATIONS:
                    connection.execute("ROLLBACK")
                    raise ShoreProtocolError("shore_sequence_exhausted")
                connection.execute("INSERT INTO sequences(scope,value) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET value=excluded.value", (scope, value))
                connection.execute("COMMIT")
        except ShoreProtocolError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ShoreProtocolError("shore_sequence_store_failed") from exc
        return value

    @staticmethod
    def _timestamp(value: int) -> str:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ShoreHostConnection:
    """Maintains the authenticated host relay socket until explicitly stopped."""

    def __init__(self, channel: ShoreChannel, *, broker: str, username: str,
                 host_id: str, signing_key: ed25519.Ed25519PrivateKey,
                 heartbeat_seconds: float = 30.0, base_backoff: float = 1.0,
                 max_backoff: float = 30.0, stable_seconds: float = 60.0):
        if not valid_broker_url(broker):
            raise ValueError("broker must use HTTPS, or HTTP on an explicit loopback host")
        parsed = urlsplit(broker)
        self.channel = channel
        self.host_id = host_id
        self.signing_key = signing_key
        self.heartbeat_seconds = heartbeat_seconds
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.stable_seconds = stable_seconds
        base_path = parsed.path.rstrip("/")
        account_path = f"{base_path}/@{quote(username, safe='')}"
        self.challenge_url = urlunsplit((parsed.scheme, parsed.netloc, account_path + "/host/connect-challenge", "", ""))
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self.relay_url = urlunsplit((ws_scheme, parsed.netloc, account_path + "/relay", f"account_id={channel.account_id}", ""))

    async def run(self, stop: asyncio.Event) -> None:
        """Reconnect with bounded exponential backoff; return only when stopped."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
        delay = self.base_backoff
        while not stop.is_set():
            connected_at: float | None = None
            try:
                headers = await self._connection_headers()
                async with connect(
                    self.relay_url, additional_headers=headers,
                    open_timeout=15, close_timeout=5, ping_interval=20,
                ) as socket:
                    connected_at = time.monotonic()
                    await self._serve(socket, stop)
                if stop.is_set():
                    break
                raise ShoreProtocolError("shore_connection_closed")
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if code in {1008, 1009}:
                    return
                delay = await self._backoff(stop, self._reset_if_stable(connected_at, delay))
            except InvalidStatus as exc:
                status = exc.response.status_code
                if self._terminal_http_status(status):
                    return
                delay = await self._backoff(stop, delay)
            except httpx.HTTPStatusError as exc:
                if self._terminal_http_status(exc.response.status_code):
                    return
                delay = await self._backoff(stop, delay)
            except (OSError, httpx.HTTPError, ShoreProtocolError, WebSocketException):
                delay = await self._backoff(stop, self._reset_if_stable(connected_at, delay))

    @staticmethod
    def _terminal_http_status(status: int) -> bool:
        return 400 <= status < 500 and status not in {408, 425, 429}

    def _reset_if_stable(self, connected_at: float | None, delay: float) -> float:
        """A connection that stayed up past stable_seconds earns a fresh backoff budget."""
        if connected_at is not None and time.monotonic() - connected_at >= self.stable_seconds:
            return self.base_backoff
        return delay

    async def _backoff(self, stop: asyncio.Event, delay: float) -> float:
        if stop.is_set():
            return delay
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay + random.uniform(0, delay * 0.2))
        except asyncio.TimeoutError:
            pass
        # `delay or 0.001` keeps growth working even when base_backoff is 0
        # (only used by tests, for a fast first retry); production always
        # starts from the 1.0s default, where this is a no-op.
        return min(self.max_backoff, max(self.base_backoff, (delay or 0.001) * 2))

    async def _connection_headers(self) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(self.challenge_url, json={"hostId": self.host_id})
            response.raise_for_status()
            try:
                challenge = response.json()
            except ValueError as exc:
                raise ShoreProtocolError("shore_invalid_host_challenge") from exc
        if not isinstance(challenge, dict) or not all(isinstance(challenge.get(key), str) for key in ("id", "nonce")):
            raise ShoreProtocolError("shore_invalid_host_challenge")
        proof = canonical({"challenge_id": challenge["id"], "host_id": self.host_id,
                           "nonce": challenge["nonce"], "purpose": "websocket", "v": 1})
        return {"x-shore-role": "host", "x-shore-host-id": self.host_id,
                "x-shore-challenge-id": challenge["id"],
                "x-shore-signature": b64url(self.signing_key.sign(proof))}

    async def _serve(self, socket: Any, stop: asyncio.Event) -> None:
        last_sent = time.monotonic()
        receive = asyncio.create_task(socket.recv())
        try:
            while not stop.is_set():
                remaining = max(0.0, self.heartbeat_seconds - (time.monotonic() - last_sent))
                try:
                    message = await asyncio.wait_for(asyncio.shield(receive), timeout=remaining)
                except asyncio.TimeoutError:
                    # This deadline tracks host sends, independently of inbound
                    # traffic, so malformed peer frames cannot suppress leases.
                    await socket.send(b"")
                    last_sent = time.monotonic()
                    continue
                receive = asyncio.create_task(socket.recv())
                if not isinstance(message, bytes):
                    await socket.close(code=1003, reason="binary_frames_only")
                    return
                try:
                    response = await asyncio.to_thread(self.channel.handle, message)
                except ShoreProtocolError:
                    # A malformed or injected peer frame must not tear down the
                    # authenticated host transport or produce an oracle response.
                    continue
                if response is not None:
                    await socket.send(response)
                    last_sent = time.monotonic()
        finally:
            receive.cancel()
            # The shielded receive task may have already finished with its own
            # exception (e.g. ConnectionClosed) right as this scope was
            # cancelled from outside; suppress it broadly so that unrelated
            # exception doesn't shadow the CancelledError already propagating.
            with suppress(Exception, asyncio.CancelledError):
                await receive
