---
status: accepted
date: 2026-06-01
---
# ADR-0016: Security Model — Network Isolation + Bearer Token + Path Allowlist

## Context and Problem Statement

Squid exposes HTTP endpoints that have significant local impact:

- `/chat` — streams agent responses (spawns CLI subprocesses)
- `/cmd` — kills processes, clears sessions, restarts the server
- `/localfile` — serves local files by absolute path

Left open on all interfaces with no auth, these endpoints would let anyone on
the same network read arbitrary files, restart the server, or generate API
costs. The question is what level of access control is appropriate for a
personal, single-user tool.

## Decision Outcome

Three complementary layers, each enforced at startup or in middleware:

### Layer 1 — Host binding restricted to loopback

`main()` validates `server.host` at startup and exits with an error if the
address is not in `127.0.0.0/8`:

| Range | Purpose |
|---|---|
| `127.0.0.0/8` | Loopback — local machine only |

Public IPs, LAN IPs, Tailscale IPs, and `0.0.0.0` are all rejected. Squid
never binds directly to a network interface. Remote access is handled
exclusively by `tailscale serve`, which proxies HTTPS traffic from the
Tailscale network to `127.0.0.1:<port>`.

### Layer 2 — Bearer token (optional but recommended)

If `server.token` is set in `squid.yaml`, an HTTP middleware requires
`Authorization: Bearer <token>` on every non-static-asset request. Static files
(`.js`, `.css`, `.html`, fonts, etc.) are exempt so the UI page can load before
the browser has a token.

**First-visit flow:** type `/remote` in the chat to get a QR code. The URL
embeds the token:

```
https://<machine-name>.<tailnet>.ts.net/?token=<value>
```

The UI reads the token from the URL, stores it in `localStorage`, strips it
from the URL bar, and injects it into every subsequent `fetch()` call via a
`window.fetch` interceptor. No further token entry is needed on that device.

If a request is rejected with 401, the UI shows a full-screen auth banner
explaining the `?token=` flow.

**What the token protects:** a Tailscale-enrolled device that has never been
given the token cannot access any API endpoint. This is a real second factor —
network membership (Tailscale) and token possession are independent gates.

**What the token does not protect against:** a compromised device that has
already authenticated (the token is in `localStorage`, visible in browser
DevTools). The token is also visible in `squid.yaml` to anyone with filesystem
access to the host machine.

### Layer 3 — `/localfile` path allowlist

`server.localfile_roots` in `squid.yaml` is an explicit list of directories
that `/localfile` is permitted to serve from. Requests for paths outside the
list return 403. An empty list disables the endpoint entirely.

This prevents `/localfile` (the highest-risk endpoint — arbitrary file reads)
from being used to read SSH keys, credentials, or other sensitive files even by
an authenticated client.

### Tailscale + MagicDNS setup

Tailscale's MagicDNS assigns stable hostnames within your mesh without touching
public DNS.

**Host setup:**
1. In Tailscale admin, rename the machine (e.g. `agent-squid`).
2. Keep `server.host: "127.0.0.1"` — squid always binds to loopback.
3. Run `bin/start.sh` — it auto-configures `tailscale serve` as the HTTPS proxy.

**Phone/tablet access:**
- Connect the device to the same Tailscale network.
- Type `/remote` in the chat on the host machine to get a QR code.
- The QR code URL is `https://<machine-name>.<tailnet>.ts.net/?token=<value>` — scan it to authenticate in one tap.
- MagicDNS resolves the hostname automatically — no port-forwarding needed.

### `squid.yaml` configuration

```yaml
# All values require a full server restart to take effect.
server:
  host: "127.0.0.1"   # must be loopback; use tailscale serve for remote access
  port: 8000
  token: ""             # set to openssl rand -hex 32 output; empty = no auth
  localfile_roots:
    - "/tmp/squid"      # add other paths as needed (e.g. ~/clawd)
```

## Consequences

- Good: three independent layers — network, token, path — each stops a
  different class of attacker
- Good: token is frictionless after first visit (localStorage + fetch interceptor)
- Good: `/localfile` risk is scoped to explicitly allowed directories
- Good: startup validation prevents accidental public-IP binding
- Bad: token in `localStorage` is visible in browser DevTools — physical device
  access bypasses this layer
- Bad: Tailscale account compromise collapses the network layer
- Bad: no per-device ACL within the tailnet — any enrolled device can reach all
  endpoints once it has the token
- Note: rotating the token requires updating `squid.yaml` and re-authenticating
  all devices via `?token=<new-value>`
