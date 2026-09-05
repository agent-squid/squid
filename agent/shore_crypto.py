"""Broker-blind Shore v1 envelopes and local pairing state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MAX_KEY_INVOCATIONS = 1 << 32
MAX_SAFE_INTEGER = (1 << 53) - 1
OUTER_FIELDS = {"v", "account_id", "host_id", "device_id", "key_epoch", "direction", "seq", "request_id", "issued_at", "expires_at", "nonce", "ciphertext", "signature"}
DIRECTIONS = {"browser_to_host", "host_to_browser"}
CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def valid_key_epoch(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SAFE_INTEGER


def valid_broker_url(value: object) -> bool:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # accessing port performs validation that urlsplit itself defers
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "http":
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return False
    return True


class ShoreProtocolError(ValueError):
    """A stable, pre-dispatch Shore protocol failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def uuid7(now_ms: int | None = None) -> str:
    """Generate the canonical UUIDv7 form used by all Shore host code."""
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    value = (timestamp << 80) | (0x7 << 76) | (secrets.randbits(12) << 64) | (0b10 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise ValueError("invalid base64url")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if b64url(decoded) != value: raise ValueError("non-canonical base64url")
    return decoded


def crockford32_encode(value: bytes) -> str:
    if len(value) != 16: raise ValueError("pairing secret must be 128 bits")
    number = int.from_bytes(value, "big")
    return "".join(CROCKFORD32[(number >> shift) & 31] for shift in range(125, -1, -5))


def crockford32_decode(value: str) -> bytes:
    if not isinstance(value, str): raise ValueError("invalid pairing code")
    value = value.upper()
    if len(value) != 26 or value[0] not in CROCKFORD32[:8]:
        raise ValueError("invalid pairing code")
    try:
        number = 0
        for character in value.upper(): number = number * 32 + CROCKFORD32.index(character)
        return number.to_bytes(16, "big")
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid pairing code") from exc


def canonical(value: Any) -> bytes:
    # Shore objects contain only integers, strings, arrays, booleans and null;
    # for that I-JSON subset this is byte-for-byte RFC 8785 JCS.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()


def fingerprint(key: ed25519.Ed25519PublicKey | x25519.X25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "sha256:" + b64url(hashlib.sha256(raw).digest())


def derive_envelope_key(private: x25519.X25519PrivateKey, peer: x25519.X25519PublicKey,
                        account_id: str, host_id: str, key_epoch: int, direction: str) -> bytes:
    prefix = b"shore-envelope-v1\0"
    salt = hashlib.sha256(prefix + account_id.encode() + host_id.encode() + str(key_epoch).encode()).digest()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=prefix + direction.encode()).derive(private.exchange(peer))


def seal_envelope(frame: dict[str, Any], *, account_id: str, host_id: str, device_id: str,
                  key_epoch: int, direction: str, seq: int, request_id: str, issued_at: str,
                  expires_at: str, sender_signing: ed25519.Ed25519PrivateKey,
                  sender_agreement: x25519.X25519PrivateKey,
                  receiver_agreement: x25519.X25519PublicKey, nonce: bytes | None = None) -> dict[str, Any]:
    if (not UUID7.fullmatch(account_id) or not UUID7.fullmatch(host_id)
            or not UUID7.fullmatch(device_id) or not UUID7.fullmatch(request_id)
            or not valid_key_epoch(key_epoch)
            or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 or seq > MAX_KEY_INVOCATIONS
            or direction not in DIRECTIONS):
        raise ShoreProtocolError("shore_invalid_frame")
    try:
        issued = _millis(issued_at)
        expires = _millis(expires_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShoreProtocolError("shore_invalid_frame") from exc
    if expires <= issued or expires - issued > 60_000:
        raise ShoreProtocolError("shore_invalid_frame")
    nonce = secrets.token_bytes(12) if nonce is None else nonce
    if not isinstance(nonce, bytes) or len(nonce) != 12:
        raise ShoreProtocolError("shore_invalid_frame")
    aad = {"v": 1, "account_id": account_id, "host_id": host_id, "device_id": device_id,
           "key_epoch": key_epoch, "direction": direction, "seq": str(seq), "request_id": request_id,
           "issued_at": issued_at, "expires_at": expires_at, "nonce": b64url(nonce)}
    try:
        plaintext = canonical(frame)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ShoreProtocolError("shore_invalid_frame") from exc
    if len(plaintext) > 256 * 1024:
        raise ShoreProtocolError("shore_invalid_frame")
    key = derive_envelope_key(sender_agreement, receiver_agreement, account_id, host_id, key_epoch, direction)
    envelope = {**aad, "ciphertext": b64url(AESGCM(key).encrypt(nonce, plaintext, canonical(aad)))}
    envelope["signature"] = b64url(sender_signing.sign(canonical(envelope)))
    return envelope


def _millis(value: str) -> int:
    from datetime import datetime
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value):
        raise ValueError
    return int(datetime.fromisoformat(value[:-1] + "+00:00").timestamp() * 1000)


class ReplayStore:
    """Transactional replay state stored in a private SQLite database."""

    def __init__(self, path: Path):
        self.path = path
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

    def accept(self, scope: str, seq: int, request_id: str, now_ms: int) -> bool:
        try:
            self._provision()
            with sqlite3.connect(self.path, timeout=5, isolation_level=None) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("CREATE TABLE IF NOT EXISTS replay_scopes (scope TEXT PRIMARY KEY, sequence TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS replay_requests (request_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("DELETE FROM replay_requests WHERE expires_at <= ?", (now_ms,))
                    row = connection.execute("SELECT sequence FROM replay_scopes WHERE scope = ?", (scope,)).fetchone()
                    duplicate_id = connection.execute(
                        "SELECT 1 FROM replay_requests WHERE request_id = ?", (request_id,)
                    ).fetchone()
                    if (row is not None and seq <= int(row[0])) or duplicate_id is not None:
                        connection.execute("ROLLBACK")
                        return False
                    connection.execute(
                        "INSERT INTO replay_scopes(scope, sequence) VALUES (?, ?) "
                        "ON CONFLICT(scope) DO UPDATE SET sequence = excluded.sequence",
                        (scope, str(seq)),
                    )
                    connection.execute(
                        "INSERT INTO replay_requests(request_id, expires_at) VALUES (?, ?)",
                        (request_id, now_ms + 7 * 24 * 60 * 60_000),
                    )
                    connection.execute("COMMIT")
                    return True
                except Exception:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        except (OSError, sqlite3.Error) as exc:
            raise ShoreProtocolError("shore_replay") from exc


def open_envelope(envelope: dict[str, Any], *, expected: dict[str, Any],
                  sender_signing: ed25519.Ed25519PublicKey,
                  receiver_agreement: x25519.X25519PrivateKey,
                  sender_agreement: x25519.X25519PublicKey, replay: ReplayStore,
                  now_ms: int | None = None, validate_frame: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not isinstance(envelope, dict) or set(envelope) != OUTER_FIELDS:
        raise ShoreProtocolError("shore_invalid_frame")
    try:
        encoded_envelope = canonical(envelope)
    except (TypeError, ValueError, RecursionError):
        raise ShoreProtocolError("shore_invalid_frame")
    if len(encoded_envelope) > 384 * 1024:
        raise ShoreProtocolError("shore_invalid_frame")
    if envelope.get("v") != 1:
        raise ShoreProtocolError("shore_invalid_frame")
    try:
        seq = int(envelope["seq"])
        if (str(seq) != envelope["seq"] or seq < 1 or seq > MAX_KEY_INVOCATIONS
                or not UUID7.fullmatch(envelope["account_id"])
                or not UUID7.fullmatch(envelope["host_id"])
                or not UUID7.fullmatch(envelope["device_id"])
                or not UUID7.fullmatch(envelope["request_id"])
                or not valid_key_epoch(envelope["key_epoch"])
                or envelope["direction"] not in DIRECTIONS):
            raise ValueError
        nonce, signature, ciphertext = (unb64url(envelope["nonce"]), unb64url(envelope["signature"]),
                                        unb64url(envelope["ciphertext"]))
        if len(nonce) != 12 or len(signature) != 64 or len(ciphertext) < 16: raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ShoreProtocolError("shore_invalid_frame")
    for name in ("account_id", "host_id", "device_id", "key_epoch", "direction"):
        if envelope.get(name) != expected.get(name):
            raise ShoreProtocolError("shore_key_epoch_mismatch" if name == "key_epoch" else "shore_identity_mismatch")
    signed = {key: value for key, value in envelope.items() if key != "signature"}
    try: sender_signing.verify(signature, canonical(signed))
    except Exception: raise ShoreProtocolError("shore_bad_signature")
    try: issued, expires = _millis(envelope["issued_at"]), _millis(envelope["expires_at"])
    except (TypeError, ValueError): raise ShoreProtocolError("shore_invalid_frame")
    if issued > now_ms + 30_000: raise ShoreProtocolError("shore_clock_skew")
    if expires <= issued or expires - issued > 60_000 or expires <= now_ms - 30_000: raise ShoreProtocolError("shore_expired")
    aad = {key: value for key, value in envelope.items() if key not in {"ciphertext", "signature"}}
    key = derive_envelope_key(receiver_agreement, sender_agreement, envelope["account_id"], envelope["host_id"], envelope["key_epoch"], envelope["direction"])
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, canonical(aad))
        if len(plaintext) > 256 * 1024: raise ValueError
    except Exception: raise ShoreProtocolError("shore_decrypt_failed")
    # Authentication must succeed before untrusted traffic can consume durable
    # replay state; replay must succeed before ADR-0040 bytes are decoded.
    scope = ":".join(str(envelope[k]) for k in ("account_id", "host_id", "key_epoch", "device_id", "direction"))
    if not replay.accept(scope, seq, envelope["request_id"], now_ms): raise ShoreProtocolError("shore_replay")
    try:
        frame = json.loads(plaintext)
        if canonical(frame) != plaintext or not isinstance(frame, dict): raise ValueError
    except Exception: raise ShoreProtocolError("shore_decrypt_failed")
    if validate_frame: validate_frame(frame)
    return frame


def pairing_binding(**values: Any) -> bytes:
    required = {"v", "account_id", "host_id", "device_id", "ceremony_nonce", "host_sign_fingerprint", "host_enc_fingerprint", "browser_sign_fingerprint", "browser_enc_fingerprint"}
    if set(values) != required or values["v"] != 1: raise ShoreProtocolError("shore_invalid_frame")
    return canonical(values)


def derive_pair_key(secret: bytes, ceremony_nonce: bytes, binding: bytes) -> bytes:
    if len(secret) != 16 or len(ceremony_nonce) != 16: raise ValueError("pairing material must be 128 bits")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ceremony_nonce,
                info=b"shore-pair-v1\0" + binding).derive(secret)


def derive_pair_bootstrap_key(secret: bytes, ceremony_nonce: bytes) -> bytes:
    if len(secret) != 16 or len(ceremony_nonce) != 16: raise ValueError("pairing material must be 128 bits")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ceremony_nonce,
                info=b"shore-pair-bootstrap-v1\0").derive(secret)


def pairing_finished(key: bytes, role: str, binding: bytes) -> bytes:
    if role not in {"browser", "host", "browser-confirmed"}: raise ValueError("invalid pairing role")
    return hmac.new(key, role.encode() + b"-finished\0" + binding, hashlib.sha256).digest()


@dataclass
class PairingCeremony:
    secret: bytearray
    nonce: bytes
    expires_at: float
    failures: int = 0
    used: bool = False
    packet_nonces: set[bytes] = field(default_factory=set)
    pending: tuple[bytes, bytes, bytes, bytes] | None = None

    @classmethod
    def create(cls, now: float | None = None) -> "PairingCeremony":
        return cls(bytearray(secrets.token_bytes(16)), secrets.token_bytes(16), (time.time() if now is None else now) + 300)

    def finish(self, proof: bytes, expected: bytes, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.used or now >= self.expires_at or self.failures >= 5:
            self.erase(); raise ShoreProtocolError("pairing_failed")
        if not hmac.compare_digest(proof, expected):
            self.failures += 1
            if self.failures >= 5: self.erase()
            raise ShoreProtocolError("pairing_failed")
        self.used = True; self.erase()

    def erase(self) -> None:
        for index in range(len(self.secret)): self.secret[index] = 0


@dataclass(frozen=True)
class TrustedDevice:
    device_id: str
    signing_key: bytes
    agreement_key: bytes
    key_epoch: int


class DeviceTrustStore:
    """Host-owned durable browser trust. Only public key material is stored."""

    def __init__(self, path: Path):
        self.path = path
        self._provisioned = False

    def _provision(self) -> None:
        if self._provisioned:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self._provisioned = True

    def _connect(self) -> sqlite3.Connection:
        self._provision()
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS shore_devices (
            device_id TEXT PRIMARY KEY, signing_key BLOB NOT NULL,
            agreement_key BLOB NOT NULL, key_epoch INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('paired','revoked')),
            approved_at INTEGER NOT NULL, revoked_at INTEGER)""")
        return connection

    def approve(self, device_id: str, signing_key: bytes, agreement_key: bytes,
                key_epoch: int, now_ms: int | None = None) -> TrustedDevice:
        if (not UUID7.fullmatch(device_id) or len(signing_key) != 32 or len(agreement_key) != 32
                or not valid_key_epoch(key_epoch)):
            raise ShoreProtocolError("pairing_failed")
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT signing_key, agreement_key, key_epoch, status FROM shore_devices WHERE device_id=?", (device_id,)
                ).fetchone()
                if existing:
                    existing_signing, existing_agreement, existing_epoch, status = existing
                    # A completed local pairing may advance the same device
                    # keys into a newer host-key epoch. It may never revive a
                    # revoked device, move backwards, or replace device keys.
                    if (status == "revoked" or existing_signing != signing_key
                            or existing_agreement != agreement_key or existing_epoch > key_epoch):
                        connection.rollback(); raise ShoreProtocolError("pairing_failed")
                    if existing_epoch < key_epoch:
                        connection.execute("UPDATE shore_devices SET key_epoch=?, approved_at=? WHERE device_id=?",
                                           (key_epoch, now_ms, device_id))
                else:
                    connection.execute("""INSERT INTO shore_devices
                        (device_id, signing_key, agreement_key, key_epoch, status, approved_at)
                        VALUES (?, ?, ?, ?, 'paired', ?)""",
                        (device_id, signing_key, agreement_key, key_epoch, now_ms))
                connection.commit()
        except ShoreProtocolError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ShoreProtocolError("pairing_failed") from exc
        return TrustedDevice(device_id, signing_key, agreement_key, key_epoch)

    def get(self, device_id: str) -> TrustedDevice | None:
        try:
            with self._connect() as connection:
                row = connection.execute("""SELECT signing_key, agreement_key, key_epoch
                    FROM shore_devices WHERE device_id=? AND status='paired'""", (device_id,)).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise ShoreProtocolError("shore_untrusted_device") from exc
        return TrustedDevice(device_id, row[0], row[1], row[2]) if row else None

    def revoke(self, device_id: str, now_ms: int | None = None) -> bool:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            with self._connect() as connection:
                cursor = connection.execute("UPDATE shore_devices SET status='revoked', revoked_at=? WHERE device_id=? AND status='paired'", (now_ms, device_id))
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error) as exc:
            raise ShoreProtocolError("shore_untrusted_device") from exc


class PairingCoordinator:
    """Single-process local ceremony; broker input is never a trust decision."""

    # A 128-bit secret makes guessing infeasible regardless of rate; these
    # bound resource exhaustion from unbounded ceremony creation and close
    # the bypass where a per-ceremony 5-attempt cap (PairingCeremony.finish)
    # is reset for free by simply starting a new ceremony.
    _BEGIN_WINDOW_SECONDS = 300.0
    _MAX_BEGINS_PER_WINDOW = 10
    _MAX_FAILURES_PER_WINDOW = 20

    def __init__(self, trust: DeviceTrustStore, *, account_id: str, host_id: str,
                 host_signing_key: ed25519.Ed25519PublicKey,
                 host_agreement_key: x25519.X25519PublicKey, key_epoch: int):
        if (not UUID7.fullmatch(account_id) or not UUID7.fullmatch(host_id)
                or not valid_key_epoch(key_epoch)):
            raise ValueError("invalid Shore host identity")
        self.trust = trust
        self.account_id = account_id
        self.host_id = host_id
        self.host_signing_key = host_signing_key
        self.host_agreement_key = host_agreement_key
        self.key_epoch = key_epoch
        self._ceremonies: dict[str, PairingCeremony] = {}
        self._expiry_timers: dict[str, threading.Timer] = {}
        # accept_packet holds this lock while selecting and executing the
        # ceremony phase; the phase-specific methods re-enter it.
        self._lock = threading.RLock()
        self._begin_history: list[float] = []
        self._failure_history: list[float] = []

    def _prune(self, history: list[float], now: float) -> None:
        cutoff = now - self._BEGIN_WINDOW_SECONDS
        while history and history[0] <= cutoff:
            history.pop(0)

    def _record_failure(self, now: float) -> None:
        self._prune(self._failure_history, now)
        self._failure_history.append(now)

    def _begin_rate_limited(self, now: float) -> bool:
        self._prune(self._begin_history, now)
        self._prune(self._failure_history, now)
        return (len(self._begin_history) >= self._MAX_BEGINS_PER_WINDOW
                or len(self._failure_history) >= self._MAX_FAILURES_PER_WINDOW)

    def _failure_rate_limited(self, now: float) -> bool:
        self._prune(self._failure_history, now)
        return len(self._failure_history) >= self._MAX_FAILURES_PER_WINDOW

    def _expire(self, ceremony_id: str, ceremony: PairingCeremony) -> None:
        with self._lock:
            current = self._ceremonies.get(ceremony_id)
            if current is ceremony:
                ceremony.erase()
                self._ceremonies.pop(ceremony_id, None)
                self._expiry_timers.pop(ceremony_id, None)

    def _cancel_expiry(self, ceremony_id: str) -> None:
        timer = self._expiry_timers.pop(ceremony_id, None)
        if timer: timer.cancel()

    def begin(self, *, ceremony_id: str, now: float | None = None) -> dict[str, Any]:
        if not UUID7.fullmatch(ceremony_id): raise ShoreProtocolError("pairing_failed")
        checked_at = time.time() if now is None else now
        ceremony = PairingCeremony.create(checked_at)
        offer = {
            "v": 1, "ceremony_id": ceremony_id, "ceremony_nonce": b64url(ceremony.nonce),
            "account_id": self.account_id, "host_id": self.host_id,
            "host_sign_fingerprint": fingerprint(self.host_signing_key),
            "host_enc_fingerprint": fingerprint(self.host_agreement_key),
        }
        with self._lock:
            if self._begin_rate_limited(checked_at):
                ceremony.erase(); raise ShoreProtocolError("pairing_rate_limited")
            if ceremony_id in self._ceremonies:
                ceremony.erase(); raise ShoreProtocolError("pairing_failed")
            self._begin_history.append(checked_at)
            self._ceremonies[ceremony_id] = ceremony
            if now is None:
                timer = threading.Timer(max(0, ceremony.expires_at - time.time()), self._expire, (ceremony_id, ceremony))
                timer.daemon = True
                self._expiry_timers[ceremony_id] = timer
                timer.start()
        return {"code": crockford32_encode(bytes(ceremony.secret)), "offer": offer, "expires_at": ceremony.expires_at}

    def accept_packet(self, packet: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
        """Atomically select and execute the current pairing protocol phase."""
        ceremony_id = packet.get("ceremony_id") if isinstance(packet, dict) else None
        with self._lock:
            ceremony = self._ceremonies.get(ceremony_id)
            if ceremony and ceremony.pending is not None:
                self.accept_browser_confirmation(packet, now=now)
                return None
            return self.accept_browser_packet(packet, now=now)

    def accept_browser_packet(self, packet: dict[str, Any], *, now: float | None = None,
                              response_nonce: bytes | None = None) -> dict[str, Any]:
        ceremony_id = packet.get("ceremony_id") if isinstance(packet, dict) else None
        checked_at = time.time() if now is None else now
        with self._lock:
            if self._failure_rate_limited(checked_at): raise ShoreProtocolError("pairing_rate_limited")
            ceremony = self._ceremonies.get(ceremony_id)
            if not ceremony: raise ShoreProtocolError("pairing_failed")
            try:
                if set(packet) != {"v", "ceremony_id", "direction", "nonce", "ciphertext"} or packet["v"] != 1 or packet["direction"] != "browser_to_host": raise ValueError
                nonce, ciphertext = unb64url(packet["nonce"]), unb64url(packet["ciphertext"])
                if len(nonce) != 12 or len(ciphertext) < 16 or nonce in ceremony.packet_nonces or ceremony.pending is not None: raise ValueError
                ceremony.packet_nonces.add(nonce)
                aad = {key: value for key, value in packet.items() if key != "ciphertext"}
                raw = AESGCM(derive_pair_bootstrap_key(bytes(ceremony.secret), ceremony.nonce)).decrypt(nonce, ciphertext, canonical(aad))
                plaintext = json.loads(raw)
                if canonical(plaintext) != raw or set(plaintext) != {"v", "binding", "browser_keys", "finished"} or plaintext["v"] != 1: raise ValueError
                keys = plaintext["browser_keys"]
                if not isinstance(keys, dict) or set(keys) != {"signing", "agreement"}: raise ValueError
                signing_key, agreement_key = unb64url(keys["signing"]), unb64url(keys["agreement"])
                browser_signing = ed25519.Ed25519PublicKey.from_public_bytes(signing_key)
                browser_agreement = x25519.X25519PublicKey.from_public_bytes(agreement_key)
                supplied = plaintext["binding"]
                expected = {"v": 1, "account_id": self.account_id, "host_id": self.host_id,
                    "device_id": supplied.get("device_id"), "ceremony_nonce": b64url(ceremony.nonce),
                    "host_sign_fingerprint": fingerprint(self.host_signing_key),
                    "host_enc_fingerprint": fingerprint(self.host_agreement_key),
                    "browser_sign_fingerprint": fingerprint(browser_signing),
                    "browser_enc_fingerprint": fingerprint(browser_agreement)}
                if supplied != expected or not UUID7.fullmatch(expected["device_id"]): raise ValueError
                binding = pairing_binding(**expected)
                key = derive_pair_key(bytes(ceremony.secret), ceremony.nonce, binding)
                proof = unb64url(plaintext["finished"])
                if ceremony.used or checked_at >= ceremony.expires_at or ceremony.failures >= 5: raise ValueError
                if not hmac.compare_digest(proof, pairing_finished(key, "browser", binding)): raise ValueError
            except Exception:
                if not ceremony.used:
                    ceremony.failures += 1
                    self._record_failure(checked_at)
                if ceremony.failures >= 5 or checked_at >= ceremony.expires_at:
                    ceremony.erase(); self._ceremonies.pop(ceremony_id, None); self._cancel_expiry(ceremony_id)
                raise ShoreProtocolError("pairing_failed")
            response_nonce = secrets.token_bytes(12) if response_nonce is None else response_nonce
            if len(response_nonce) != 12 or response_nonce in ceremony.packet_nonces: raise ShoreProtocolError("pairing_failed")
            ceremony.packet_nonces.add(response_nonce)
            host_keys = {"signing": b64url(self.host_signing_key.public_bytes_raw()), "agreement": b64url(self.host_agreement_key.public_bytes_raw())}
            response = {"v": 1, "ceremony_id": ceremony_id, "direction": "host_to_browser", "nonce": b64url(response_nonce)}
            host_plaintext = {"v": 1, "binding": expected, "host_keys": host_keys,
                              "finished": b64url(pairing_finished(key, "host", binding))}
            response["ciphertext"] = b64url(AESGCM(key).encrypt(response_nonce, canonical(host_plaintext), canonical(response)))
            ceremony.pending = (binding, signing_key, agreement_key, key)
            ceremony.erase()
            return response

    def accept_browser_confirmation(self, packet: dict[str, Any], *, now: float | None = None) -> TrustedDevice:
        ceremony_id = packet.get("ceremony_id") if isinstance(packet, dict) else None
        checked_at = time.time() if now is None else now
        with self._lock:
            if self._failure_rate_limited(checked_at): raise ShoreProtocolError("pairing_rate_limited")
            ceremony = self._ceremonies.get(ceremony_id)
            if not ceremony or ceremony.pending is None: raise ShoreProtocolError("pairing_failed")
            binding, signing_key, agreement_key, key = ceremony.pending
            try:
                if set(packet) != {"v", "ceremony_id", "direction", "nonce", "ciphertext"} or packet["v"] != 1 or packet["direction"] != "browser_to_host": raise ValueError
                nonce, ciphertext = unb64url(packet["nonce"]), unb64url(packet["ciphertext"])
                if len(nonce) != 12 or len(ciphertext) < 16 or nonce in ceremony.packet_nonces: raise ValueError
                ceremony.packet_nonces.add(nonce)
                aad = {name: value for name, value in packet.items() if name != "ciphertext"}
                raw = AESGCM(key).decrypt(nonce, ciphertext, canonical(aad))
                plaintext = json.loads(raw)
                if canonical(plaintext) != raw or set(plaintext) != {"v", "binding", "finished"} or plaintext["v"] != 1: raise ValueError
                if canonical(plaintext["binding"]) != binding: raise ValueError
                proof = unb64url(plaintext["finished"])
                if checked_at >= ceremony.expires_at or not hmac.compare_digest(proof, pairing_finished(key, "browser-confirmed", binding)): raise ValueError
            except Exception:
                ceremony.failures += 1
                self._record_failure(checked_at)
                if ceremony.failures >= 5 or checked_at >= ceremony.expires_at:
                    ceremony.erase(); self._ceremonies.pop(ceremony_id, None); self._cancel_expiry(ceremony_id)
                raise ShoreProtocolError("pairing_failed")
            device_id = json.loads(binding)["device_id"]
            trusted = self.trust.approve(device_id, signing_key, agreement_key, self.key_epoch)
            ceremony.used = True
            self._ceremonies.pop(ceremony_id, None); self._cancel_expiry(ceremony_id)
            return trusted
