from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import instance_auth


def test_pairing_is_atomic_and_credentials_expire(tmp_path):
    now = 0
    credentials = instance_auth.InstanceCredentials(tmp_path / "credentials", clock=lambda: now)
    code = credentials.issue()
    expired_code = credentials.issue()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(credentials.redeem, [code, code]))
    tokens = [token for token in results if token]
    assert len(tokens) == 1
    assert credentials.session_id(tokens[0])

    now = instance_auth.PAIRING_LIFETIME
    assert credentials.redeem(expired_code) is None
    now = instance_auth.SESSION_LIFETIME
    assert credentials.session_id(tokens[0]) is None


def test_exchange_checks_origin_and_sets_session_cookie(tmp_path, monkeypatch):
    credentials = instance_auth.InstanceCredentials(tmp_path / "credentials")
    monkeypatch.setattr(instance_auth, "instance_credentials", lambda: credentials)
    app = FastAPI()
    app.include_router(instance_auth.router)
    code = credentials.issue()
    remote = TestClient(app, base_url="http://100.64.0.1")
    local = TestClient(app, base_url="http://127.0.0.1")
    https = TestClient(app, base_url="https://squid.example")

    def exchange(client, origin, code):
        return client.post("/instance/exchange", json={"code": code}, headers={"Origin": origin})

    assert exchange(remote, "http://100.64.0.1", code).status_code == 403
    assert exchange(local, "https://elsewhere.example", code).status_code == 403
    response = exchange(https, "https://squid.example", code)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert credentials.session_id(response.cookies[instance_auth.SESSION_COOKIE])
    assert exchange(https, "https://squid.example", code).status_code == 401

    response = exchange(local, "http://127.0.0.1", credentials.issue())
    assert response.status_code == 204
    assert "Secure" not in response.headers["set-cookie"]


def test_remote_pairing_requires_an_active_same_origin_session(tmp_path, monkeypatch):
    from agent import server

    now = 0
    credentials = instance_auth.InstanceCredentials(tmp_path / "credentials", clock=lambda: now)
    monkeypatch.setattr(instance_auth, "instance_credentials", lambda: credentials)
    monkeypatch.setattr(server, "_tailscale_remote_access", lambda: {"url": "https://squid.example/"})
    client = TestClient(server.app, base_url="http://127.0.0.1")
    origin = {"Origin": "http://127.0.0.1"}
    assert client.post("/remote", headers=origin).status_code == 401

    code = credentials.issue()
    assert client.post("/instance/exchange", json={"code": code}, headers=origin).status_code == 204
    assert client.post("/remote", headers={"Origin": "https://other.example"}).status_code == 403
    offer = client.post("/remote", headers=origin)
    assert offer.status_code == 200
    assert offer.headers["cache-control"] == "no-store"
    url, code = offer.json()["pair_url"].split("#code=")
    assert url == "https://squid.example/instance/pair"

    remote = TestClient(server.app, base_url="https://squid.example")
    response = remote.post("/instance/exchange", json={"code": code},
                           headers={"Origin": "https://squid.example"})
    assert response.status_code == 204
    assert "Secure" in response.headers["set-cookie"]
    now = instance_auth.SESSION_LIFETIME
    assert client.post("/remote", headers=origin).status_code == 401


def test_browser_gate_protects_http_and_websocket(tmp_path, monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    from agent import server

    now = 0
    credentials = instance_auth.InstanceCredentials(tmp_path / "credentials", clock=lambda: now)
    monkeypatch.setattr(instance_auth, "instance_credentials", lambda: credentials)
    client = TestClient(server.app, base_url="https://squid.example", client=("127.0.0.1", 5000))
    origin = {"Origin": "https://squid.example"}

    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/instance/pair"
    guide = client.get("/instance/pair")
    assert guide.status_code == 200
    assert "Generate pairing QR" in guide.text
    assert "Locked out remotely?" in guide.text
    assert client.get("/favicon.png").status_code == 200
    assert client.get("/topics").status_code == 401
    assert client.post("/cmd", headers=origin, json={"command": "list"}).status_code == 401
    assert set(client.get("/health").json()) == {"status", "boot_time"}
    assert client.post("/agent-history/search", json={}).status_code == 403

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://squid.example/ws/v1", headers=origin):
            pass

    code = credentials.issue()
    assert client.post("/instance/exchange", headers=origin, json={"code": code}).status_code == 204
    assert client.get("/topics").status_code == 200
    assert client.post("/cmd", json={"command": "list"}).status_code == 403
    assert client.post("/cmd", headers=origin, json={"command": "list"}).status_code == 200
    assert client.post("/instance/restart", headers=origin).status_code == 401

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://squid.example/ws/v1", headers={"Origin": "https://elsewhere.example"}):
            pass

    with client.websocket_connect("wss://squid.example/ws/v1", headers=origin) as socket:
        assert socket.receive_json()["type"] == "hello"
        now = instance_auth.SESSION_LIFETIME
        socket.send_text("{}")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert client.get("/topics").status_code == 401


def test_host_control_is_limited_to_explicit_operations(monkeypatch):
    from agent import server

    monkeypatch.setattr(instance_auth, "host_token", lambda: "host-token")
    commands = []

    async def fake_command(req):
        commands.append(req.command)
        return {"ok": True}

    monkeypatch.setattr(server, "run_cmd", fake_command)
    client = TestClient(server.app, base_url="http://127.0.0.1")
    auth = {"Authorization": "Bearer host-token"}
    monkeypatch.setattr(server, "list_active_procs", lambda: [{"topic": "private"}])
    count_url = "/instance/active-process-count"
    assert client.get(count_url).status_code == 401
    assert client.get(count_url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(count_url, headers=auth).json() == {"count": 1}
    assert client.get("/processes", headers=auth).status_code == 401
    assert client.post("/instance/shutdown").status_code == 401
    assert client.post("/instance/restart", headers=auth).json() == {"ok": True}
    assert client.post("/instance/shutdown", headers=auth).json() == {"ok": True}
    assert client.get("/topics", headers=auth).status_code == 401
    assert commands == ["restart", "shutdown"]


def test_http_keychain_unlock_requires_pairing_and_owns_prompt(monkeypatch):
    from fastapi.testclient import TestClient

    from agent import auth_sessions, server
    from tests.browser_client import PairedBrowserClient

    origin = "https://squid.example"
    owner = PairedBrowserClient(server.app, base_url=origin, headers={"x-forwarded-for": "100.64.0.2"})
    other = PairedBrowserClient(server.app, base_url=origin)
    guest = TestClient(server.app, base_url=origin, headers={"Origin": origin})
    monkeypatch.setitem(server._cfg, "auth", {"allow_remote_keychain_unlock": False})
    session = auth_sessions.AuthSession("unlock-test", "keychain", 0, -1, "security unlock-keychain")
    starts = []

    async def start(target, cols, rows, *, mode, model):
        starts.append((target, mode))
        return session

    monkeypatch.setattr(auth_sessions, "create_session", start)
    monkeypatch.setitem(auth_sessions._sessions, session.id, session)
    body = {"harness": "keychain", "mode": "unlock", "cols": 80, "rows": 24}
    assert guest.post("/auth/session", json=body).status_code == 401
    assert owner.post("/auth/session", json=body, headers={"Origin": "https://elsewhere.example"}).status_code == 403
    assert starts == []
    assert owner.post("/auth/session", json=body).json()["id"] == session.id
    assert starts == [("keychain", "unlock")]
    assert owner.get(f"/auth/session/{session.id}").status_code == 200
    assert other.get(f"/auth/session/{session.id}").status_code == 404
