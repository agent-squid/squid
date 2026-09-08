import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from agent.shore_audit import ShoreAuditLog
from agent.shore_audit_export import B2AuditConfig, B2AuditWriter, export_once
from .test_shore_audit import HOST_ID, REQUEST_ID
from .test_shore_transport import DEVICE, NOW


def _log(tmp_path):
    log = ShoreAuditLog(tmp_path / "audit.sqlite3", host_id=HOST_ID, key_epoch=1,
                        host_signing=ed25519.Ed25519PrivateKey.generate())
    log.record(request_id=REQUEST_ID, device_id=DEVICE, message_type="ping",
               frame={"v": 1, "type": "ping", "payload": {}},
               decision="granted", outcome="ok", now_ms=NOW)
    return log


def _config():
    return B2AuditConfig("https://s3.us-west-004.backblazeb2.com", "shore-audit-dev",
                         "write-only-key", "secret", "us-west-004")


def test_writer_uses_signed_encrypted_put(tmp_path):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200)
    log = _log(tmp_path)
    writer = B2AuditWriter(_config(), transport=httpx.MockTransport(handler))
    assert export_once(log, writer)
    request = seen[0]
    assert request.method == "PUT" and request.headers["x-amz-server-side-encryption"] == "AES256"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=write-only-key/")
    assert request.url.path.startswith("/shore-audit-dev/host/events/")
    assert log.pending_export() is None


def test_failed_upload_does_not_advance_cursor(tmp_path):
    log = _log(tmp_path)
    writer = B2AuditWriter(_config(), transport=httpx.MockTransport(lambda _request: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        export_once(log, writer)
    assert log.pending_export() is not None


def test_config_requires_complete_https_environment(monkeypatch):
    monkeypatch.setenv("SQUID_SHORE_AUDIT_B2_ENDPOINT", "http://example.test")
    with pytest.raises(ValueError, match="incomplete"):
        B2AuditConfig.from_env()
