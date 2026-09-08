"""Append-only S3-compatible export for the local Shore audit queue."""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from .shore_audit import ShoreAuditLog

log = logging.getLogger(__name__)
MAX_EXPORT_LAG_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class B2AuditConfig:
    endpoint: str
    bucket: str
    key_id: str
    application_key: str
    region: str

    @classmethod
    def from_env(cls) -> "B2AuditConfig | None":
        names = ("ENDPOINT", "BUCKET", "KEY_ID", "APPLICATION_KEY")
        values = {name: os.environ.get(f"SQUID_SHORE_AUDIT_B2_{name}") for name in names}
        if not any(values.values()):
            return None
        if not all(values.values()):
            raise ValueError("incomplete Shore audit B2 configuration")
        endpoint = values["ENDPOINT"].rstrip("/")
        if urlsplit(endpoint).scheme != "https" or not urlsplit(endpoint).netloc:
            raise ValueError("Shore audit B2 endpoint must use HTTPS")
        return cls(endpoint, values["BUCKET"], values["KEY_ID"], values["APPLICATION_KEY"],
                   os.environ.get("SQUID_SHORE_AUDIT_B2_REGION", "us-west-004"))


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


class B2AuditWriter:
    def __init__(self, config: B2AuditConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.transport = transport

    def put_append_only(self, object_name: str, body: bytes, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        parsed = urlsplit(self.config.endpoint)
        path = "/" + quote(self.config.bucket, safe="") + "/" + quote(object_name, safe="/")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "content-type": "application/json",
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": stamp,
            "x-amz-server-side-encryption": "AES256",
        }
        names = sorted(headers)
        canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in names)
        signed_headers = ";".join(names)
        canonical_request = f"PUT\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{day}/{self.config.region}/s3/aws4_request"
        string_to_sign = "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
        date_key = _sign(("AWS4" + self.config.application_key).encode(), day)
        region_key = _sign(date_key, self.config.region)
        service_key = _sign(region_key, "s3")
        signature = hmac.new(_sign(service_key, "aws4_request"), string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.config.key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        with httpx.Client(timeout=15.0, transport=self.transport) as client:
            response = client.put(self.config.endpoint + path, content=body, headers=headers)
        response.raise_for_status()


def export_once(log: ShoreAuditLog, writer: B2AuditWriter, *, limit: int = 500) -> bool:
    batch = log.pending_export(limit=limit)
    if batch is None:
        return False
    writer.put_append_only(batch.object_name, batch.body)
    log.mark_exported(batch)
    return True


async def run_export_loop(audit: ShoreAuditLog, writer: B2AuditWriter, stop: asyncio.Event,
                          *, interval: float = 5.0) -> None:
    """Drain batches until stopped; transient failures retain the local cursor."""
    while not stop.is_set():
        try:
            while await asyncio.to_thread(export_once, audit, writer):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            lag = await asyncio.to_thread(audit.export_lag_ms)
            if lag > MAX_EXPORT_LAG_MS:
                log.error("Shore audit export lag exceeds five minutes: %dms", lag, exc_info=True)
            else:
                log.warning("Shore audit export failed; retrying", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
