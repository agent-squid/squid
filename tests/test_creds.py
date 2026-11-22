import json
import stat

import pytest

from agent import creds


def test_max_budget_roundtrip_per_gauge(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "_CREDS_PATH", tmp_path / "squid-creds.json")

    creds.save_max_budget("kimi", 25.0)
    creds.save_max_budget("deepseek", 10.0)

    assert creds.get_max_budget("kimi") == 25.0
    assert creds.get_max_budget("deepseek") == 10.0

    creds.clear_max_budget("kimi")
    assert creds.get_max_budget("kimi") is None
    assert creds.get_max_budget("deepseek") == 10.0


def test_deepseek_max_budget_legacy_key_migrates(tmp_path, monkeypatch):
    path = tmp_path / "squid-creds.json"
    path.write_text(json.dumps({"deepseek_max_budget": 20.0}))
    monkeypatch.setattr(creds, "_CREDS_PATH", path)

    # pre-registry installs stored the cap under a flat deepseek-only key
    assert creds.get_max_budget("deepseek") == 20.0

    creds.save_max_budget("deepseek", 30.0)
    data = json.loads(path.read_text())
    assert "deepseek_max_budget" not in data
    assert creds.get_max_budget("deepseek") == 30.0


def test_codex_browser_import_replaces_account_with_token(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "_CREDS_PATH", tmp_path / "squid-creds.json")
    creds.save_codex("token-one", "workspace-one")
    assert creds.get_codex_token() == "token-one"
    assert creds.get_codex_account_id() == "workspace-one"
    creds.save_codex("token-two")
    assert creds.get_codex_token() == "token-two"
    assert not creds.get_codex_account_id()


def test_claude_meter_requires_confirmation_of_matching_account(tmp_path, monkeypatch):
    from curl_cffi import requests
    from fastapi.testclient import TestClient

    from agent import server

    class AccountResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "email_address": "personal@example.com",
                "memberships": [{"organization": {"uuid": "personal-org", "name": "Personal"}}],
            }

    usage_status = 403
    usage_body = {"five_hour": {"utilization": 42}}

    class UsageResponse:
        @property
        def status_code(self):
            return usage_status

        def json(self):
            return usage_body

    class ClaudeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **kwargs):
            assert kwargs["headers"]["Cookie"] == "sessionKey=personal-session"
            if url == "https://claude.ai/api/account":
                return AccountResponse()
            assert url == "https://claude.ai/api/organizations/personal-org/usage"
            return UsageResponse()

    monkeypatch.setattr(creds, "_CREDS_PATH", tmp_path / "squid-creds.json")
    monkeypatch.setattr(requests, "AsyncSession", ClaudeSession)
    monkeypatch.setattr(server, "_claude_meter_preview", None)
    creds.save("work-org", "work-session", "work@example.com")
    client = TestClient(server.app)
    body = {"claude_org_id": "work-org", "claude_session_key": "personal-session"}

    legacy = client.post("/config/creds", json=body)
    assert legacy.status_code == 409
    assert "Refresh Squid" in legacy.json()["error"]
    legacy_auto = client.post("/config/creds/auto")
    assert legacy_auto.status_code == 409
    assert "Refresh Squid" in legacy_auto.json()["error"]
    assert client.post("/config/creds/preview", json=body).status_code == 422
    assert creds.get_session_key() == "work-session"

    body["claude_org_id"] = "personal-org"
    response = client.post("/config/creds/preview", json=body)
    assert response.status_code == 200
    assert "personal-session" not in response.text
    preview = response.json()
    assert preview["email"] == "personal@example.com"
    assert preview["organization"] == "Personal"
    assert preview["account_changed"] is True
    assert preview["previous_email"] == "work@example.com"
    assert creds.get_session_key() == "work-session"

    confirmation = {"confirmation": preview["confirmation"]}
    assert client.post("/config/creds/confirm", json=confirmation).status_code == 502
    assert creds.get_session_key() == "work-session"

    usage_status = 200
    usage_body = {}
    preview = client.post("/config/creds/preview", json=body).json()
    confirmation = {"confirmation": preview["confirmation"]}
    assert client.post("/config/creds/confirm", json=confirmation).status_code == 502
    assert creds.get_session_key() == "work-session"

    usage_body = {"five_hour": {"utilization": 42}}
    preview = client.post("/config/creds/preview", json=body).json()
    confirmation = {"confirmation": preview["confirmation"]}
    assert client.post("/config/creds/confirm", json=confirmation).status_code == 200
    assert creds.get_session_key() == "personal-session"
    assert client.post("/config/creds/confirm", json=confirmation).status_code == 409


def test_claude_browser_profiles_keep_chrome_channels_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(creds.Path, "home", lambda: tmp_path)
    google = tmp_path / "Library/Application Support/Google"
    for browser in ("Chrome", "Chrome Beta", "Chrome Dev"):
        root = google / browser
        cookies = root / "Default/Network/Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.touch()
        (root / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {"Default": {"name": "Work"}}},
        }))

    profiles = creds.claude_browser_profiles()
    assert profiles["Default"] == ("Chrome · Work (Default)", google / "Chrome/Default/Network/Cookies")
    for browser in ("Chrome Beta", "Chrome Dev"):
        assert profiles[f"{browser}/Default"] == (
            f"{browser} · Work (Default)", google / browser / "Default/Network/Cookies",
        )


def test_claude_meter_endpoints_require_same_origin():
    from fastapi.testclient import TestClient

    from agent import server

    client = TestClient(server.app, base_url="http://localhost", client=("127.0.0.1", 40000))
    headers = {"Origin": "https://other.example"}
    assert client.get("/config/creds/profiles", headers=headers).status_code == 403
    assert client.get("/quota/provider/anthropic_pers/account", headers=headers).status_code == 403

    requests = {
        "/config/creds": {"claude_org_id": "org", "claude_session_key": "test-session"},
        "/config/creds/preview": {"claude_org_id": "org", "claude_session_key": "test-session"},
        "/config/creds/auto": {"profile": "Default"},
        "/config/creds/confirm": {"confirmation": "test-confirmation"},
    }
    for path, body in requests.items():
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["error"] == "Cross-origin Claude meter requests are not allowed."



def test_abandoned_claude_meter_review_drops_pending_credentials(monkeypatch):
    from agent import server

    preview = ("token", 0, "org", "pending-session", "email", (None, None, None))
    monkeypatch.setattr(server, "_claude_meter_preview", preview)
    server._expire_claude_meter_preview("older-token")
    assert server._claude_meter_preview is preview
    server._expire_claude_meter_preview("token")
    assert server._claude_meter_preview is None



def test_meter_credentials_are_written_privately_and_atomically(tmp_path, monkeypatch):
    path = tmp_path / "squid-creds.json"
    monkeypatch.setattr(creds, "_CREDS_PATH", path)
    path.write_text(json.dumps({"codex_token": "existing-token"}))
    path.chmod(0o644)

    creds.save("work-org", "working-session")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert creds.get_codex_token() == "existing-token"

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(creds.Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        creds.save("personal-org", "replacement-session")
    assert creds.get_session_key() == "working-session"
    assert list(tmp_path.iterdir()) == [path]



def test_claude_browser_import_ignores_stale_and_path_scoped_cookies(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import browser_cookie3

    cookie_file = tmp_path / "Cookies"
    monkeypatch.setattr(creds, "claude_browser_profiles", lambda: {
        "Default": ("Chrome", cookie_file),
    })

    def cookie(name, value, *, path="/", domain="claude.ai", expired=False):
        return SimpleNamespace(
            name=name, value=value, path=path, domain=domain, is_expired=lambda: expired,
        )

    def chrome(**kwargs):
        assert kwargs["cookie_file"] == str(cookie_file)
        return [
            cookie("sessionKey", "active"),
            cookie("lastActiveOrg", "personal-org"),
            cookie("sessionKey", "expired", domain=".claude.ai", expired=True),
            cookie("lastActiveOrg", "other-org", path="/settings"),
        ]

    monkeypatch.setattr(browser_cookie3, "chrome", chrome)
    assert creds.read_claude_browser_creds("Default") == {
        "sessionKey": "active", "lastActiveOrg": "personal-org",
    }
