---
status: accepted
date: 2026-06-01
updated: 2026-06-17
---
# ADR-0016: Security Model — Network Isolation + Path Allowlist

## Context and Problem Statement

Squid exposes HTTP endpoints that have significant local impact:

- `/chat` — streams agent responses (spawns CLI subprocesses)
- `/cmd` — kills processes, clears sessions, restarts the server
- `/localfile` — serves local files by absolute path

Left open on all interfaces with no access control, these endpoints would let
anyone on the same network read arbitrary files, restart the server, or generate
API costs. The question is what level of access control is appropriate for a
personal, single-user tool.

## Decision Outcome

Two complementary layers, each enforced at startup or in middleware:

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

This means only processes on the local machine can reach squid directly.
Tailscale's own device-level authentication handles who can reach the tailnet.

### Layer 2 — `/localfile` path allowlist

`server.localfile_roots` in `squid.yaml` is an explicit list of directories
that `/localfile` is permitted to serve from. Requests for paths outside the
list return 403. An empty list disables the endpoint entirely.

This prevents `/localfile` (the highest-risk endpoint — arbitrary file reads)
from being used to read SSH keys, credentials, or other sensitive files.

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
- Scan it to open squid in one tap.
- MagicDNS resolves the hostname automatically — no port-forwarding needed.

### `squid.yaml` configuration

```yaml
server:
  host: "127.0.0.1"   # must be loopback; use tailscale serve for remote access
  port: 8000
  localfile_roots:
    - "/tmp/squid"      # add other paths as needed (e.g. ~/clawd)
```

## Why bearer token auth was removed (2026-06-17)

An earlier version of this ADR included a bearer token as Layer 2. It was
removed for these reasons:

- **Tailscale already handles remote auth.** Tailscale's device enrollment is
  the gate for who can reach the tailnet. A second secret adds friction without
  a meaningfully different threat model.
- **Local access needs no token.** When binding to `127.0.0.1`, only processes
  on the same machine can connect — token auth adds nothing there.
- **First-run UX was brittle.** New installs required finding the token in
  `squid.yaml` and constructing a `?token=` URL, which caused confusion on
  fresh installs.

## Consequences

- Good: two independent layers — network binding and path allowlist — each
  stops a different class of attacker
- Good: startup validation prevents accidental public-IP binding
- Good: `/localfile` risk is scoped to explicitly allowed directories
- Good: no first-run auth friction on fresh installs
- Bad: any Tailscale-enrolled device on your tailnet can reach all squid
  endpoints — there is no per-device ACL within the tailnet
- Bad: Tailscale account compromise collapses the network layer
