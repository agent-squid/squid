import os
import stat

import pytest
import httpx

from agent.shore import (
    ShoreRuntimeConfig, _load_or_new_identity, _load_runtime_config,
    _new_identity, _registration_proof, _write_runtime_config, login,
)


def test_host_identity_is_created_with_private_permissions_and_never_replaced(tmp_path):
    identity = tmp_path / "shore"
    host_id, _, _ = _new_identity(identity)

    assert (identity / "host-id").read_text().strip() == host_id
    assert stat.S_IMODE(identity.stat().st_mode) == 0o700
    for name in ("host-id", "signing.pem", "agreement.pem"):
        assert stat.S_IMODE((identity / name).stat().st_mode) == 0o600

    original = (identity / "signing.pem").read_bytes()
    with pytest.raises(RuntimeError, match="refusing to replace"):
        _new_identity(identity)
    assert (identity / "signing.pem").read_bytes() == original
    loaded_id, _, _ = _load_or_new_identity(identity)
    assert loaded_id == host_id


def test_load_rejects_incomplete_unsafe_and_invalid_identities(tmp_path):
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "host-id").write_text("missing-keys")
    with pytest.raises(RuntimeError, match="incomplete"):
        _load_or_new_identity(incomplete)

    unsafe = tmp_path / "unsafe"
    _new_identity(unsafe)
    os.chmod(unsafe / "signing.pem", 0o644)
    with pytest.raises(RuntimeError, match="unsafe permissions"):
        _load_or_new_identity(unsafe)

    invalid = tmp_path / "invalid"
    _new_identity(invalid)
    (invalid / "host-id").write_text("not-a-uuid\n")
    with pytest.raises(RuntimeError, match="invalid Shore host identity"):
        _load_or_new_identity(invalid)


def test_registration_proof_is_stable_and_validates_challenge():
    proof = _registration_proof("host", {"id": "challenge", "nonce": "nonce"}, "sign", "agree")
    assert proof == b'{"agreement_key":"agree","challenge_id":"challenge","host_id":"host","nonce":"nonce","signing_key":"sign","v":1}'
    with pytest.raises(RuntimeError, match="invalid host challenge"):
        _registration_proof("host", {"id": "challenge"}, "sign", "agree")


def test_runtime_config_is_private_and_round_trips(tmp_path):
    identity = tmp_path / "shore"
    expected = ShoreRuntimeConfig("https://broker.example", "alice",
        "018f1f25-3f6b-7d75-a4d1-62d771381b20", 2)
    _write_runtime_config(identity, expected)
    assert _load_runtime_config(identity) == expected
    assert stat.S_IMODE(identity.stat().st_mode) == 0o700
    assert stat.S_IMODE((identity / "connection.json").stat().st_mode) == 0o600

    replacement = ShoreRuntimeConfig("https://broker.example", "alice",
        "018f1f25-3f6b-7d75-a4d1-62d771381b20", 3)
    os.chmod(identity, 0o755)
    _write_runtime_config(identity, replacement)
    assert _load_runtime_config(identity) == replacement
    assert stat.S_IMODE(identity.stat().st_mode) == 0o700
    assert sorted(path.name for path in identity.iterdir()) == ["connection.json"]


def test_runtime_config_writer_rejects_invalid_metadata_before_creating_files(tmp_path):
    identity = tmp_path / "shore"
    with pytest.raises(RuntimeError, match="refusing to persist invalid"):
        _write_runtime_config(identity, ShoreRuntimeConfig(
            "https://broker.example", "admin",
            "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1,
        ))
    assert not identity.exists()
    with pytest.raises(RuntimeError, match="refusing to persist invalid"):
        _write_runtime_config(identity, ShoreRuntimeConfig(
            "https://broker.example", "alice",
            "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1 << 53,
        ))


def test_runtime_config_rejects_unsafe_or_invalid_metadata(tmp_path):
    identity = tmp_path / "shore"
    _write_runtime_config(identity, ShoreRuntimeConfig("https://broker.example", "alice",
        "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1))
    os.chmod(identity / "connection.json", 0o644)
    with pytest.raises(RuntimeError, match="unsafe permissions"):
        _load_runtime_config(identity)
    os.chmod(identity / "connection.json", 0o600)
    (identity / "connection.json").write_text('{"broker":"file:///tmp","username":"Alice"}')
    with pytest.raises(RuntimeError, match="invalid Shore connection"):
        _load_runtime_config(identity)
    (identity / "connection.json").write_text(
        '{"account_id":"018f1f25-3f6b-7d75-a4d1-62d771381b20",'
        '"broker":"https://example.com","key_epoch":1,"username":"admin"}'
    )
    with pytest.raises(RuntimeError, match="invalid Shore connection"):
        _load_runtime_config(identity)
    (identity / "connection.json").write_text(
        '{"account_id":"018f1f25-3f6b-7d75-a4d1-62d771381b20",'
        '"broker":42,"key_epoch":1,"username":"alice"}'
    )
    with pytest.raises(RuntimeError, match="invalid Shore connection"):
        _load_runtime_config(identity)


def test_login_rejects_mismatched_administrative_account_id(tmp_path, monkeypatch, capsys):
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, url, **_kwargs):
            if url.endswith("/host/challenge"):
                return Response({"id": "018f1f25-c930-76f0-86e7-cb06d94e6a32", "nonce": "nonce"})
            return Response({
                "accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b21",
                "username": "alice", "keyEpoch": 1, "id": "wrong-host",
                "signingKey": {}, "agreementKey": {},
            })

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    result = login([
        "--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20",
        "--session-token", "session", "--identity-dir", str(tmp_path / "shore"),
    ])
    assert result == 1
    assert "inconsistent host registration metadata" in capsys.readouterr().err
    assert not (tmp_path / "shore" / "connection.json").exists()


def test_login_rejects_registration_response_for_different_host_keys(tmp_path, monkeypatch, capsys):
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def post(self, url, **kwargs):
            if url.endswith("/host/challenge"):
                return Response({"id": "018f1f25-c930-76f0-86e7-cb06d94e6a32", "nonce": "nonce"})
            request = kwargs["json"]
            return Response({
                "accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                "username": "alice", "keyEpoch": 1, "id": request["hostId"],
                "signingKey": request["signingKey"],
                "agreementKey": {**request["agreementKey"], "crv": "Ed25519"},
            })

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    result = login([
        "--username", "alice", "--session-token", "session",
        "--identity-dir", str(tmp_path / "shore"),
    ])
    assert result == 1
    assert "inconsistent host registration metadata" in capsys.readouterr().err
    assert not (tmp_path / "shore" / "connection.json").exists()


def test_login_rejects_malformed_or_credentialed_broker_urls():
    for broker in ("not-a-url", "https://user:secret@example.com", "ftp://example.com",
                   "https://[invalid", "https://example.com:invalid", "https://exa mple.com",
                   "http://broker.example"):
        with pytest.raises(SystemExit):
            login(["--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                   "--session-token", "session", "--broker", broker])


@pytest.mark.parametrize("flag,value", [
    ("--username", "alice/../internal"), ("--username", "admin"),
    ("--account-id", "../../victim?route=host"), ("--account-id", "not-a-uuid"),
])
def test_login_rejects_route_identifiers_before_identity_or_network(tmp_path, monkeypatch, flag, value):
    class Client:
        def __init__(self, **_kwargs):
            raise AssertionError("network client must not be constructed")

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    argv = [flag, value, "--identity-dir", str(tmp_path / "shore")]
    if flag == "--account-id":
        argv += ["--session-token", "session"]
    with pytest.raises(SystemExit):
        login(argv)
    assert not (tmp_path / "shore").exists()


def test_login_reports_network_failure_without_traceback(tmp_path, monkeypatch, capsys):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("broker unavailable")

    monkeypatch.setattr("agent.shore.httpx.Client", FailingClient)
    result = login([
        "--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20", "--session-token", "session",
        "--identity-dir", str(tmp_path / "shore"),
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR: Shore login failed: broker unavailable\n"


def test_login_performs_email_and_second_factor_flow_without_session_token(tmp_path, monkeypatch, capsys):
    calls = []

    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            return None

        def json(self):
            return self.value

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/auth/magic-link"):
                return Response({"sent": True})
            if url.endswith("/auth/consume"):
                return Response({"csrfToken": "csrf-one"})
            if url.endswith("/auth/step-up"):
                return Response({"csrfToken": "csrf-two"})
            if url.endswith("/host/challenge"):
                return Response({"id": "challenge", "nonce": "nonce"})
            request = kwargs["json"]
            return Response({"registered": True, "accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                "username": "alice", "keyEpoch": 1, "id": request["hostId"],
                "signingKey": request["signingKey"], "agreementKey": request["agreementKey"]})

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    result = login([
        "--username", "alice", "--email", "alice@example.com",
        "--magic-code", "magic", "--totp-code", "123456",
        "--identity-dir", str(tmp_path / "shore"),
    ])
    assert result == 0
    assert [url.rsplit("/", 2)[-2:] for url, _ in calls] == [
        ["auth", "magic-link"], ["auth", "consume"], ["auth", "step-up"],
        ["host", "challenge"], ["host", "register"],
    ]
    assert calls[3][1]["headers"]["x-shore-csrf"] == "csrf-two"
    assert "authorization" not in calls[3][1]["headers"]
    assert "registered Shore host" in capsys.readouterr().out
    assert _load_runtime_config(tmp_path / "shore") == ShoreRuntimeConfig(
        "https://agentsquid.ai", "alice", "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1)
