#!/usr/bin/env python3
"""Explicit live Milestone-5 verifier (not pytest/CI).

Runs the real magic-code/TOTP login and host registration before exercising
the deployed relay. Running it creates audit objects with live bucket retention::

  uv run python tests/manual/verify_shore_live_e2e.py --username alice \
      --email alice@example.com --identity-dir /path/to/shore
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent.shore_crypto import (ReplayStore, b64url, canonical, crockford32_decode,  # noqa: E402
    derive_pair_bootstrap_key, derive_pair_key, fingerprint, open_envelope,
    pairing_finished, seal_envelope, unb64url, uuid7)
from agent.shore_transport import configured_host_connection  # noqa: E402
from agent.shore import _response_error, login  # noqa: E402


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def authenticate(relay: str, username: str, email: str,
                 magic_code: str | None, totp_code: str | None) -> str:
    """Complete the public login flow and return the in-memory session cookie."""
    endpoint = f"{relay.rstrip('/')}/@{username}"
    with httpx.Client(timeout=20) as client:
        response = client.post(endpoint + "/auth/magic-link", json={"email": email})
        if response.status_code == 404 and _response_error(response) == "unknown_username":
            print(f"@{username} does not exist; requesting signup", file=sys.stderr)
            response = client.post(endpoint + "/auth/signup", json={"email": email})
        response.raise_for_status()
        code = magic_code or getpass.getpass("Sign-in code from email: ")
        consumed = client.post(endpoint + "/auth/consume", json={"token": code})
        consumed.raise_for_status()
        csrf = consumed.json().get("csrfToken")
        if not isinstance(csrf, str): raise RuntimeError("invalid login response")
        enrolled = client.post(endpoint + "/auth/totp/enroll", headers={"x-shore-csrf": csrf})
        if enrolled.status_code == 201:
            secret = enrolled.json().get("secret")
            if not isinstance(secret, str): raise RuntimeError("invalid TOTP enrollment response")
            print(f"Add this key to your authenticator before continuing: {secret}", file=sys.stderr)
        elif enrolled.status_code != 409:
            enrolled.raise_for_status()
        code = totp_code or getpass.getpass("Authenticator code: ")
        stepped = client.post(endpoint + "/auth/step-up", headers={"x-shore-csrf": csrf}, json={"code": code})
        stepped.raise_for_status()
        token = client.cookies.get("__Host-shore_session")
        if not token: raise RuntimeError("login did not issue a Shore session cookie")
        return token


def begin_pair(started: dict, device_id: str, signing, agreement):
    offer = started["offer"]
    binding = {"v": 1, "account_id": offer["account_id"], "host_id": offer["host_id"],
        "device_id": device_id, "ceremony_nonce": offer["ceremony_nonce"],
        "host_sign_fingerprint": offer["host_sign_fingerprint"], "host_enc_fingerprint": offer["host_enc_fingerprint"],
        "browser_sign_fingerprint": fingerprint(signing.public_key()), "browser_enc_fingerprint": fingerprint(agreement.public_key())}
    binding_bytes = canonical(binding)
    secret, ceremony_nonce = crockford32_decode(started["code"]), unb64url(offer["ceremony_nonce"])
    key = derive_pair_key(secret, ceremony_nonce, binding_bytes)
    plaintext = {"v": 1, "binding": binding, "browser_keys": {
        "signing": b64url(signing.public_key().public_bytes_raw()), "agreement": b64url(agreement.public_key().public_bytes_raw())},
        "finished": b64url(pairing_finished(key, "browser", binding_bytes))}
    nonce = secrets.token_bytes(12)
    packet = {"v": 1, "ceremony_id": offer["ceremony_id"], "direction": "browser_to_host", "nonce": b64url(nonce)}
    packet["ciphertext"] = b64url(AESGCM(derive_pair_bootstrap_key(secret, ceremony_nonce)).encrypt(nonce, canonical(plaintext), canonical(packet)))
    return canonical(packet), key, binding_bytes


def finish_pair(raw: bytes, key: bytes, binding: bytes):
    response = json.loads(raw)
    nonce = unb64url(response["nonce"])
    plaintext = json.loads(AESGCM(key).decrypt(nonce, unb64url(response["ciphertext"]),
        canonical({k: v for k, v in response.items() if k != "ciphertext"})))
    if canonical(plaintext["binding"]) != binding or unb64url(plaintext["finished"]) != pairing_finished(key, "host", binding):
        raise RuntimeError("host pairing proof failed")
    confirm_plaintext = {"v": 1, "binding": json.loads(binding),
        "finished": b64url(pairing_finished(key, "browser-confirmed", binding))}
    confirm_nonce = secrets.token_bytes(12)
    confirmation = {"v": 1, "ceremony_id": response["ceremony_id"], "direction": "browser_to_host", "nonce": b64url(confirm_nonce)}
    confirmation["ciphertext"] = b64url(AESGCM(key).encrypt(confirm_nonce, canonical(confirm_plaintext), canonical(confirmation)))
    return canonical(confirmation), unb64url(plaintext["host_keys"]["signing"]), unb64url(plaintext["host_keys"]["agreement"])


async def verify(identity_dir: Path, token: str, timeout: float) -> None:
    from websockets.asyncio.client import connect
    host = configured_host_connection(identity_dir)
    if host is None: raise RuntimeError(f"no registered Shore host at {identity_dir}")
    if not host.receipt_keys: raise RuntimeError("relay origin has no release-pinned receipt key")
    stop = asyncio.Event()
    host_task = asyncio.create_task(host.run(stop))
    try:
        await asyncio.sleep(2)
        started = host.channel.begin_pairing(uuid7())
        signing, agreement, device_id = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate(), uuid7()
        packet, key, binding = begin_pair(started, device_id, signing, agreement)
        url = f"{host.relay_url}&device_id={quote(device_id, safe='')}"
        origin = host.relay_url.replace("wss://", "https://", 1).split("/@", 1)[0]
        async with connect(url, origin=origin, additional_headers={"Cookie": f"__Host-shore_session={token}"}, open_timeout=timeout) as browser:
            await browser.send(packet)
            confirmation, host_signing, host_agreement = finish_pair(await asyncio.wait_for(browser.recv(), timeout), key, binding)
            if (fingerprint(ed25519.Ed25519PublicKey.from_public_bytes(host_signing)) != started["offer"]["host_sign_fingerprint"]
                    or fingerprint(x25519.X25519PublicKey.from_public_bytes(host_agreement)) != started["offer"]["host_enc_fingerprint"]):
                raise RuntimeError("host keys do not match pairing offer")
            await browser.send(confirmation)
            request_id, now = uuid7(), datetime.now(timezone.utc)
            probe = canonical(seal_envelope({"v": 1, "type": "shore.probe", "payload": {"nonce": "live-ms5"}},
                account_id=host.channel.account_id, host_id=host.host_id, device_id=device_id, key_epoch=host.channel.key_epoch,
                direction="browser_to_host", seq=1, request_id=request_id, issued_at=timestamp(now), expires_at=timestamp(now + timedelta(seconds=30)),
                sender_signing=signing, sender_agreement=agreement, receiver_agreement=x25519.X25519PublicKey.from_public_bytes(host_agreement)))
            await browser.send(probe)
            response = await asyncio.wait_for(browser.recv(), timeout)
            opened = open_envelope(json.loads(response), expected={"account_id": host.channel.account_id, "host_id": host.host_id,
                "device_id": device_id, "key_epoch": host.channel.key_epoch, "direction": "host_to_browser"},
                sender_signing=ed25519.Ed25519PublicKey.from_public_bytes(host_signing), receiver_agreement=agreement,
                sender_agreement=x25519.X25519PublicKey.from_public_bytes(host_agreement), replay=ReplayStore(identity_dir / "live-verifier-replay.sqlite3"))
            if opened != {"v": 1, "type": "shore.probe.result", "payload": {"nonce": "live-ms5"}}: raise RuntimeError("unexpected probe response")
            await browser.send(probe)
            try: duplicate = await asyncio.wait_for(browser.recv(), 2)
            except asyncio.TimeoutError: duplicate = None
            if duplicate is not None: raise RuntimeError("byte-identical retry dispatched twice")
        deadline = time.monotonic() + timeout
        while host.channel.audit.pending_export(limit=25) is not None:
            if time.monotonic() >= deadline: raise RuntimeError("live archive acknowledgement timed out")
            await asyncio.sleep(.25)
        matching = [event for event in host.channel.audit.events() if event.get("requestId") == request_id]
        if len(matching) != 1: raise RuntimeError(f"expected one dispatch audit event, found {len(matching)}")
        print(f"PASS: paired {device_id}; receipt retry {request_id} was idempotent; archive acknowledged")
    finally:
        stop.set()
        try: await asyncio.wait_for(host_task, 10)
        except asyncio.TimeoutError:
            host_task.cancel(); await asyncio.gather(host_task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", default="https://dev.agentsquid.ai")
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--magic-code", help=argparse.SUPPRESS)
    parser.add_argument("--totp-code", help=argparse.SUPPRESS)
    parser.add_argument("--identity-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        token = authenticate(args.relay, args.username, args.email, args.magic_code, args.totp_code)
        registered = login(["--relay", args.relay, "--username", args.username,
            "--session-token", token, "--identity-dir", str(args.identity_dir)])
        if registered != 0: raise RuntimeError("host registration failed")
        asyncio.run(verify(args.identity_dir, token, args.timeout))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
