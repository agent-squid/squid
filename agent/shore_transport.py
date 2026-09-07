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
from .shore_capabilities import authorize_capability_frame
from .shore_crypto import (
    MAX_KEY_INVOCATIONS, DeviceTrustStore, PairingCoordinator, ReplayStore, ShoreProtocolError,
    TrustedDevice, b64url, canonical, open_envelope, seal_envelope, uuid7,
    valid_broker_url,
)

# Per-device push sweep granularity: how often ShoreHostConnection._serve
# checks subscribed devices for new events to push, pings due, and
# ping-timeout eviction. Independent of the 30s broker transport lease
# heartbeat and of ADR-0040's own 20s/40s device ping/timeout constants
# (imported lazily from agent.server so there is one source of truth) --
# this is only how often the sweep itself runs, not a protocol value.
_PUSH_SWEEP_SECONDS = 5.0


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

    async def handle(self, payload: bytes, *, now_ms: int | None = None) -> list[bytes]:
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

        return await self._handle_envelope(value, now_ms=now_ms)

    async def _handle_envelope(self, envelope: dict[str, Any], *, now_ms: int | None) -> list[bytes]:
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
            response = {"v": 1, "type": "shore.probe.result", "payload": frame["payload"]}
            return [await asyncio.to_thread(self._seal, trusted, response, now_ms)]

        frame = authorize_capability_frame(trusted.capabilities, frame)
        responses = await self._dispatch_adr0040(trusted, frame, now_ms=now_ms)
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
        from .server import _realtime_notifier

        last_sent = time.monotonic()
        receive = asyncio.create_task(socket.recv())
        generation = _realtime_notifier.generation
        notify_task = asyncio.create_task(_realtime_notifier.wait(generation))
        next_sweep_at = time.monotonic() + _PUSH_SWEEP_SECONDS
        try:
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
                try:
                    responses = await self.channel.handle(message)
                except ShoreProtocolError:
                    # A malformed or injected peer frame must not tear down the
                    # authenticated host transport or produce an oracle response.
                    continue
                for response in responses:
                    await socket.send(response)
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
                await socket.send(sealed)
                sent += 1
                self.channel._drop_session(device_id)
                continue
            while len(outbound):
                frame = await outbound.get()
                sealed = await asyncio.to_thread(self.channel._seal, trusted, frame, now_ms)
                await socket.send(sealed)
                sent += 1

            if now - session.last_ping_at >= _REALTIME_HEARTBEAT_SECONDS:
                sealed = await asyncio.to_thread(
                    self.channel._seal, trusted, {"v": 1, "type": "ping", "payload": {}}, now_ms,
                )
                await socket.send(sealed)
                sent += 1
                session.last_ping_at = now
        return sent
