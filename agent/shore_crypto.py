"""Broker-blind Shore v1 envelopes and local pairing state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

UUID7 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MAX_UINT64 = (1 << 64) - 1
OUTER_FIELDS = {"v", "account_id", "host_id", "device_id", "key_epoch", "direction", "seq", "request_id", "issued_at", "expires_at", "nonce", "ciphertext", "signature"}
DIRECTIONS = {"browser_to_host", "host_to_browser"}


class ShoreProtocolError(ValueError):
    """A stable, pre-dispatch Shore protocol failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


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
    nonce = nonce or secrets.token_bytes(12)
    aad = {"v": 1, "account_id": account_id, "host_id": host_id, "device_id": device_id,
           "key_epoch": key_epoch, "direction": direction, "seq": str(seq), "request_id": request_id,
           "issued_at": issued_at, "expires_at": expires_at, "nonce": b64url(nonce)}
    plaintext = canonical(frame)
    if len(plaintext) > 256 * 1024 or direction not in DIRECTIONS:
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

    def accept(self, scope: str, seq: int, request_id: str, now_ms: int) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
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
        if (str(seq) != envelope["seq"] or seq < 1 or seq > MAX_UINT64
                or not UUID7.fullmatch(envelope["account_id"])
                or not UUID7.fullmatch(envelope["host_id"])
                or not UUID7.fullmatch(envelope["device_id"])
                or not UUID7.fullmatch(envelope["request_id"])
                or not isinstance(envelope["key_epoch"], int) or isinstance(envelope["key_epoch"], bool)
                or envelope["key_epoch"] < 1 or envelope["direction"] not in DIRECTIONS):
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


def pairing_finished(key: bytes, role: str, binding: bytes) -> bytes:
    if role not in {"browser", "host"}: raise ValueError("invalid pairing role")
    return hmac.new(key, role.encode() + b"-finished\0" + binding, hashlib.sha256).digest()


@dataclass
class PairingCeremony:
    secret: bytearray
    nonce: bytes
    expires_at: float
    failures: int = 0
    used: bool = False

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
