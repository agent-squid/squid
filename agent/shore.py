"""Local Shore identity material and signed host registration client."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets
import stat
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _uuid7() -> str:
    value = (int(time.time() * 1000) << 80) | (0x7 << 76) | (secrets.randbits(12) << 64) | (0b10 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


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
    host_id = _uuid7()
    _write_private(directory / "host-id", (host_id + "\n").encode())
    return host_id, signing, agreement


def _load_or_new_identity(directory: Path) -> tuple[str, ed25519.Ed25519PrivateKey, x25519.X25519PrivateKey]:
    if not directory.exists() or not any(directory.iterdir()):
        return _new_identity(directory)
    required = [directory / "host-id", directory / "signing.pem", directory / "agreement.pem"]
    if any(not path.is_file() for path in required):
        raise RuntimeError(f"incomplete Shore host identity at {directory}; refusing to replace it")
    for path in required:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"unsafe permissions on Shore identity file {path}")
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
    broker = urlsplit(args.broker)
    if broker.scheme not in {"http", "https"} or not broker.netloc or broker.username or broker.password or broker.query or broker.fragment:
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
    except (OSError, ValueError, RuntimeError, KeyError, httpx.HTTPError) as exc:
        # Keep any generated identity: silently generating another key on retry
        # would turn a transient failure into replacement.
        print(f"ERROR: Shore login failed: {exc}", file=sys.stderr)
        return 1
    print(f"registered Shore host {host_id}; private keys remain in {args.identity_dir}")
    return 0
