import os
import stat

import pytest
import httpx

from agent.shore import (
    ShoreRuntimeConfig, _confirm_custom_relay, _load_or_new_identity, _load_runtime_config,
    _new_identity, _print_totp_qr, _registration_proof, _require_response,
    _write_runtime_config, login, pair,
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
    expected = ShoreRuntimeConfig("https://relay.example", "alice",
        "018f1f25-3f6b-7d75-a4d1-62d771381b20", 2)
    _write_runtime_config(identity, expected)
    assert _load_runtime_config(identity) == expected
    assert stat.S_IMODE(identity.stat().st_mode) == 0o700
    assert stat.S_IMODE((identity / "connection.json").stat().st_mode) == 0o600

    replacement = ShoreRuntimeConfig("https://relay.example", "alice",
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
            "https://relay.example", "admin",
            "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1,
        ))
    assert not identity.exists()
    with pytest.raises(RuntimeError, match="refusing to persist invalid"):
        _write_runtime_config(identity, ShoreRuntimeConfig(
            "https://relay.example", "alice",
            "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1 << 53,
        ))


def test_runtime_config_rejects_unsafe_or_invalid_metadata(tmp_path):
    identity = tmp_path / "shore"
    _write_runtime_config(identity, ShoreRuntimeConfig("https://relay.example", "alice",
        "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1))
    os.chmod(identity / "connection.json", 0o644)
    with pytest.raises(RuntimeError, match="unsafe permissions"):
        _load_runtime_config(identity)
    os.chmod(identity / "connection.json", 0o600)
    (identity / "connection.json").write_text('{"relay":"file:///tmp","username":"Alice"}')
    with pytest.raises(RuntimeError, match="invalid Shore connection"):
        _load_runtime_config(identity)
    (identity / "connection.json").write_text(
        '{"account_id":"018f1f25-3f6b-7d75-a4d1-62d771381b20",'
        '"relay":"https://example.com","key_epoch":1,"username":"admin"}'
    )
    with pytest.raises(RuntimeError, match="invalid Shore connection"):
        _load_runtime_config(identity)
    (identity / "connection.json").write_text(
        '{"account_id":"018f1f25-3f6b-7d75-a4d1-62d771381b20",'
        '"relay":42,"key_epoch":1,"username":"alice"}'
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


def test_login_rejects_malformed_or_credentialed_relay_urls():
    for relay in ("not-a-url", "https://user:secret@example.com", "ftp://example.com",
                   "https://[invalid", "https://example.com:invalid", "https://exa mple.com",
                   "http://relay.example"):
        with pytest.raises(SystemExit):
            login(["--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                   "--session-token", "session", "--relay", relay])


def test_login_requires_exact_typed_confirmation_for_custom_relay(tmp_path, monkeypatch, capsys):
    class Client:
        def __init__(self, **_kwargs):
            raise AssertionError("network client must not be constructed")

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    monkeypatch.setattr("builtins.input", lambda _prompt: "https://relay.example.evil")
    result = login(["--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20",
        "--session-token", "session", "--relay", "https://relay.example",
        "--identity-dir", str(tmp_path / "shore")])
    assert result == 1
    assert "custom relay confirmation did not match" in capsys.readouterr().err
    assert not (tmp_path / "shore").exists()


def test_first_party_relay_origins_do_not_require_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))
    _confirm_custom_relay("https://agentsquid.ai")
    _confirm_custom_relay("https://agentsquid.ai/")
    _confirm_custom_relay("https://dev.agentsquid.ai")


def test_other_agentsquid_subdomains_still_require_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    with pytest.raises(RuntimeError, match="custom relay confirmation did not match"):
        _confirm_custom_relay("https://preview.agentsquid.ai")


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
            raise httpx.ConnectError("relay unavailable")

    monkeypatch.setattr("agent.shore.httpx.Client", FailingClient)
    result = login([
        "--account-id", "018f1f25-3f6b-7d75-a4d1-62d771381b20", "--session-token", "session",
        "--identity-dir", str(tmp_path / "shore"),
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "ERROR: Shore login failed: relay unavailable\n"


def test_login_performs_email_and_second_factor_flow_without_session_token(tmp_path, monkeypatch, capsys):
    calls = []

    class Response:
        def __init__(self, value, status_code=200):
            self.value = value
            self.status_code = status_code

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
            if url.endswith("/auth/totp/enroll"):
                return Response({}, 409)
            if url.endswith("/auth/step-up"):
                return Response({"csrfToken": "csrf-two"})
            if url.endswith("/host/challenge"):
                return Response({"id": "challenge", "nonce": "nonce"})
            request = kwargs["json"]
            return Response({"registered": True, "accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                "username": "alice", "keyEpoch": 1, "id": request["hostId"],
                "signingKey": request["signingKey"], "agreementKey": request["agreementKey"]})

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    codes = iter(["magic", "123456"])
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: next(codes))
    result = login([
        "--username", "alice", "--email", "alice@example.com",
        "--identity-dir", str(tmp_path / "shore"),
    ])
    assert result == 0
    assert [url.rsplit("/", 2)[-2:] for url, _ in calls] == [
        ["auth", "magic-link"], ["auth", "consume"], ["totp", "enroll"], ["auth", "step-up"],
        ["host", "challenge"], ["host", "register"],
    ]
    assert calls[4][1]["headers"]["x-shore-csrf"] == "csrf-two"
    assert "authorization" not in calls[4][1]["headers"]
    assert "registered Shore host" in capsys.readouterr().out
    assert _load_runtime_config(tmp_path / "shore") == ShoreRuntimeConfig(
        "https://agentsquid.ai", "alice", "018f1f25-3f6b-7d75-a4d1-62d771381b20", 1)


class _ScriptedRelay:
    """httpx.Client stand-in answering each endpoint suffix from `routes`; a
    list value is consumed one response per call."""

    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    def __call__(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                if isinstance(answer, list):
                    answer = answer.pop(0)
                value, status = answer if isinstance(answer, tuple) else (answer, 200)
                return httpx.Response(status, json=value, request=httpx.Request("POST", url))
        request = kwargs["json"]
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b20", "username": "alice", "keyEpoch": 1,
            "id": request["hostId"], "signingKey": request["signingKey"], "agreementKey": request["agreementKey"]})


def test_login_approves_a_passkey_only_account_in_the_browser(tmp_path, monkeypatch, capsys):
    calls = []
    relay = _ScriptedRelay({
        "/auth/magic-link": {"sent": True},
        "/auth/consume": ({"csrfToken": "csrf-one", "factors": ["passkey"]}, 201),
        "/auth/approval/start": ({"approvalId": "018f1f25-3f6b-7d75-a4d1-62d771381b99", "code": "ABCD-EFGH",
                                  "approvePath": "/@alice/approve", "expiresAt": 1}, 201),
        "/auth/approval/poll": [({"status": "pending"}, 202), {"csrfToken": "csrf-two", "state": "remote_authenticated"}],
        "/host/challenge": ({"id": "challenge", "nonce": "nonce"}, 201),
    }, calls)
    monkeypatch.setattr("agent.shore.httpx.Client", relay)
    monkeypatch.setattr("agent.shore.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: "magic")
    assert login(["--username", "alice", "--email", "alice@example.com", "--identity-dir", str(tmp_path / "shore")]) == 0
    assert [url.rsplit("/", 2)[-2:] for url, _ in calls] == [
        ["auth", "magic-link"], ["auth", "consume"], ["approval", "start"], ["approval", "poll"], ["approval", "poll"],
        ["host", "challenge"], ["host", "register"],
    ]
    assert calls[3][1]["json"] == {"approvalId": "018f1f25-3f6b-7d75-a4d1-62d771381b99"}
    assert calls[5][1]["headers"]["x-shore-csrf"] == "csrf-two"
    err = capsys.readouterr().err
    assert "https://agentsquid.ai/@alice/approve" in err and "ABCD-EFGH" in err


def test_login_with_both_factors_uses_a_typed_code_or_falls_back_to_the_passkey(tmp_path, monkeypatch):
    for typed, expected in (("123456", ["auth", "step-up"]), ("", ["approval", "start"])):
        calls = []
        relay = _ScriptedRelay({
            "/auth/magic-link": {"sent": True},
            "/auth/consume": ({"csrfToken": "csrf-one", "factors": ["totp", "passkey"]}, 201),
            "/auth/step-up": {"csrfToken": "csrf-two"},
            "/auth/approval/start": ({"approvalId": "018f1f25-3f6b-7d75-a4d1-62d771381b99", "code": "ABCD-EFGH",
                                      "approvePath": "/@alice/approve", "expiresAt": 1}, 201),
            "/auth/approval/poll": {"csrfToken": "csrf-two"},
            "/host/challenge": ({"id": "challenge", "nonce": "nonce"}, 201),
        }, calls)
        monkeypatch.setattr("agent.shore.httpx.Client", relay)
        monkeypatch.setattr("agent.shore.time.sleep", lambda _seconds: None)
        answers = iter(["magic", typed])
        monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: next(answers))
        assert login(["--username", "alice", "--email", "alice@example.com", "--identity-dir", str(tmp_path / typed / "shore")]) == 0
        assert calls[2][0].rsplit("/", 2)[-2:] == expected


def test_login_reports_an_expired_passkey_approval(tmp_path, monkeypatch, capsys):
    relay = _ScriptedRelay({
        "/auth/magic-link": {"sent": True},
        "/auth/consume": ({"csrfToken": "csrf-one", "factors": ["passkey"]}, 201),
        "/auth/approval/start": ({"approvalId": "018f1f25-3f6b-7d75-a4d1-62d771381b99", "code": "ABCD-EFGH",
                                  "approvePath": "/@alice/approve", "expiresAt": 1}, 201),
        "/auth/approval/poll": ({"error": "approval_expired"}, 410),
    }, [])
    monkeypatch.setattr("agent.shore.httpx.Client", relay)
    monkeypatch.setattr("agent.shore.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: "magic")
    assert login(["--username", "alice", "--email", "alice@example.com", "--identity-dir", str(tmp_path / "shore")]) == 1
    assert "passkey approval expired" in capsys.readouterr().err


def test_login_creates_a_missing_account_email_first(tmp_path, monkeypatch, capsys):
    calls = []
    relay = _ScriptedRelay({
        "/auth/magic-link": ({"error": "unknown_username"}, 404),
        "/signup/start": ({"sent": True}, 202),
        "/signup/verify": ({"csrfToken": "csrf-one", "factors": []}, 201),
        "/signup/handle": {"username": "alice", "expiresAt": 1},
        "/auth/totp/enroll": ({"secret": "ABCDEFGHIJKLMNOP"}, 201),
        "/auth/step-up": {"csrfToken": "csrf-two"},
        "/host/challenge": ({"id": "challenge", "nonce": "nonce"}, 201),
    }, calls)
    monkeypatch.setattr("agent.shore.httpx.Client", relay)
    monkeypatch.setattr("agent.shore._print_totp_qr", lambda *_args: None)
    answers = iter(["magic", "123456"])
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: next(answers))
    assert login(["--username", "alice", "--email", "alice@example.com", "--identity-dir", str(tmp_path / "shore")]) == 0
    assert [url for url, _ in calls[:4]] == [
        "https://agentsquid.ai/@alice/auth/magic-link", "https://agentsquid.ai/signup/start",
        "https://agentsquid.ai/signup/verify", "https://agentsquid.ai/signup/handle",
    ]
    assert calls[2][1]["json"] == {"email": "alice@example.com", "token": "magic"}
    assert calls[3][1] == {"headers": {"x-shore-csrf": "csrf-one"}, "json": {"email": "alice@example.com", "handle": "alice"}}
    assert calls[6][1]["headers"]["x-shore-csrf"] == "csrf-two"


def test_login_explains_a_taken_username_during_signup(tmp_path, monkeypatch, capsys):
    relay = _ScriptedRelay({
        "/auth/magic-link": ({"error": "unknown_username"}, 404),
        "/signup/start": ({"sent": True}, 202),
        "/signup/verify": ({"csrfToken": "csrf-one", "factors": []}, 201),
        "/signup/handle": ({"error": "username_taken"}, 409),
    }, [])
    monkeypatch.setattr("agent.shore.httpx.Client", relay)
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: "magic")
    assert login(["--username", "alice", "--email", "alice@example.com", "--identity-dir", str(tmp_path / "shore")]) == 1
    assert "username is already taken" in capsys.readouterr().err


def test_login_prints_totp_enrollment_qr(tmp_path, monkeypatch, capsys):
    calls = []

    class Response:
        def __init__(self, value, status_code=200):
            self.value = value
            self.status_code = status_code

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
            if url.endswith("/auth/magic-link"):
                return Response({"sent": True})
            if url.endswith("/auth/consume"):
                return Response({"csrfToken": "csrf-one"})
            if url.endswith("/auth/totp/enroll"):
                return Response({"secret": "ABCDEFGHIJKLMNOP"}, 201)
            if url.endswith("/auth/step-up"):
                return Response({"csrfToken": "csrf-two"})
            if url.endswith("/host/challenge"):
                return Response({"id": "challenge", "nonce": "nonce"})
            request = kwargs["json"]
            return Response({"accountId": "018f1f25-3f6b-7d75-a4d1-62d771381b20",
                "username": "alice", "keyEpoch": 1, "id": request["hostId"],
                "signingKey": request["signingKey"], "agreementKey": request["agreementKey"]})

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    monkeypatch.setattr("agent.shore._print_totp_qr", lambda secret, username, relay: calls.append((secret, username, relay)))
    codes = iter(["magic", "123456"])
    monkeypatch.setattr("agent.shore.getpass.getpass", lambda _prompt: next(codes))
    assert login(["--username", "alice", "--email", "alice@example.com",
        "--identity-dir", str(tmp_path / "shore")]) == 0
    assert calls == [("ABCDEFGHIJKLMNOP", "alice", "https://agentsquid.ai")]
    assert "Scan this QR code with your authenticator app:" in capsys.readouterr().err


def test_totp_qr_distinguishes_dev_issuer(monkeypatch):
    values = []

    class QR:
        def __init__(self, **_kwargs): pass
        def add_data(self, value): values.append(value)
        def print_ascii(self, **_kwargs): pass

    monkeypatch.setattr("agent.shore.qrcode.QRCode", QR)
    _print_totp_qr("SECRET", "alice", "https://dev.agentsquid.ai")
    _print_totp_qr("SECRET", "alice", "https://agentsquid.ai")
    assert "AgentSquid%20%28dev%29%3A%40alice" in values[0]
    assert "issuer=AgentSquid+%28dev%29" in values[0]
    assert "AgentSquid%3A%40alice" in values[1]
    assert "issuer=AgentSquid&" in values[1]


def test_response_errors_reject_redirects_and_explain_nonreusable_host():
    with pytest.raises(RuntimeError, match="HTTP 302"):
        _require_response(httpx.Response(302, request=httpx.Request("POST", "https://relay.example")), "login")
    response = httpx.Response(409, json={"error": "host_id_not_reusable"},
        request=httpx.Request("POST", "https://relay.example"))
    with pytest.raises(RuntimeError, match="fresh identity directory"):
        _require_response(response, "host registration")


def test_login_explains_existing_host_conflict(tmp_path, monkeypatch, capsys):
    class Response:
        status_code = 409
        def json(self): return {"error": "current_host_exists"}
        def raise_for_status(self): raise AssertionError("expected specific conflict handling")

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr("agent.shore.httpx.Client", Client)
    result = login(["--username", "alice", "--session-token", "session",
        "--identity-dir", str(tmp_path / "shore")])
    assert result == 1
    assert "already has a different registered host" in capsys.readouterr().err


_REAL_HTTPX_CLIENT = httpx.Client


def _pair_client(monkeypatch, handler):
    def client(**kwargs):
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("agent.shore.httpx.Client", client)


def test_pair_prints_qr_and_waits_until_paired(monkeypatch, capsys):
    statuses = iter(["pending", "paired"])

    def handler(request):
        if request.url.path == "/shore/pairing/begin":
            return httpx.Response(200, json={
                "ceremony_id": "018f1f25-c930-76f0-86e7-cb06d94e6a32", "code": "ABCD-EFGH",
                "expires_at": 4102444800, "pair_url": "https://agentsquid.ai/@alice/pair?v=1#offer",
            })
        assert request.url.params["ceremony_id"] == "018f1f25-c930-76f0-86e7-cb06d94e6a32"
        return httpx.Response(200, json={"status": next(statuses)})

    _pair_client(monkeypatch, handler)
    assert pair([], "http://127.0.0.1:1", poll_seconds=0) == 0
    out = capsys.readouterr().out
    assert "https://agentsquid.ai/@alice/pair?v=1#offer" in out
    assert "code: ABCD-EFGH" in out
    assert "Paired ✓" in out


def test_pair_reports_expired_code(monkeypatch, capsys):
    def handler(request):
        if request.url.path == "/shore/pairing/begin":
            return httpx.Response(200, json={
                "ceremony_id": "018f1f25-c930-76f0-86e7-cb06d94e6a32", "code": "ABCD",
                "expires_at": 0, "pair_url": "https://agentsquid.ai/@alice/pair#offer",
            })
        return httpx.Response(200, json={"status": "expired"})

    _pair_client(monkeypatch, handler)
    assert pair([], "http://127.0.0.1:1", poll_seconds=0) == 1
    assert "expired" in capsys.readouterr().err


def test_pair_explains_missing_login_and_stopped_server(monkeypatch, capsys):
    _pair_client(monkeypatch, lambda request: httpx.Response(400, json={"error": "shore_not_configured"}))
    assert pair([], "http://127.0.0.1:1") == 1
    assert "agentsquid login" in capsys.readouterr().err

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    _pair_client(monkeypatch, refused)
    assert pair([], "http://127.0.0.1:1") == 1
    assert "agentsquid start" in capsys.readouterr().err


def test_pair_requests_approves_picked_choice_and_rejects_blank(monkeypatch, capsys):
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"requests": [
                {"request_id": "r1", "device_id": "d1", "choices": ["AAAA", "BBBB", "CCCC"]},
                {"request_id": "r2", "device_id": "d2", "choices": ["DDDD", "EEEE", "FFFF"]},
            ]})
        posted.append((request.url.path, request.read()))
        return httpx.Response(200, json={"ok": True})

    _pair_client(monkeypatch, handler)
    answers = iter(["2", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert pair(["--requests"], "http://127.0.0.1:1") == 0
    assert posted == [
        ("/shore/pairing/requests/approve", b'{"request_id":"r1","verification_code":"BBBB"}'),
        ("/shore/pairing/requests/reject", b'{"request_id":"r2"}'),
    ]
    out = capsys.readouterr().out
    assert "Approved ✓" in out and "Rejected." in out
