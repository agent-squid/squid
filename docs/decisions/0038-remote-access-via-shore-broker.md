---
status: proposed
date: 2026-08-11
updated: 2026-08-11
---
# ADR-0038: Remote access via a Cloudflare Workers + Durable Objects broker (shore.agentsquid.ai)

## Context

AgentSquid runs a local web server (shell command execution, dashboard) on
loopback on a user's always-on machine (e.g. a home server). We want a phone
to reach that local server securely, with zero client install on the phone,
and with the whole thing working "out of the box" after `agentsquid login` —
no VPN app, no manual DNS setup, no per-user domain.

### Options considered

1. **Tailscale (or Headscale) mesh** — every device, including the phone,
   needs the Tailscale client installed. Rejected: violates the zero-install
   requirement for the phone, and gives us full mesh networking we don't
   need for a client-reads-from-one-server use case.

2. **Tailscale Funnel** — exposes the local server via a public HTTPS URL
   without phone-side install, reusing the Tailscale node already useful for
   other things. Rejected for this use case: still requires the *host*
   machine to run Tailscale, ties remote access to a Tailscale account, and
   provides no built-in application-level auth (Funnel just makes the port
   public — we would still have to build auth ourselves, at which point the
   Tailscale dependency buys us little over the Cloudflare option below).

3. **Cloudflare Tunnel with one subdomain per user**
   (`username.agentsquid.ai`) — `cloudflared` runs on the host machine,
   phone hits a plain HTTPS URL, no phone-side install. Rejected as the
   long-term design: each user needs a dedicated Cloudflare Tunnel and DNS
   record, and Cloudflare's free plan caps DNS records per zone at 200 for
   zones created on or after September 1, 2024 (1,000 for older zones; 3,500
   on Pro/Business/Enterprise). This caps free-tier growth at roughly 200
   users and requires a paid Cloudflare plan beyond that.

4. **Cloudflare Access for authentication** — turnkey login screen (email
   OTP, OAuth) gating a Tunnel hostname. Rejected as the primary auth
   mechanism: free plan covers only 50 users (a seat is consumed per
   authentication event and held until removed), $7/user/month after that.
   Using Access for every user ties our per-user cost directly to Cloudflare
   pricing rather than to our own infrastructure.

5. **Self-hosted relay (frp / ngrok-style broker)** — one server we run,
   users' AgentSquid instances dial out to it, it multiplexes by hostname or
   path. Solves the DNS-record-limit problem but makes us the operator of
   always-on relay infrastructure: an outage takes down remote access for
   every user, and we own patching, scaling, and abuse prevention for a
   public network service.

6. **Cloudflare Workers + Durable Objects broker** (chosen) — see Decision.

## Decision

Build the remote-access broker as a single Cloudflare Worker, backed by one
Durable Object per registered username, reachable at
`shore.agentsquid.ai/@<username>`.

### Architecture

- **One DNS record total**: `shore.agentsquid.ai` → the Worker. No
  per-user DNS records, so the 200/3,500-record zone cap never becomes a
  constraint at any realistic scale.
- **One Durable Object per user**, addressed via `idFromName(username)`.
  Cloudflare routes to the correct instance by name — no lookup table to
  build or maintain ourselves.
- **No `cloudflared` binary and no Cloudflare Tunnel resource.** Each
  AgentSquid instance opens an outbound WebSocket directly to the Worker
  (`wss://shore.agentsquid.ai/register?user=<username>`) and the owning
  Durable Object holds that connection, using WebSocket hibernation so an
  idle connection costs near-zero compute.
- **Request flow**: a request to `shore.agentsquid.ai/@<username>` hits the
  Worker, which resolves the Durable Object for that username, forwards the
  request down the held WebSocket to the user's local AgentSquid instance,
  and relays the response back.
- Runs entirely on Cloudflare's free tier at expected early-stage volume
  (Workers Free: ~100K requests/day; Durable Objects on Workers Free:
  ~1M requests/month, ~400K GB-seconds/month). No paid Cloudflare plan
  required to launch.

### User registration

- A user creates an AgentSquid account at `agentsquid.ai` via email
  magic-link (OAuth as a later addition, not required for v1).
- At signup, the user claims a `username` (validated: alphanumeric,
  length-bounded, checked against a reserved-word blocklist). The username
  is the Durable Object key and the path segment (`/@username`) — one
  account, one username, one Durable Object.
- Account state (email, username, password/session metadata, created_at) is
  persisted in the user's own Durable Object storage (SQLite-backed, GA on
  the Workers Free plan) — no separate database service needed.

### Authentication

Two distinct authentication events, both handled by AgentSquid/the Worker,
not by Cloudflare Access:

1. **Device registration (host machine → broker)**: on `agentsquid login`,
   the CLI obtains a long-lived device token tied to the account. The local
   AgentSquid process presents this token when opening the outbound
   WebSocket to its Durable Object, so only the legitimate owner of a
   username can register as its backing connection (prevents another party
   from squatting an active registration for someone else's username).

2. **End-user session (phone/browser → broker)**: visiting
   `shore.agentsquid.ai/@<username>` requires a login (same account system
   as registration — email magic-link session, later OAuth). The Worker
   checks this session before proxying any request into the Durable
   Object's WebSocket. This keeps auth logic and cost inside code we own,
   rather than inside Cloudflare Access's per-seat billing.

## Consequences

### Positive

- No VPN client on any device, including the phone.
- No per-user DNS record — removes the ~200-user free-tier ceiling that a
  Tunnel-per-user design would hit.
- No Cloudflare Access dependency — removes the 50-seat free ceiling and
  $7/user/month cost past it.
- No self-operated relay server — Cloudflare runs the Worker and Durable
  Objects; an AgentSquid-side outage doesn't take down every user's access
  the way a single self-hosted broker would.
- Fully buildable and operable on Cloudflare's free tier at current scale.

### Negative / risks

- The Worker's auth and routing logic is security-critical code we own and
  must maintain correctly — there is no vendor-provided access-control layer
  in front of it (a deliberate tradeoff against Cloudflare Access's cost
  model).
- AgentSquid must implement WebSocket reconnect/backoff logic against
  Durable Object hibernation and Worker restarts; this is new client-side
  complexity that a plain Cloudflare Tunnel setup would not require.
- At very large scale, Workers/Durable Objects free-tier limits (~1M
  requests/month, ~400K GB-seconds/month) will eventually require the
  Workers Paid plan ($5/month minimum) — a much later and cheaper ceiling
  than the per-seat or per-DNS-record alternatives above.
- Ties the core remote-access feature to Cloudflare's platform. Mitigation:
  keep the AgentSquid ↔ Worker protocol a plain WebSocket/JSON contract, so
  the broker could be reimplemented against another provider (or
  self-hosted) without changing the client-side design.

## References

- Cloudflare DNS record limits per zone (Free: 200 for zones created on/after
  2024-09-01, 1,000 for older zones; Pro/Business/Enterprise: 3,500)
- Cloudflare Access pricing (Free: 50 users; Pay-as-you-go: $7/user/month)
- Cloudflare Durable Objects on Workers Free plan, WebSocket hibernation,
  SQLite storage backend (GA)
