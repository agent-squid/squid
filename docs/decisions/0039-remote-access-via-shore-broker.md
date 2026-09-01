---
status: proposed
date: 2026-08-11
updated: 2026-08-11
---
# ADR-0039: Remote access via a Cloudflare Workers + Durable Objects broker (agentsquid.ai/@username)

## Context

AgentSquid runs a local web server (shell command execution, dashboard) on
loopback on a user's always-on machine (e.g. a home server). We want a phone
to reach that local server securely, with zero client install on the phone,
and with the whole thing working "out of the box" after `agentsquid login` —
no VPN app, no manual DNS setup, no per-user domain.

Because this grants remote shell-command execution, the security bar for
this design should be treated as equivalent to SSH or remote-desktop access,
not a typical web-app login.

### Options considered

1. **Tailscale (or Headscale) mesh** — every device, including the phone,
   needs the Tailscale client installed. Rejected: violates the zero-install
   requirement for the phone, and gives us full mesh networking we don't
   need for a client-reads-from-one-server use case. Notably, WireGuard-based
   tunnels (Tailscale included) are end-to-end encrypted between devices —
   the coordination/relay servers never see tunnel content, only metadata.
   That property is not automatic in the design below and has to be
   deliberately rebuilt (see Security section).

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

6. **A dedicated `shore.agentsquid.ai` subdomain for the broker** — kept the
   Worker's routes cleanly separate from the GitHub-Pages-hosted marketing
   site on the apex domain, at the cost of a longer URL for users to type
   and remember. Rejected in favor of option 7: the same separation is
   achievable on the apex domain itself via a path-scoped Worker route, at
   no cost to security or architecture, while giving users a shorter URL.

7. **Cloudflare Workers + Durable Objects broker on `agentsquid.ai`
   directly** (chosen) — see Decision.

## Decision

Build the remote-access broker as a single Cloudflare Worker, backed by one
Durable Object per registered username, reachable at
`agentsquid.ai/@<username>`.

Implementation sequencing, acceptance gates, and verification are maintained
in the [ADR-0039 Shore implementation plan](../plans/adr-0039-shore-remote-access.md).
This ADR remains the authority for architecture and security decisions; the
plan must not weaken or silently replace them.

The broker carries the versioned real-time application protocol defined by
ADR-0040. Implement and validate that protocol locally before making the broker
its remote transport; Shore must not define a second set of chat, lifecycle,
reconnect, or replay semantics.

### Coexisting with the existing GitHub-Pages-hosted site

`agentsquid.ai` already serves the project's marketing/docs site via GitHub
Pages. Rather than route the broker through a separate subdomain, scope the
Worker to a **path pattern**, e.g. a Cloudflare Workers Route of
`agentsquid.ai/@*`. Only requests matching `/@username` are intercepted by
the Worker; everything else continues to be served by GitHub Pages
untouched. This keeps the two systems decoupled operationally (no shared
deploy, no risk of one's routing rules swallowing the other's paths) while
giving users the shorter, single-domain URL.

### Architecture

- **No new DNS record for the broker itself** — it rides on the existing
  `agentsquid.ai` zone via a path-scoped Worker route, not a new hostname.
- **One Durable Object per user**, addressed via `idFromName(account_id)`.
  A separate singleton
  identity-index Durable Object maps normalized email addresses to immutable
  account IDs and current usernames, and serializes uniqueness checks,
  signup, recovery, and username changes. Per-user objects remain the source
  of truth for account and device state.
- **No `cloudflared` binary and no Cloudflare Tunnel resource.** Each
  AgentSquid instance opens an outbound WebSocket directly to the Worker
  (`wss://agentsquid.ai/@<username>/register`, or an equivalent registration
  path) and the owning Durable Object holds that connection, using WebSocket
  hibernation so an idle connection costs near-zero compute.
- **Request flow**: a request to `agentsquid.ai/@<username>` hits the
  Worker, which resolves the immutable account ID through the identity index,
  addresses that user's Durable Object, forwards the
  request down the held WebSocket to the user's local AgentSquid instance,
  and relays the response back.
- Runs entirely on Cloudflare's free tier at expected early-stage volume
  (Workers Free: ~100K requests/day; Durable Objects on Workers Free:
  ~1M requests/month, ~400K GB-seconds/month). No paid Cloudflare plan
  required to launch.
- **Status/health updates are pushed over the WebSocket, not polled via
  HTTP.** The Workers Free request quota (~100K/day) is consumed by every
  request through the Worker, not just login/registration — a naive client
  polling for status every few seconds would burn through a large share of
  that daily budget from a single active session alone. The phone holds its
  own WebSocket connection to the Durable Object (the same pattern already
  used for the host machine's connection), and AgentSquid pushes
  status/output events down it only when state actually changes, rather
  than the phone re-requesting on an interval. WebSocket messages are
  billed at a 20:1 ratio against the separate Durable Objects quota, not
  1:1 against the Workers request limit. This also solves a second,
  independent problem: without push, a change made from one device (e.g.
  starting a job on desktop) wouldn't appear on another (e.g. the phone)
  without a manual refresh. Push-based sync makes multi-device state
  consistency (desktop and phone reflecting the same running state without
  polling) a byproduct of the same mechanism, rather than a separate
  feature to build — closer to how a chat app keeps devices in sync than to
  a traditional request/response API. Request and message volume in this
  design scales with connections, commands, and emitted state changes, not
  with polling frequency or total registered user count — idle accounts
  contribute negligible volume.

### User registration

- A user creates an AgentSquid account at `agentsquid.ai` via email
  magic-link (OAuth as a later addition, not required for v1).
- At signup, the user claims a `username` (validated: alphanumeric,
  length-bounded, checked against a reserved-word blocklist). The username
  is the public path segment (`/@username`); an immutable generated account
  ID is the Durable Object key — one account, one username, one Durable
  Object.
- Account state (email, username, password/session metadata, created_at) is
  persisted in the user's own Durable Object storage (SQLite-backed, GA on
  the Workers Free plan). The identity index stores only the minimum global
  mapping needed to resolve email login and enforce uniqueness; no separate
  database service is needed. User objects are keyed by immutable account ID,
  so a rename changes the route and index without moving account data.

### Authentication

Three distinct authentication events, handled by AgentSquid/the Worker,
not by Cloudflare Access:

1. **Device registration (host machine → broker)**: on `agentsquid login`,
   the CLI generates a local keypair; the private key never leaves the
   machine. The public key is registered to the account at login time, and
   the local AgentSquid process signs its WebSocket registration to the
   Durable Object rather than presenting a static bearer token — closer to
   SSH key auth than password auth. This prevents another party from
   squatting an active registration for someone else's username even if a
   single bearer secret were to leak.

2. **End-user session (phone/browser → broker)**: visiting
   `agentsquid.ai/@<username>` requires a login (same account system as
   registration). Sessions are short-lived with refresh, gated behind a
   second factor (TOTP or passkey) given what a session authorizes, and
   listed/revocable from one place — the equivalent of checking
   `~/.ssh/authorized_keys` or running `ssh-add -D`. The Worker checks this
   session before proxying any request into the Durable Object's WebSocket.

3. **Phone-to-host pairing**: browser login authenticates the account but
   does not authorize commands by itself. A new phone generates a non-
   extractable signing/encryption keypair and must be approved on the host
   through a locally displayed one-time pairing code that commits to both
   device public-key fingerprints. The host persists the approved phone key;
   the phone pins the host key. Key changes require a new local pairing or an
   explicit recovery flow that revokes prior device keys. The broker cannot
   add or replace either key.

## Security

Because a compromised or misused session here is equivalent to a
compromised SSH key or remote-desktop credential, the following are treated
as v1 requirements, not later hardening:

- **End-to-end payload encryption via a device keypair, above and beyond
  TLS.** By default, the Worker is an application-layer proxy: TLS
  terminates at Cloudflare's edge, and command/response payloads pass
  through the Worker as plaintext before being relayed. This differs from
  SSH and from WireGuard-based tunnels (Tailscale included), where the
  relay/coordination layer only ever handles encrypted bytes and is
  cryptographically blind to content. To restore that property here:
  - At `agentsquid login`, the local AgentSquid instance generates a
    public/private keypair on the host machine. The private key never
    leaves that machine — not stored server-side, not transmitted anywhere.
    The public key is uploaded to the account record.
  - After pairing, the phone uses its pinned host key to establish an
    authenticated encrypted channel. It signs every command envelope with
    its approved device key. Each envelope includes the account and device
    IDs, protocol version, monotonically increasing sequence number, unique
    request ID, expiry, and ciphertext; all fields are covered by the
    signature.
  - The Worker and Durable Object route the resulting ciphertext by
    username only; they hold no key capable of decrypting or forging it. The
    AgentSquid instance rejects unapproved device keys, expired envelopes,
    duplicate request IDs, and non-increasing sequence numbers before
    execution. It encrypts and signs responses to the paired phone key.
  - Trust is established by the local pairing ceremony, not by a host key
    fetched from the broker on first use. This prevents a malicious broker
    from substituting keys or injecting commands encrypted with the public
    host key.
  - This makes passive broker access to command and response payloads
    cryptographically impossible and prevents the broker protocol from
    forging commands. It does not make a browser client delivered by that
    same operator immune to a malicious software update: with the zero-
    install requirement, the operator remains part of the active client
    supply-chain trust boundary. Production must use immutable, versioned
    client assets with a restrictive CSP and public reproducible-build
    hashes, and must state this residual risk rather than claim the operator
    can never influence an active session. Removing that trust entirely
    would require a separately installed or independently distributed client.
- **No admin master key for session minting.** Any admin tooling must not
  be able to generate a valid session for an arbitrary username on its own.
  Sessions are only issuable through the user's own login flow or their
  registered device key — a capability to mint sessions on demand is a
  backdoor by construction, independent of payload encryption.
- **Rate limiting on the `/@*` route.** Usernames are public and guessable
  by design; without per-IP rate limiting and lockout on failed logins,
  `/@username` is brute-forceable. Use Cloudflare's WAF/rate-limiting rules
  (available on the free plan) scoped to this route.
- **Scoped execution privilege.** SSH inherits the OS's user/permission
  model for free; a custom command-execution API does not unless it is
  deliberately built in. AgentSquid should run as a dedicated, low-privilege
  OS user rather than whichever account performed the install, so a leaked
  session's blast radius is bounded by that user's permissions, not by
  whatever account happened to run the installer.
- **Correlated, tamper-evident audit logging.** Before forwarding an envelope,
  the broker appends its request ID, authenticated account/device/session
  IDs, source IP, and receipt timestamp to an audit object. After validating
  and executing it, the host appends a signed event containing the same
  request ID, command hash, outcome, and host timestamp. The broker cannot
  read the command, while the correlated records still attribute its hash
  to the observed source. Records are hash-chained and exported to append-
  only storage under separate credentials that neither a remote session nor
  the host process can delete. Raw command text is excluded by default to
  avoid creating a second store of secrets.
- **If real admin access to a user's account is ever needed** (e.g.
  support), it should require deliberate, logged, ideally user-notified
  action, not silent default reach — an append-only audit log the admin
  account itself cannot quietly edit, and a notification to the affected
  user.
- **Remote execution is capability-scoped by default.** A paired phone may
  use the existing dashboard and a versioned allowlist of non-destructive
  AgentSquid operations, but cannot invoke arbitrary shell commands. Full
  shell access requires a separate capability granted locally on the host
  to a specific device, with an explicit warning, expiry (at most 24 hours),
  audit event, and immediate revocation. It is never granted by account
  login, pairing, or an administrator.

## Consequences

### Positive

- No VPN client on any device, including the phone.
- No per-user DNS record — removes the ~200-user free-tier ceiling that a
  Tunnel-per-user design would hit, and no separate subdomain to keep track
  of or type.
- No Cloudflare Access dependency — removes the 50-seat free ceiling and
  $7/user/month cost past it.
- No self-operated relay server — Cloudflare runs the Worker and Durable
  Objects; an AgentSquid-side outage doesn't take down every user's access
  the way a single self-hosted broker would.
- Fully buildable and operable on Cloudflare's free tier at current scale.
- Path-scoped routing keeps the broker decoupled from the existing GitHub
  Pages site with no shared deploy risk.

### Negative / risks

- The Worker's auth and routing logic is security-critical code we own and
  must maintain correctly — there is no vendor-provided access-control layer
  in front of it (a deliberate tradeoff against Cloudflare Access's cost
  model).
- Without the end-to-end encryption layer described above, the broker
  operator (us) has a structural ability to observe traffic that SSH and
  WireGuard-based alternatives do not grant their operators — this must be
  closed before this is treated as production-ready for real remote shell
  use, not deferred as a nice-to-have.
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
- Cloudflare Workers Routes (path-scoped routing on an existing zone)
