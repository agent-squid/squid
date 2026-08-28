---
status: accepted
date: 2026-06-01
updated: 2026-08-28
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
exclusively by `tailscale serve`, which proxies traffic from the Tailscale
network to `127.0.0.1:<port>` via two rules — see "Two serve rules" below.

This means only processes on the local machine can reach squid directly.
Tailscale's own device-level authentication handles who can reach the tailnet.

### Two serve rules: https by domain, http by IP (2026-08-28)

`_configure_tailscale_serve` in `agent/server.py` (mirrored by `bin/start.sh`
for source checkouts) configures two `tailscale serve` rules, not one,
because a single rule can't cover both access patterns:

```bash
tailscale serve --bg 127.0.0.1:<port>              # https, default port 443
tailscale serve --bg --http=<port> 127.0.0.1:<port> # http, squid's own port
```

- `https://<machine-name>.<tailnet>.ts.net/` (443, no port in the URL) is the
  short form, but only resolves if MagicDNS is enabled on the tailnet.
- `http://<tailscale-ip>:<port>/` works by IP regardless of MagicDNS. It has
  to be HTTP, not HTTPS: Tailscale's cert is issued for the `*.ts.net`
  domain, not the IP, so `https://<tailscale-ip>:<port>/` fails TLS
  validation. Plain HTTP has no cert to validate, and the connection is
  already encrypted by Tailscale's own WireGuard layer regardless — this
  isn't sending anything in the clear over the internet.

`_tailscale_serve_status` (read-only — no `tailscale serve` mutation) checks
`tailscale status --json`'s `CurrentTailnet.MagicDNSEnabled` field and
`tailscale serve status --json`'s `Web`/`TCP` entries, so squid can tell
which of the two rules is actually live and whether the domain URL will
resolve. Both `agentsquid start`/`agentsquid status` and the foreground
`agentsquid` CLI print both URLs at startup, flagging the domain one if
MagicDNS looks off.

### Tailscale + MagicDNS setup

Tailscale's MagicDNS assigns stable hostnames within your mesh without touching
public DNS.

**Host setup:**
1. In Tailscale admin, rename the machine (e.g. `agent-squid`).
2. Keep `server.host: "127.0.0.1"` — squid always binds to loopback.
3. Start squid (`agentsquid`, or `bin/start.sh` from a source checkout) — it
   auto-configures both `tailscale serve` rules above and prints both URLs.

**Phone/tablet access:**
- Connect the device to the same Tailscale network.
- Type `/remote` in the chat on the host machine to get a QR code.
- Scan it to open squid in one tap.
- MagicDNS resolves the hostname automatically — no port-forwarding needed.
  If MagicDNS is off for this tailnet, use the `http://<tailscale-ip>:<port>/`
  URL printed at startup instead.

### Exposing other local services alongside squid (2026-08-27)

`squid` publishes at the tailnet's default HTTPS port (443) — `_configure_tailscale_serve`
in `agent/server.py`, mirrored by `bin/start.sh`, runs
`tailscale serve --bg 127.0.0.1:<port>` — giving the shortest possible URL,
`https://<machine-name>.<tailnet>.ts.net/`, with no port to remember.

`tailscale serve` supports multiple concurrent rules at different ports on
the same node, so other local services on this machine — e.g. an Ollama or
oMLX server running on its own port — can be exposed the same way, without
touching or competing with squid's rule. Example, Ollama on its default
port `11434`:

```bash
tailscale serve --bg --https=11434 127.0.0.1:11434
```

Reachable at `https://<machine-name>.<tailnet>.ts.net:11434/`, independently
of squid's 443 rule — same one-tap, no-VPN-on-the-client remote access squid
itself gets, from any device already on the tailnet. Substitute whatever
port the other service actually listens on:

```bash
tailscale serve --bg --https=<their-port> 127.0.0.1:<their-port>
```

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
