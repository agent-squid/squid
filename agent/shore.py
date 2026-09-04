"""Local Shore identity material and signed host registration client."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from .shore_crypto import UUID7, uuid7, valid_broker_url, valid_key_epoch

_valid_broker_url = valid_broker_url


@dataclass(frozen=True)
class ShoreRuntimeConfig:
    broker: str
    username: str
    account_id: str
    key_epoch: int


def _valid_uuid7(value: object) -> bool:
    return isinstance(value, str) and bool(UUID7.fullmatch(value))


def _valid_username(value: object) -> bool:
    return bool(
        isinstance(value, str) and re.fullmatch(r"[a-z0-9]{3,32}", value)
        and value not in {"admin", "api", "internal", "www"}
    )


def _runtime_config_path(directory: Path) -> Path:
    return directory / "connection.json"


def _validate_runtime_config(config: ShoreRuntimeConfig, message: str) -> None:
    if (not _valid_broker_url(config.broker)
            or not _valid_username(config.username)
            or not _valid_uuid7(config.account_id)
            or not valid_key_epoch(config.key_epoch)):
        raise RuntimeError(message)


def _check_private_file_permissions(path: Path, message: str) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"{message} {path}")


def _write_runtime_config(directory: Path, config: ShoreRuntimeConfig) -> None:
    """Atomically persist only public routing metadata beside the host keys."""
    _validate_runtime_config(config, "refusing to persist invalid Shore connection configuration")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = json.dumps({
        "account_id": config.account_id, "broker": config.broker,
        "key_epoch": config.key_epoch, "username": config.username,
    }, separators=(",", ":"), sort_keys=True) + "\n"
    fd, name = tempfile.mkstemp(prefix=".connection.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, _runtime_config_path(directory))
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _load_runtime_config(directory: Path) -> ShoreRuntimeConfig | None:
    path = _runtime_config_path(directory)
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError(f"unsafe permissions on Shore connection configuration {path}")
    _check_private_file_permissions(path, "unsafe permissions on Shore connection configuration")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        config = ShoreRuntimeConfig(
            broker=value["broker"], username=value["username"],
            account_id=value["account_id"], key_epoch=value["key_epoch"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Shore connection configuration at {path}") from exc
    _validate_runtime_config(config, f"invalid Shore connection configuration at {path}")
    return config


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_identity(directory: Path) -> tuple[str, ed25519.Ed25519PrivateKey, x25519.X25519PrivateKey]:
    if directory.exists() and any(directory.iterdir()):
        raise RuntimeError(f"Shore host identity already exists at {directory}; refusing to replace it")
    signing = ed25519.Ed25519PrivateKey.generate()
    agreement = x25519.X25519PrivateKey.generate()
    _write_private(directory / "signing.pem", signing.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write_private(directory / "agreement.pem", agreement.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    host_id = uuid7()
    _write_private(directory / "host-id", (host_id + "\n").encode())
    return host_id, signing, agreement


def _load_or_new_identity(directory: Path) -> tuple[str, ed25519.Ed25519PrivateKey, x25519.X25519PrivateKey]:
    if not directory.exists() or not any(directory.iterdir()):
        return _new_identity(directory)
    required = [directory / "host-id", directory / "signing.pem", directory / "agreement.pem"]
    if any(not path.is_file() for path in required):
        raise RuntimeError(f"incomplete Shore host identity at {directory}; refusing to replace it")
    for path in required:
        _check_private_file_permissions(path, "unsafe permissions on Shore identity file")
    signing = serialization.load_pem_private_key(required[1].read_bytes(), password=None)
    agreement = serialization.load_pem_private_key(required[2].read_bytes(), password=None)
    if not isinstance(signing, ed25519.Ed25519PrivateKey) or not isinstance(agreement, x25519.X25519PrivateKey):
        raise RuntimeError(f"invalid Shore host identity at {directory}")
    host_id = required[0].read_text().strip()
    try:
        parsed = uuid.UUID(host_id)
    except ValueError as exc:
        raise RuntimeError(f"invalid Shore host identity at {directory}") from exc
    if parsed.version != 7 or str(parsed) != host_id:
        raise RuntimeError(f"invalid Shore host identity at {directory}")
    return host_id, signing, agreement


def _registration_proof(host_id: str, challenge: dict, signing_x: str, agreement_x: str) -> bytes:
    required = {"id", "nonce"}
    if not required.issubset(challenge) or not all(isinstance(challenge[key], str) for key in required):
        raise RuntimeError("broker returned an invalid host challenge")
    # All keys and values in this closed object are restricted ASCII. This is
    # therefore byte-for-byte RFC 8785 JCS while avoiding a second JSON model.
    return json.dumps(
        {"agreement_key": agreement_x, "challenge_id": challenge["id"], "host_id": host_id,
         "nonce": challenge["nonce"], "signing_key": signing_x, "v": 1},
        ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def login(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentsquid login", description="Register this machine as the account's Shore host")
    parser.add_argument("--broker", default="https://agentsquid.ai")
    account = parser.add_mutually_exclusive_group(required=True)
    account.add_argument("--username", help="AgentSquid username (recommended)")
    account.add_argument("--account-id", help="immutable account ID for administrative use")
    parser.add_argument("--session-token", default=os.environ.get("AGENTSQUID_SESSION_TOKEN"))
    parser.add_argument("--email", help="account email (prompted when omitted)")
    parser.add_argument("--magic-code", help=argparse.SUPPRESS)
    parser.add_argument("--totp-code", help=argparse.SUPPRESS)
    parser.add_argument("--identity-dir", type=Path, default=Path.home() / ".squid" / "shore")
    args = parser.parse_args(argv)
    if args.account_id and not args.session_token:
        parser.error("--account-id requires --session-token or AGENTSQUID_SESSION_TOKEN")
    if args.username:
        args.username = args.username.lower()
        if not _valid_username(args.username):
            parser.error("--username must be a non-reserved 3-32 character lowercase name")
    if args.account_id and not _valid_uuid7(args.account_id):
        parser.error("--account-id must be a canonical UUIDv7")
    if not _valid_broker_url(args.broker):
        parser.error("--broker must be an absolute HTTP(S) URL without embedded credentials")

    try:
        host_id, signing, agreement = _load_or_new_identity(args.identity_dir)
        endpoint = (
            f"{args.broker.rstrip('/')}/@{args.username}"
            if args.username
            else f"{args.broker.rstrip('/')}/internal/accounts/{args.account_id}"
        )
        with httpx.Client(timeout=15.0) as client:
            if args.session_token:
                headers = {"authorization": f"Bearer {args.session_token}", "content-type": "application/json"}
            else:
                email = args.email or input("Account email: ").strip()
                magic_response = client.post(endpoint + "/auth/magic-link", json={"email": email})
                magic_response.raise_for_status()
                magic_code = args.magic_code or getpass.getpass("Sign-in code from email: ")
                consume_response = client.post(endpoint + "/auth/consume", json={"token": magic_code})
                consume_response.raise_for_status()
                consume = consume_response.json()
                csrf = consume.get("csrfToken")
                if not isinstance(csrf, str):
                    raise RuntimeError("broker returned an invalid login response")
                totp_code = args.totp_code or getpass.getpass("Authenticator code: ")
                step_response = client.post(endpoint + "/auth/step-up", headers={"x-shore-csrf": csrf}, json={"code": totp_code})
                step_response.raise_for_status()
                stepped = step_response.json()
                csrf = stepped.get("csrfToken")
                if not isinstance(csrf, str):
                    raise RuntimeError("broker returned an invalid second-factor response")
                headers = {"x-shore-csrf": csrf, "content-type": "application/json"}
            challenge_response = client.post(endpoint + "/host/challenge", headers=headers, json={"hostId": host_id})
            challenge_response.raise_for_status()
            challenge = challenge_response.json()
            signing_x = _b64url(signing.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
            agreement_x = _b64url(agreement.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
            proof = _registration_proof(host_id, challenge, signing_x, agreement_x)
            response = client.post(endpoint + "/host/register", headers=headers, json={
                "challengeId": challenge["id"], "hostId": host_id,
                "signingKey": {"kty": "OKP", "crv": "Ed25519", "x": signing_x},
                "agreementKey": {"kty": "OKP", "crv": "X25519", "x": agreement_x},
                "signature": _b64url(signing.sign(proof)),
            })
            response.raise_for_status()
            registered = response.json()
            runtime = ShoreRuntimeConfig(
                broker=args.broker.rstrip("/"), username=registered["username"],
                account_id=registered["accountId"], key_epoch=registered["keyEpoch"],
            )
            _validate_runtime_config(runtime, "broker returned inconsistent host registration metadata")
            expected_signing = {"kty": "OKP", "crv": "Ed25519", "x": signing_x}
            expected_agreement = {"kty": "OKP", "crv": "X25519", "x": agreement_x}
            if (registered.get("id") != host_id
                    or registered.get("signingKey") != expected_signing
                    or registered.get("agreementKey") != expected_agreement
                    or (args.username and runtime.username != args.username)
                    or (args.account_id and runtime.account_id != args.account_id)):
                raise RuntimeError("broker returned inconsistent host registration metadata")
            _write_runtime_config(args.identity_dir, runtime)
    except (OSError, TypeError, ValueError, RuntimeError, KeyError, httpx.HTTPError) as exc:
        # Keep any generated identity: silently generating another key on retry
        # would turn a transient failure into replacement.
        print(f"ERROR: Shore login failed: {exc}", file=sys.stderr)
        return 1
    print(f"registered Shore host {host_id}; private keys remain in {args.identity_dir}")
    return 0
