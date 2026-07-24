---
status: accepted
date: 2026-06-01
updated: 2026-07-16
---
# ADR-0016: Security Model — Network Isolation

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

### Host binding restricted to loopback

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

### Tailscale + MagicDNS setup

Tailscale's MagicDNS assigns stable hostnames within your mesh without touching
public DNS.

**Host setup:**
1. In Tailscale admin, rename the machine (e.g. `agent-squid`).
2. Keep `server.host: "127.0.0.1"` — squid always binds to loopback.
3. Start squid (`agentsquid`, or `bin/start.sh` from a source checkout) — it
   auto-configures `tailscale serve` as the HTTPS proxy.

**Phone/tablet access:**
- Connect the device to the same Tailscale network.
- Type `/remote` in the chat on the host machine to get a QR code.
- Scan it to open squid in one tap.
- MagicDNS resolves the hostname automatically — no port-forwarding needed.

### `~/.squid/squid.yaml` configuration

```yaml
server:
  host: "127.0.0.1"   # must be loopback; use tailscale serve for remote access
  port: 8000
  localfile_roots:
    - "/tmp/<user>/squid"   # starting points shown in the file viewer, not an access restriction
```

## Why the `/localfile` path allowlist was removed (2026-07-16)

An earlier version of this ADR had `server.localfile_roots` enforced as a
second security layer: `/localfile` returned 403 for any path outside the
configured list. That allowlist was removed:

- **It didn't restrict what the agent could reach, only the viewer.** The
  agent's own tools (shell, file edit, etc.) already run as the local OS user
  with no allowlist, so anything reachable via `/localfile` was already
  reachable through the agent regardless of the list. The allowlist gave a
  false sense of a security boundary that didn't exist.
- **The real boundary is Layer 1 plus OS file permissions.** `/localfile`
  is unreachable from outside the machine (loopback binding), and once on the
  machine, standard OS file permissions of the logged-in user decide what's
  readable — the same boundary the agent's tools already operate under.

`server.localfile_roots` still exists in `~/.squid/squid.yaml`, but purely as
the list of starting points shown in the file viewer's root view — a
favorites list, not an allowlist. `/localfile` will serve any path the OS
user can read.

## Why bearer token auth was removed (2026-06-17)

An earlier version of this ADR included a bearer token as Layer 2. It was
removed for these reasons:

- **Tailscale already handles remote auth.** Tailscale's device enrollment is
  the gate for who can reach the tailnet. A second secret adds friction without
  a meaningfully different threat model.
- **Local access needs no token.** When binding to `127.0.0.1`, only processes
  on the same machine can connect — token auth adds nothing there.
- **First-run UX was brittle.** New installs required finding the token in
  `~/.squid/squid.yaml` and constructing a `?token=` URL, which caused confusion on
  fresh installs.

## Consequences

- Good: startup validation prevents accidental public-IP binding
- Good: no first-run auth friction on fresh installs
- Good: the file viewer's access matches the agent's actual reach instead of
  pretending to be narrower
- Bad: any Tailscale-enrolled device on your tailnet can reach all squid
  endpoints — there is no per-device ACL within the tailnet
- Bad: Tailscale account compromise collapses the network layer
- Bad: loopback binding is now the only thing standing between `/localfile`
  and the filesystem — if that binding were ever relaxed, `/localfile` would
  become a raw filesystem-read/write oracle for anyone who could reach the
  port
