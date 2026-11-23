"""Browser pairing credentials and sessions for the Squid instance."""
import hashlib
import ipaddress
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.requests import HTTPConnection
from starlette.concurrency import run_in_threadpool

PAIRING_LIFETIME = 5 * 60
SESSION_LIFETIME = 30 * 24 * 60 * 60
SESSION_COOKIE = "squid_instance_session"

router = APIRouter(prefix="/instance")


def _digest(credential: str) -> bytes:
    return hashlib.sha256(credential.encode()).digest()


class InstanceCredentials:
    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pairing_credentials (
                        digest BLOB PRIMARY KEY, expires_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS device_sessions (
                        id TEXT PRIMARY KEY, digest BLOB NOT NULL UNIQUE,
                        created_at REAL NOT NULL, expires_at REAL NOT NULL
                    )
                """)
                yield conn
        finally:
            conn.close()

    def issue(self, *, session_token: str | None = None) -> str | None:
        code = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if session_token is not None and not self._active_session(conn, session_token, now):
                return None
            conn.execute("DELETE FROM pairing_credentials WHERE expires_at <= ?", (now,))
            conn.execute("INSERT INTO pairing_credentials VALUES (?, ?)",
                         (_digest(code), now + PAIRING_LIFETIME))
        return code

    def redeem(self, code: str) -> str | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock()
            consumed = conn.execute(
                "DELETE FROM pairing_credentials WHERE digest = ? AND expires_at > ?",
                (_digest(code), now),
            )
            if not consumed.rowcount:
                return None
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO device_sessions VALUES (?, ?, ?, ?)", (
                secrets.token_hex(16), _digest(token), now, now + SESSION_LIFETIME,
            ))
        return token

    def session_id(self, token: str) -> str | None:
        with self._connect() as conn:
            return self._active_session(conn, token, self.clock())

    @staticmethod
    def _active_session(conn, token: str, now: float) -> str | None:
        row = conn.execute(
            "SELECT id FROM device_sessions WHERE digest = ? AND expires_at > ?",
            (_digest(token), now),
        ).fetchone()
        return row[0] if row else None


def instance_credentials() -> InstanceCredentials:
    return InstanceCredentials(Path.home() / ".squid" / "instance-auth" / "credentials.sqlite3")


def host_token(*, create=False):
    from .watch_auth import file_token

    return file_token(create=create, path=Path.home() / ".squid" / "instance-auth" / "host.token")


def host_authorized(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return False
    expected = host_token()
    return expected is not None and secrets.compare_digest(
        authorization[7:].encode(), expected.encode(),
    )


def browser_session(connection: HTTPConnection) -> str | None:
    token = connection.cookies.get(SESSION_COOKIE)
    if not token or len(token) != 43 or not browser_origin(connection):
        return None
    return instance_credentials().session_id(token)


def browser_origin(request: HTTPConnection) -> str | None:
    host = request.headers.get("host", "")
    scheme = {"ws": "http", "wss": "https"}.get(request.scope["scheme"], request.scope["scheme"])
    try:
        url = urlsplit(f"{scheme}://{host}")
        if url.netloc != host or url.username is not None or url.path or url.query or url.fragment:
            return None
        if scheme == "https" and url.hostname:
            return f"https://{host}"
        if scheme == "http" and url.hostname:
            if url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback:
                return f"http://{host}"
    except ValueError:
        pass
    return None


@router.post("/exchange")
async def exchange(request: Request):
    origin = browser_origin(request)
    headers = {"Cache-Control": "no-store"}
    if origin is None or request.headers.get("origin") != origin:
        return JSONResponse({"detail": "Pairing requires a same-origin HTTPS or localhost request"},
                            status_code=403, headers=headers)
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str) or len(code) != 43 or not code.isascii():
        return JSONResponse({"detail": "Invalid pairing request"}, status_code=400, headers=headers)
    token = await run_in_threadpool(instance_credentials().redeem, code)
    if not token:
        return JSONResponse({"detail": "Pairing link is invalid or expired"},
                            status_code=401, headers=headers)
    response = Response(status_code=204, headers=headers)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_LIFETIME,
                        httponly=True, samesite="strict", secure=origin.startswith("https:"))
    return response


@router.get("/pair", response_class=HTMLResponse)
def pairing_page():
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair browser · AgentSquid</title>
<link rel="icon" href="/favicon.png">
<style nonce="{nonce}">
  :root {{
    color-scheme: dark;
    font: 100%/1.55 'JetBrains Mono', ui-monospace, monospace;
    color: #e8e4dc;
    background: #0f0f13;
    --bubble: #171721;
    --line: #313149;
    --accent: #d07840;
  }}
  body {{ margin: 0; padding: clamp(1rem, 5vw, 3rem); }}
  main {{ width: min(100%, 46rem); margin: 0 auto; }}
  header {{ display: flex; align-items: center; justify-content: center; gap: clamp(.75rem, 4vw, 2rem); }}
  header img {{ width: clamp(7rem, 30vw, 16rem); height: auto; flex: none; }}
  .bubble {{
    position: relative; min-width: 0; padding: clamp(.55rem, 2vw, 1rem) clamp(.65rem, 2.5vw, 1.5rem);
    border: 1px solid var(--line); border-radius: .6rem;
    background: var(--bubble); color: var(--accent);
    box-shadow: 0 10px 24px rgba(0, 0, 0, .18);
  }}
  .bubble::before {{
    content: ""; position: absolute; left: -.4rem; top: 50%;
    width: .65rem; height: .65rem; background: var(--bubble);
    border-left: 1px solid var(--line); border-bottom: 1px solid var(--line);
    transform: translateY(-50%) rotate(45deg);
  }}
  p {{ margin: 0; }}
  .bubble p {{ font-size: clamp(.82rem, 2vw, 1rem); line-height: 1.35; }}
  section {{ border-top: 1px solid var(--line); margin-top: 1.5rem; padding-top: 1rem; }}
  h2 {{ font-size: 1rem; margin: 0 0 .55rem; }}
  ol {{ margin: 0; padding-left: 1.3rem; }}
  li + li {{ margin-top: .4rem; }}
  code {{
    color: #f5a060; background: var(--bubble); padding: .12em .35em;
    border-radius: .25rem; overflow-wrap: anywhere;
  }}
</style>
<main>
  <header>
    <img src="/favicon.png" width="400" height="400" alt="">
    <div class="bubble">
      <p id="status" role="status">Pair this browser to connect to Squid.</p>
    </div>
  </header>
  <section>
    <h2>On the Squid host</h2>
    <ol>
      <li>Run <code>agentsquid pair-browser</code> in a terminal.</li>
      <li>Open the link it prints in this browser within five minutes.</li>
    </ol>
  </section>
  <section>
    <h2>On a phone or another Tailscale device</h2>
    <ol>
      <li>In an already paired browser, enter <code>/remote</code> and select Tailscale.</li>
      <li>Choose <strong>Generate pairing QR</strong>, then scan it or open the copied HTTPS link on this device.</li>
    </ol>
  </section>
  <section>
    <h2>Locked out remotely?</h2>
    <p>SSH to the Squid host and forward its Squid port to localhost. Run
    <code>agentsquid pair-browser</code> there, then open the printed localhost
    link on your computer while the tunnel is connected.</p>
  </section>
</main>
<script nonce="{nonce}">
  const code = new URLSearchParams(location.hash.slice(1)).get('code');
  history.replaceState(null, '', location.pathname);
  if (code) {{
    const status = document.getElementById('status');
    status.textContent = 'Pairing browser…';
    fetch('/instance/exchange', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{code}})
    }}).then(response => {{
      if (response.ok) location.replace('/');
      else status.textContent = 'Pairing failed. Generate a fresh link with agentsquid pair-browser.';
    }}).catch(() => {{
      status.textContent = 'Could not reach Squid. Generate a fresh pairing link and try again.';
    }});
  }}
</script>
</html>""", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
    })


def pair_browser_cli(argv):
    import argparse

    parser = argparse.ArgumentParser(prog="agentsquid pair-browser",
                                     description="Print a single-use pairing link for this host.")
    parser.parse_args(argv)
    from .config import _cfg

    host = ipaddress.IPv4Address(_cfg["server"]["host"])
    if not host.is_loopback:
        parser.error("the configured server host must be an IPv4 loopback address")
    port = int(_cfg["server"]["port"])
    code = instance_credentials().issue()
    print(f"http://{host}:{port}/instance/pair#code={code}")
