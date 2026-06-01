---
status: accepted
date: 2026-06-01
---
# ADR-0016: Security Model — Network-Level Isolation via Localhost or Tailscale

## Context and Problem Statement

Squid exposes HTTP endpoints with no application-level authentication:

- `/chat` — streams agent responses (spawns CLI subprocesses)
- `/cmd` — kills processes, clears sessions, restarts the server
- `/localfile` — serves arbitrary local files by path

Leaving these open on all interfaces (`0.0.0.0`) would expose them to every
network the host is on — LAN, Wi-Fi, VPN — with no protection. The question is
what level of access control is appropriate for a personal, single-user tool.

## Considered Options

1. **Application-level auth** — bearer token or session cookie on every request
2. **Bind to `127.0.0.1` only** — local access only; remote access requires an
   explicit tunnel (SSH port-forward, etc.)
3. **Bind to Tailscale IP** — accessible to all devices on the personal Tailscale
   mesh; blocked from everything else at the network level
4. **Bind to `0.0.0.0`** — all interfaces, no network isolation

## Decision Outcome

**Options 2 and 3 are the only permitted bindings.** `0.0.0.0` is explicitly
blocked at startup with an error message. Application-level auth is deferred —
the attack surface is a personal tool on a controlled network, so network
isolation is sufficient for now.

`server.host` in `squid.yaml` controls the binding:

| Value | Access | When to use |
|---|---|---|
| `127.0.0.1` | This machine only | Default; no remote access needed |
| Tailscale IP | All devices on your Tailscale mesh | Mobile/tablet access via Tailscale |
| `0.0.0.0` | **Blocked — server refuses to start** | Never |

### Tailscale + MagicDNS setup

Tailscale's MagicDNS lets you assign a stable hostname to a machine within
your mesh without touching public DNS.

**Laptop setup:**
1. In Tailscale admin, rename the machine to a friendly name (e.g. `agent-squid`).
2. Set `server.host` in `squid.yaml` to the machine's Tailscale IP
   (find it with `tailscale ip -4`).
3. Start squid — it binds to that IP.

**Phone/tablet access:**
- Ensure the device is connected to the same Tailscale network.
- Open `http://agent-squid:8000` — MagicDNS resolves the hostname to the
  Tailscale IP automatically. No DNS configuration or port-forwarding required.

### Why not application-level auth?

- Squid is a single-user personal tool; there is no multi-tenant surface.
- Tailscale already enforces identity (device must be enrolled in your tailnet).
- Adding bearer tokens adds friction to mobile use with no meaningful gain given
  the network boundary already in place.
- If the threat model changes (shared server, public IP), a FastAPI middleware
  layer checking a token from `squid.yaml` is the right next step.

## Consequences

- Good: zero friction — no tokens to manage, no login flow
- Good: `/localfile` risk is contained to the Tailscale mesh (trusted devices only)
- Good: MagicDNS hostname survives IP changes; `squid.yaml` only needs updating
  if the Tailscale IP itself changes (rare)
- Bad: any enrolled Tailscale device can reach all endpoints — no per-device ACL
- Bad: if Tailscale is misconfigured or a device is compromised, there is no
  second layer of defense
- Note: to add auth later, add a middleware in `server.py` that checks
  `Authorization: Bearer <token>` against a token stored in `squid.yaml`
