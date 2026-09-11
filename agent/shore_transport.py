"""Runtime boundary for Shore pairing and encrypted transport.

Milestone 3 proved the authenticated encrypted channel with a single
`shore.probe`/`shore.probe.result` echo, which the real browser client still
uses as a connectivity check and which stays supported unchanged. Milestone 4
adds real ADR-0040 dispatch for whatever message types a device's granted
capabilities allow (`agent/shore_capabilities.py`), including a push-capable
per-device subscription reusing the shared realtime core in `agent/server.py`
(`_realtime_catchup`/`_realtime_snapshot`) -- see
docs/plans/adr-0039-shore-remote-access.md Milestone 4.3 and the per-device
push liveness/backpressure section of docs/shore-protocol-v1.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .shore import _load_or_new_identity, _load_runtime_config
from .shore_audit import AuditExportBatch, ShoreAuditLog
from .shore_capabilities import authorize_capability_frame
from .shore_crypto import (
    MAX_KEY_INVOCATIONS, DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    TrustedDevice, b64url, canonical, open_envelope, seal_envelope, unb64url, uuid7,
    valid_relay_url,
)
from .shore_receipt import (
    PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN, ReceiptVerificationError, VerifiedRelayReceipt,
    verify_relay_receipt,
)

log = logging.getLogger(__name__)

# Per-device push sweep granularity: how often ShoreHostConnection._serve
# checks subscribed devices for new events to push, pings due, and
# ping-timeout eviction. Independent of the 30s relay transport lease
# heartbeat and of ADR-0040's own 20s/40s device ping/timeout constants
# (imported lazily from agent.server so there is one source of truth) --
# this is only how often the sweep itself runs, not a protocol value.
_PUSH_SWEEP_SECONDS = 5.0
_MAX_PENDING_RECEIPTS = 256


@dataclass
class _DeviceSession:
    """Live per-device subscription state, held only in memory.

    Cleared on unsubscribe, revocation, key-epoch mismatch, overflow
    (`slow_consumer`), or ping-timeout -- never touches paired trust,
    granted capabilities, or the key epoch, so the device can always recover
    with a fresh `subscribe` (docs/shore-protocol-v1.md, "Per-device push
    liveness and backpressure").
    """

    scopes: list[dict] = field(default_factory=list)
    cursor: int = 0
    last_acked_cursor: int = -1
    last_ping_at: float = 0.0
    last_inbound_at: float = 0.0


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
        channel, relay=config.relay, username=config.username,
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
        self.audit = ShoreAuditLog(state_dir / "audit.sqlite3", host_id=host_id, key_epoch=key_epoch, host_signing=host_signing)
        self.pairing = PairingCoordinator(
            self.trust, account_id=account_id, host_id=host_id,
            host_signing_key=host_signing.public_key(),
            host_agreement_key=host_agreement.public_key(), key_epoch=key_epoch,
        )
        self._outbound_provisioned = False
        # Live per-device push subscriptions; never persisted (see
        # _DeviceSession). One host socket multiplexes every paired device,
        # so this dict -- not a per-connection local -- is the per-device
        # state the 4.1 shared realtime core needs. Guarded by a real
        # threading.Lock, not left to the event loop's single-thread
        # cooperative scheduling to protect it, because revoke_device runs
        # this dict's own add/remove operations from a *different OS thread*:
        # agent/server.py's revoke endpoint calls it via asyncio.to_thread,
        # racing the event-loop thread's _dispatch_adr0040/_push_sweep. All
        # dict-level (not per-session-field) access goes through the
        # _session_*  helpers below so this can't be bypassed by accident.
        self._sessions_lock = threading.Lock()
        self.sessions: dict[str, _DeviceSession] = {}

    def _session_snapshot(self) -> list[tuple[str, "_DeviceSession"]]:
        with self._sessions_lock:
            return list(self.sessions.items())

    def _get_session(self, device_id: str) -> "_DeviceSession | None":
        with self._sessions_lock:
            return self.sessions.get(device_id)

    def _get_or_create_session(self, device_id: str) -> "_DeviceSession":
        with self._sessions_lock:
            return self.sessions.setdefault(device_id, _DeviceSession())

    def _drop_session(self, device_id: str) -> None:
        with self._sessions_lock:
            self.sessions.pop(device_id, None)

    def begin_pairing(self, ceremony_id: str) -> dict[str, Any]:
        return self.pairing.begin(ceremony_id=ceremony_id)

    def pairing_status(self, ceremony_id: str) -> dict[str, Any]:
        return self.pairing.status(ceremony_id)

    def list_devices(self) -> list[TrustedDevice]:
        return self.trust.list_paired()

    def revoke_device(self, device_id: str) -> bool:
        # Revocation must drop any live push subscription immediately, not
        # just future trust lookups -- otherwise a revoked device would keep
        # receiving pushes from an already-authorized in-memory session.
        # This runs on a worker thread (server.py's /shore/devices/revoke
        # calls it via asyncio.to_thread) concurrently with the event loop
        # thread's own session dict access -- see the lock note above.
        self._drop_session(device_id)
        return self.trust.revoke(device_id)

    async def handle(self, payload: bytes, *, now_ms: int | None = None,
                     relay_receipt: VerifiedRelayReceipt | None = None) -> list[bytes]:
        """Handle one binary relay frame; malformed input always fails closed.

        Returns zero or more sealed `host_to_browser` envelopes to send, in
        order (e.g. a `subscribe` can produce `subscribed` plus a snapshot or
        several replayed events).
        """
        try:
            value = json.loads(payload)
            if canonical(value) != payload or not isinstance(value, dict):
                raise ValueError
        except Exception as exc:
            raise ShoreProtocolError("shore_invalid_frame") from exc

        if relay_receipt is not None and value.get("request_id") != relay_receipt.receipt["request_id"]:
            raise ReceiptVerificationError("shore_receipt_conflict")

        if set(value) == {"v", "ceremony_id", "direction", "nonce", "ciphertext"}:
            if value.get("direction") != "browser_to_host":
                raise ShoreProtocolError("pairing_failed")
            # Pairing crypto (HKDF/AESGCM/HMAC) is CPU-bound; offloaded like
            # the rest of this method's crypto (see _handle_envelope) so it
            # can't block the event loop other Shore devices and the direct
            # /ws/v1 path share.
            response = await asyncio.to_thread(
                self.pairing.accept_packet, value, now=None if now_ms is None else now_ms / 1000,
            )
            return [] if response is None else [canonical(response)]

        return await self._handle_envelope(value, now_ms=now_ms, relay_receipt=relay_receipt)

    async def _record_audit_best_effort(
        self, *, request_id: str, device_id: str, message_type: str, frame: dict[str, Any],
        decision: str, outcome: str, now_ms: int, context: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.audit.record, request_id=request_id, device_id=device_id,
                message_type=message_type, frame=frame, decision=decision,
                outcome=outcome, now_ms=now_ms,
            )
        except Exception:
            log.warning(
                "shore: best-effort audit record failed (%s) request_id=%s device_id=%s",
                context, request_id, device_id, exc_info=True,
            )

    async def _handle_envelope(self, envelope: dict[str, Any], *, now_ms: int | None,
                               relay_receipt: VerifiedRelayReceipt | None = None) -> list[bytes]:
        device_id = envelope.get("device_id")
        trusted = self.trust.get(device_id) if isinstance(device_id, str) else None
        # Device approval is scoped to the host-key epoch in which pairing
        # occurred. Merely relabeling an old device's envelope with the new
        # epoch must never carry trust across a host-key rotation.
        if not trusted or trusted.key_epoch != self.key_epoch:
            raise ShoreProtocolError("shore_untrusted_device")
        # Signature verify + AEAD decrypt are CPU-bound; offloaded (as the
        # whole of this method used to be, pre-4.3) so they can't block the
        # event loop every other Shore device and the direct /ws/v1 path
        # share -- 4.3 open question 4, kept conservative pending an actual
        # benchmark rather than assumed safe to inline.
        frame = await asyncio.to_thread(
            open_envelope, envelope,
            expected={"account_id": self.account_id, "host_id": self.host_id,
                      "device_id": trusted.device_id, "key_epoch": self.key_epoch,
                      "direction": "browser_to_host"},
            sender_signing=ed25519.Ed25519PublicKey.from_public_bytes(trusted.signing_key),
            receiver_agreement=self.host_agreement,
            sender_agreement=x25519.X25519PublicKey.from_public_bytes(trusted.agreement_key),
            replay=self.replay, now_ms=now_ms, validate_frame=None,
        )
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms

        # `shore.probe`/`shore.probe.result` is the Milestone-3 connectivity
        # echo the real browser client still sends before/independent of any
        # capability-gated ADR-0040 traffic (shore/browser/src/client.ts) --
        # it stays supported unchanged, checked before capability dispatch
        # since it isn't an ADR-0040 type at all.
        if frame.get("type") == "shore.probe":
            self._validate_probe(frame)
            try:
                recorded = await asyncio.to_thread(
                    self.audit.record, request_id=envelope["request_id"], device_id=trusted.device_id,
                    message_type="shore.probe", frame=frame, decision="protocol", outcome="ok", now_ms=now_ms,
                    relay_receipt=relay_receipt,
                )
                if recorded is None:
                    return []
            except ReceiptVerificationError:
                raise
            except Exception as exc:
                raise ShoreProtocolError("shore_audit_unavailable") from exc
            response = {"v": 1, "type": "shore.probe.result", "payload": frame["payload"]}
            return [await asyncio.to_thread(self._seal, trusted, response, now_ms)]

        request_id = envelope["request_id"]
        message_type = frame.get("type") if isinstance(frame.get("type"), str) else "unknown"
        try:
            authorized = authorize_capability_frame(trusted.capabilities, frame)
        except ShoreProtocolError as exc:
            if relay_receipt is not None:
                # The receipt chain covers denied ordinary envelopes too. Its
                # tip and the denial must advance atomically or the next valid
                # receipt would appear to be a gap.
                try:
                    await asyncio.to_thread(
                        self.audit.record, request_id=request_id, device_id=trusted.device_id,
                        message_type=message_type, frame=frame, decision="denied",
                        outcome=f"denied:{exc.code}", now_ms=now_ms,
                        relay_receipt=relay_receipt,
                    )
                except ReceiptVerificationError:
                    raise
                except Exception as audit_exc:
                    raise ShoreProtocolError("shore_audit_unavailable") from audit_exc
            else:
                # Legacy/observe-only transport keeps the established
                # best-effort behavior until receipt enforcement is enabled.
                await self._record_audit_best_effort(
                    request_id=request_id, device_id=trusted.device_id,
                    message_type=message_type, frame=frame, decision="denied",
                    outcome=f"denied:{exc.code}", now_ms=now_ms, context="capability denial",
                )
            raise

        # Durably record the authorization *before* dispatch runs any side
        # effect (e.g. creating/mutating a subscription session), and fail
        # closed -- never dispatch -- if that can't be recorded. Recording
        # only ever appends a new chained event, so the follow-up outcome
        # record below is a second, separately verifiable event correlated
        # by the same request_id, not an in-place rewrite of this one.
        try:
            recorded = await asyncio.to_thread(
                self.audit.record, request_id=request_id, device_id=trusted.device_id,
                message_type=message_type, frame=frame, decision="granted", outcome="pending", now_ms=now_ms,
                relay_receipt=relay_receipt,
            )
            if recorded is None:
                return []
        except ReceiptVerificationError:
            raise
        except Exception as exc:
            # shore-security-operations.md: "security actions fail closed if
            # their audit record cannot be durably queued" -- a command that
            # can't be attributed is never dispatched.
            raise ShoreProtocolError("shore_audit_unavailable") from exc

        try:
            responses = await self._dispatch_adr0040(trusted, authorized, now_ms=now_ms)
        except Exception:
            # Dispatch already failed; best-effort so this can't mask the
            # original error, but the chain shouldn't be left silent
            # about what happened to an authorization it already recorded.
            await self._record_audit_best_effort(
                request_id=request_id, device_id=trusted.device_id,
                message_type=message_type, frame=frame, decision="granted",
                outcome="error:dispatch_failed", now_ms=now_ms, context="dispatch failure",
            )
            raise

        outcome = "ok"
        for response in responses:
            if response.get("type") == "error":
                code = response.get("payload", {}).get("code") if isinstance(response.get("payload"), dict) else None
                outcome = f"error:{code}" if isinstance(code, str) else "error"
                break
        # The authorization is already durably recorded above; a failure
        # to also record the completion outcome doesn't need to fail
        # closed the same way, since the command already ran and
        # verify_chain's tip check still proves the "pending" record
        # wasn't silently the last word.
        await self._record_audit_best_effort(
            request_id=request_id, device_id=trusted.device_id,
            message_type=message_type, frame=frame, decision="granted",
            outcome=outcome, now_ms=now_ms, context="outcome",
        )
        sealed = []
        for response in responses:
            sealed.append(await asyncio.to_thread(self._seal, trusted, response, now_ms))
        return sealed

    async def _dispatch_adr0040(
        self, trusted: TrustedDevice, frame: dict[str, Any], *, now_ms: int,
    ) -> list[dict[str, Any]]:
        """Dispatch one authorized ADR-0040 frame into the shared realtime core.

        `frame` has already passed `authorize_capability_frame`: its type is
        granted by the device's capabilities and its payload (including any
        `scopes`) is validated and authorized. Only produces reply frames;
        proactive pushes for an already-subscribed device are the push sweep's
        job (`ShoreHostConnection._push_sweep`), not this method's.
        """
        from .server import _RealtimeSlowConsumer, _realtime_catchup, _realtime_snapshot

        device_id = trusted.device_id
        principal = f"shore:{device_id}"
        message_type = frame["type"]
        payload = frame["payload"]
        session = self._get_session(device_id)
        now = time.monotonic()
        responses: list[dict[str, Any]] = []

        if message_type == "subscribe":
            session = self._get_or_create_session(device_id)
            session.scopes = payload["scopes"]
            session.last_inbound_at = now
            session.last_ping_at = now
            responses.append({"v": 1, "type": "subscribed", "payload": {"scopes": session.scopes}})
            requested_cursor = payload.get("cursor")
            if not isinstance(requested_cursor, int) or requested_cursor < 0:
                # Mirrors the fresh-subscribe snapshot framing in both
                # realtime_v1's own no-cursor branch and _realtime_catchup's
                # rollover branch (agent/server.py) -- there is no shared
                # helper for "build a {type: snapshot, event_id, payload}
                # frame with cursor_reset set" across all three call sites,
                # so a future shape change to one must be applied to all.
                snapshot = await _realtime_snapshot(session.scopes)
                snapshot["cursor_reset"] = True
                session.cursor = snapshot["cursor"]
                responses.append({"v": 1, "type": "snapshot", "event_id": session.cursor, "payload": snapshot})
            else:
                outbound = self._new_outbound()
                try:
                    session.cursor = await _realtime_catchup(
                        outbound, requested_cursor, session.scopes, principal, session.last_acked_cursor,
                    )
                except _RealtimeSlowConsumer:
                    # A backlog too large to catch up from on first subscribe
                    # is the same overflow condition as a live one -- fail
                    # the fresh session the same way, rather than leaving a
                    # subscribed-but-never-caught-up session behind.
                    self._drop_session(device_id)
                    return [{"v": 1, "type": "subscribed", "payload": {"scopes": session.scopes}},
                            {"v": 1, "type": "error", "payload": {"code": "slow_consumer", "resumable": True}}]
                while len(outbound):
                    responses.append(await outbound.get())
        elif message_type == "unsubscribe":
            self._drop_session(device_id)
            responses.append({"v": 1, "type": "unsubscribed", "payload": {}})
        elif message_type == "ack":
            if session is not None:
                acked = payload["event_id"]
                session.last_acked_cursor = max(session.last_acked_cursor, min(acked, session.cursor))
                session.last_inbound_at = now
        elif message_type == "ping":
            responses.append({"v": 1, "type": "pong", "payload": {}})
            if session is not None:
                session.last_inbound_at = now
        elif message_type == "pong":
            if session is not None:
                session.last_inbound_at = now
        return responses

    @staticmethod
    def _new_outbound():
        from .server import _REALTIME_OUTBOUND_QUEUE_LIMIT, _RealtimeOutbound
        return _RealtimeOutbound(_REALTIME_OUTBOUND_QUEUE_LIMIT)

    def _seal(self, trusted: TrustedDevice, frame: dict[str, Any], now_ms: int) -> bytes:
        sequence = self._next_sequence(trusted)
        response = seal_envelope(
            frame, account_id=self.account_id, host_id=self.host_id, device_id=trusted.device_id,
            key_epoch=self.key_epoch, direction="host_to_browser", seq=sequence,
            request_id=uuid7(now_ms), issued_at=self._timestamp(now_ms),
            expires_at=self._timestamp(now_ms + 30_000), sender_signing=self.host_signing,
            sender_agreement=self.host_agreement,
            receiver_agreement=x25519.X25519PublicKey.from_public_bytes(trusted.agreement_key),
        )
        return canonical(response)

    @staticmethod
    def _validate_probe(frame: dict[str, Any]) -> None:
        if set(frame) != {"v", "type", "payload"} or frame.get("v") != 1:
            raise ShoreProtocolError("shore_invalid_frame")
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

    def __init__(self, channel: ShoreChannel, *, relay: str, username: str,
                 host_id: str, signing_key: ed25519.Ed25519PrivateKey,
                 heartbeat_seconds: float = 30.0, base_backoff: float = 1.0,
                 max_backoff: float = 30.0, stable_seconds: float = 60.0):
        if not valid_relay_url(relay):
            raise ValueError("relay must use HTTPS, or HTTP on an explicit loopback host")
        parsed = urlsplit(relay)
        self.channel = channel
        self.host_id = host_id
        self.signing_key = signing_key
        self.heartbeat_seconds = heartbeat_seconds
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.stable_seconds = stable_seconds
        self.receipt_keys = self._load_receipt_keys(
            self._relay_origin(parsed), parsed.hostname,
        )
        self._pending_receipt_envelopes: dict[str, bytes] = {}
        self._pending_audit_batch: AuditExportBatch | None = None
        base_path = parsed.path.rstrip("/")
        account_path = f"{base_path}/@{quote(username, safe='')}"
        self.challenge_url = urlunsplit((parsed.scheme, parsed.netloc, account_path + "/host/connect-challenge", "", ""))
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self.relay_url = urlunsplit((ws_scheme, parsed.netloc, account_path + "/relay", f"account_id={channel.account_id}", ""))

    @staticmethod
    def _relay_origin(parsed: Any) -> str:
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        authority = host if parsed.port in {None, default_port} else f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{authority}"

    @staticmethod
    def _load_receipt_keys(
        origin: str, hostname: str | None,
    ) -> dict[int, ed25519.Ed25519PublicKey]:
        loopback = hostname in {"127.0.0.1", "::1", "localhost"}
        encoded = os.environ.get("SHORE_RECEIPT_PUBLIC_KEYS") if loopback else None
        try:
            if encoded is not None:
                values = json.loads(encoded)
            elif loopback or not PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN:
                values = {}
            else:
                pinned = PINNED_SHORE_RECEIPT_PUBLIC_KEYS_BY_ORIGIN.get(origin)
                if pinned is None:
                    raise ValueError
                values = {str(epoch): value for epoch, value in pinned.items()}
            if not isinstance(values, dict):
                raise ValueError
            if not values:
                return {}
            keys = {}
            for epoch, value in values.items():
                if not isinstance(epoch, str) or not epoch.isascii() or not epoch.isdigit() or epoch.startswith("0"):
                    raise ValueError
                if not isinstance(value, str) or b64url(unb64url(value)) != value:
                    raise ValueError
                keys[int(epoch)] = ed25519.Ed25519PublicKey.from_public_bytes(unb64url(value))
            return keys
        except Exception as exc:
            source = "SHORE_RECEIPT_PUBLIC_KEYS" if loopback else "release-pinned Shore receipt keys"
            raise ValueError(f"invalid {source}") from exc

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
        from .server import _realtime_notifier

        # Acknowledgements belong to one authenticated socket. A reconnect
        # cannot legitimately acknowledge bytes sent on its predecessor.
        self._pending_receipt_envelopes.clear()
        last_sent = time.monotonic()
        receive = asyncio.create_task(socket.recv())
        generation = _realtime_notifier.generation
        notify_task = asyncio.create_task(_realtime_notifier.wait(generation))
        next_sweep_at = time.monotonic() + _PUSH_SWEEP_SECONDS
        try:
            if self.receipt_keys and await self._send_audit_batch(socket):
                last_sent = time.monotonic()
            while not stop.is_set():
                now = time.monotonic()
                remaining = max(0.0, self.heartbeat_seconds - (now - last_sent))
                sweep_remaining = max(0.0, next_sweep_at - now)
                done, _pending = await asyncio.wait(
                    {receive, notify_task}, timeout=min(remaining, sweep_remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )

                notified = notify_task in done
                if notified:
                    generation = notify_task.result()
                    notify_task = asyncio.create_task(_realtime_notifier.wait(generation))

                if notified or time.monotonic() >= next_sweep_at:
                    # last_sent must only advance on an actual send -- an
                    # empty sweep (no due pushes/pings) must not suppress the
                    # transport lease heartbeat below.
                    if await self._push_sweep(socket):
                        last_sent = time.monotonic()
                    next_sweep_at = time.monotonic() + _PUSH_SWEEP_SECONDS

                if time.monotonic() - last_sent >= self.heartbeat_seconds:
                    # This deadline tracks host sends, independently of inbound
                    # traffic, so malformed peer frames cannot suppress leases.
                    await socket.send(b"")
                    last_sent = time.monotonic()

                if receive not in done:
                    continue
                message = receive.result()
                receive = asyncio.create_task(socket.recv())
                if not isinstance(message, bytes):
                    await socket.close(code=1003, reason="binary_frames_only")
                    return
                if message == b"":
                    # Relay lease heartbeat; receipts cover ordinary
                    # encrypted envelopes only.
                    continue
                try:
                    relay_receipt = None
                    envelope = message
                    if self.receipt_keys:
                        try:
                            wrapper = json.loads(message)
                            if canonical(wrapper) != message or wrapper.get("v") != 1:
                                raise ValueError
                            if wrapper.get("type") == "host_audit_batch_ack":
                                await self._accept_audit_batch_ack(wrapper)
                                if await self._send_audit_batch(socket):
                                    last_sent = time.monotonic()
                                continue
                            if set(wrapper) == {"v", "ceremony_id", "direction", "nonce", "ciphertext"}:
                                # Pairing packets retain their raw wire format
                                # and are validated by ShoreChannel.handle.
                                try:
                                    responses = await self.channel.handle(message)
                                except ShoreProtocolError:
                                    continue
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    log.exception("shore: unexpected error handling pairing frame")
                                    continue
                                for response in responses:
                                    await socket.send(response)
                                    last_sent = time.monotonic()
                                continue
                            if wrapper.get("type") == "relay_receipt_ack" and set(wrapper) == {"v", "type", "receipt"}:
                                receipt_value = wrapper["receipt"]
                                request_id = receipt_value.get("request_id") if isinstance(receipt_value, dict) else None
                                pending = self._pending_receipt_envelopes.get(request_id) if isinstance(request_id, str) else None
                                if pending is None:
                                    try:
                                        if await asyncio.to_thread(self.channel.audit.has_receipt, receipt_value):
                                            # Stable acknowledgement retry: it
                                            # was verified before the durable
                                            # copy was accepted, so it is a
                                            # no-op and must not regress tip.
                                            continue
                                    except Exception as exc:
                                        raise ReceiptVerificationError("shore_audit_continuity_unavailable") from exc
                                    raise ReceiptVerificationError("shore_audit_continuity_unavailable")
                                acknowledgement = verify_relay_receipt(
                                    receipt_value, pending, host_id=self.host_id,
                                    direction="host_to_browser", keys=self.receipt_keys,
                                )
                                try:
                                    await asyncio.to_thread(self.channel.audit.accept_receipt, acknowledgement)
                                except ReceiptVerificationError:
                                    raise
                                except Exception as exc:
                                    raise ReceiptVerificationError("shore_audit_continuity_unavailable") from exc
                                self._pending_receipt_envelopes.pop(request_id, None)
                                continue
                            if set(wrapper) != {"v", "type", "envelope", "receipt"} or wrapper.get("type") != "relay_delivery":
                                raise ValueError
                            envelope = unb64url(wrapper["envelope"])
                        except Exception as exc:
                            if isinstance(exc, ReceiptVerificationError):
                                raise
                            raise ReceiptVerificationError("shore_audit_continuity_unavailable") from exc
                        relay_receipt = verify_relay_receipt(
                            wrapper["receipt"], envelope, host_id=self.host_id,
                            direction="browser_to_host", keys=self.receipt_keys,
                        )
                    responses = await self.channel.handle(envelope, relay_receipt=relay_receipt)
                except ReceiptVerificationError as exc:
                    await socket.close(code=1008, reason=exc.code)
                    return
                except ShoreProtocolError:
                    # A malformed or injected peer frame must not tear down the
                    # authenticated host transport or produce an oracle response.
                    # Once a relay receipt has been authenticated, however,
                    # failing before its atomic audit transaction would strand
                    # the local tip behind Shore's next sequence forever.
                    if relay_receipt is not None:
                        try:
                            persisted = await asyncio.to_thread(
                                self.channel.audit.has_receipt, relay_receipt.receipt,
                            )
                        except Exception:
                            persisted = False
                        if not persisted:
                            await socket.close(code=1008, reason="shore_audit_continuity_unavailable")
                            return
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # An authorized frame can still reach into the reused local
                    # realtime core (agent/server.py's _realtime_snapshot/
                    # _realtime_catchup via _dispatch_adr0040), which was
                    # written for the fully-trusted local session and isn't
                    # guaranteed to fail closed with ShoreProtocolError for
                    # every internal-state edge. One frame from one device
                    # must not tear down the multiplexed relay for every other
                    # paired device -- drop it and keep serving, the same as a
                    # malformed peer frame.
                    log.exception("shore: unexpected error handling relay frame")
                    continue
                for response in responses:
                    await self._send_application(socket, response)
                    last_sent = time.monotonic()
        finally:
            receive.cancel()
            notify_task.cancel()
            # Either task may have already finished with its own exception
            # (e.g. ConnectionClosed) right as this scope was cancelled from
            # outside; suppress broadly so that unrelated exception doesn't
            # shadow the CancelledError already propagating.
            with suppress(Exception, asyncio.CancelledError):
                await receive
            with suppress(Exception, asyncio.CancelledError):
                await notify_task

    async def _send_audit_batch(self, socket: Any) -> bool:
        batch = await asyncio.to_thread(self.channel.audit.pending_export, limit=25)
        self._pending_audit_batch = batch
        if batch is None:
            return False
        await socket.send(canonical({"v": 1, "type": "host_audit_batch", "body": b64url(batch.body)}))
        return True

    async def _accept_audit_batch_ack(self, acknowledgement: dict[str, Any]) -> None:
        batch = self._pending_audit_batch
        keys = {"v", "type", "hostId", "throughSeq", "headHash", "payloadHash", "receiptEpoch", "signature"}
        if batch is None or set(acknowledgement) != keys:
            raise ReceiptVerificationError("shore_audit_continuity_unavailable")
        signature = acknowledgement.get("signature")
        epoch = acknowledgement.get("receiptEpoch")
        try:
            document = json.loads(batch.body)
            manifest = document["manifest"]
            if (acknowledgement.get("v") != 1
                    or acknowledgement.get("type") != "host_audit_batch_ack"
                    or acknowledgement.get("hostId") != self.host_id
                    or acknowledgement.get("throughSeq") != batch.through_seq
                    or acknowledgement.get("headHash") != batch.through_hash
                    or acknowledgement.get("payloadHash") != manifest["payloadHash"]
                    or isinstance(epoch, bool) or not isinstance(epoch, int)
                    or not isinstance(signature, str)):
                raise ValueError
            key = self.receipt_keys.get(epoch)
            if key is None:
                raise ValueError
            unsigned = {name: acknowledgement[name] for name in acknowledgement if name != "signature"}
            encoded_signature = unb64url(signature)
            if len(encoded_signature) != 64 or b64url(encoded_signature) != signature:
                raise ValueError
            key.verify(encoded_signature, canonical(unsigned))
            await asyncio.to_thread(self.channel.audit.mark_exported, batch)
            self._pending_audit_batch = None
        except ReceiptVerificationError:
            raise
        except Exception as exc:
            raise ReceiptVerificationError("shore_audit_continuity_unavailable") from exc

    async def _push_sweep(self, socket: Any) -> int:
        """Push new events, due pings, and timeout evictions to every subscribed device.

        One host socket multiplexes every paired device, so a slow or
        unresponsive device is handled entirely in memory here -- an
        application-level `slow_consumer`/ping-timeout equivalent, never a
        WebSocket close, per docs/shore-protocol-v1.md's "Per-device push
        liveness and backpressure". Overflow or timeout on one device never
        touches another device's session. Returns how many frames were
        actually sent, so the caller can tell a real send from a no-op sweep.
        """
        from .server import (
            _REALTIME_HEARTBEAT_MISS_LIMIT, _REALTIME_HEARTBEAT_SECONDS,
            _RealtimeSlowConsumer, _realtime_catchup,
        )

        now = time.monotonic()
        now_ms = int(time.time() * 1000)
        sent = 0
        for device_id, session in self.channel._session_snapshot():
            if not session.scopes:
                continue
            trusted = self.channel.trust.get(device_id)
            if not trusted or trusted.key_epoch != self.channel.key_epoch:
                self.channel._drop_session(device_id)
                continue
            if now - session.last_inbound_at > _REALTIME_HEARTBEAT_SECONDS * _REALTIME_HEARTBEAT_MISS_LIMIT:
                self.channel._drop_session(device_id)
                continue

            outbound = self.channel._new_outbound()
            try:
                session.cursor = await _realtime_catchup(
                    outbound, session.cursor, session.scopes,
                    f"shore:{device_id}", session.last_acked_cursor,
                )
            except _RealtimeSlowConsumer:
                sealed = await asyncio.to_thread(
                    self.channel._seal, trusted,
                    {"v": 1, "type": "error", "payload": {"code": "slow_consumer", "resumable": True}}, now_ms,
                )
                await self._send_application(socket, sealed)
                sent += 1
                self.channel._drop_session(device_id)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same reused-realtime-core residual as the inbound path in
                # _serve above: one device's catch-up must not abort the
                # sweep for every other subscribed device.
                log.exception("shore: unexpected error in push sweep for device %s", device_id)
                self.channel._drop_session(device_id)
                continue
            while len(outbound):
                frame = await outbound.get()
                sealed = await asyncio.to_thread(self.channel._seal, trusted, frame, now_ms)
                await self._send_application(socket, sealed)
                sent += 1

            if now - session.last_ping_at >= _REALTIME_HEARTBEAT_SECONDS:
                sealed = await asyncio.to_thread(
                    self.channel._seal, trusted, {"v": 1, "type": "ping", "payload": {}}, now_ms,
                )
                await self._send_application(socket, sealed)
                sent += 1
                session.last_ping_at = now
        return sent

    async def _send_application(self, socket: Any, envelope: bytes) -> None:
        if self.receipt_keys:
            try:
                request_id = json.loads(envelope)["request_id"]
                if not isinstance(request_id, str):
                    raise ValueError
            except Exception as exc:
                raise ShoreProtocolError("shore_invalid_outbound_envelope") from exc
            existing = self._pending_receipt_envelopes.get(request_id)
            if existing is not None and existing != envelope:
                raise ShoreProtocolError("shore_receipt_conflict")
            if existing is None and len(self._pending_receipt_envelopes) >= _MAX_PENDING_RECEIPTS:
                raise ShoreProtocolError("shore_receipt_backpressure")
            self._pending_receipt_envelopes[request_id] = envelope
        await socket.send(envelope)
