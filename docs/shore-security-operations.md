# Shore security, privacy, and operations contract

This document assigns every Milestone 0 control to an operational role. Before
production, named people and an escalation schedule must fill these roles; a
vacant role blocks deployment.

## Ownership and audit

| Area | Accountable role | Contract |
| --- | --- | --- |
| Shore service, Cloudflare credentials, quota and kill switch | Shore Service Owner | Separate production account; hardware-backed admin MFA; two-person deploy/recovery; quarterly access review. |
| Cryptography, incidents, vulnerability intake | Security Owner / on-call incident commander | Owns critical mitigations, key-compromise response, notification decision, and annual independent review. |
| Account authentication, sessions, second factor, recovery | Identity Owner (interim: Shore Service Owner) | Owns identity state machines, account-takeover controls, recovery review, and identity incident response. |
| Wire protocol, pairing, envelope, replay and capability registry | Protocol Owner (interim: Security Owner) | Owns protocol changes, vectors, interoperability, crypto review, and fail-closed authorization semantics. |
| Local host keys, runtime isolation, revocation enforcement | Host Runtime Owner (interim: Release Owner) | Owns host key custody, low-privilege execution, local enforcement, and host-compromise response. |
| Retention, export, deletion, user requests | Privacy Owner | Approves fields and retention; quarterly deletion/redaction sampling. |
| Host release signing and reproducible client build | Release Owner | Two-person release, immutable asset hashes, rollback and emergency disable. |
| Audit archive | Security Audit Custodian, separate from Shore runtime admins | Owns archive account and restore/read credentials; runtime has append-only write credentials only. |

During the initial deployment one named person may fill multiple roles, but all
duties and escalation paths remain explicit. The Security Audit Custodian must
remain separate from Shore runtime administration, and every stated two-person
control still requires two distinct people.

## Accepted interim exceptions

**2026-09-05 — Solo operator; Shore Service Owner's two-person deploy control
not met.** Shore has one active contributor, so the two-person deploy/recovery
control in the Ownership table is not currently satisfied: the `shore-prod`
GitHub environment (`shore/.github/workflows/deploy-production.yml`) has no
required-reviewer protection rule, and production deploys/recoveries are
single-operator actions. This is recorded as a temporary, explicit exception
given the current low user count, not a waiver of the control itself. Close it
by adding a second named reviewer/approver and enabling required reviewers on
the `shore-prod` GitHub environment as soon as a new contributor joins; revisit
this entry at that time.

Broker and host events use the same request/transition ID and hash commitment.
The broker chain contains prior hash, event ID, account/host/device/session IDs,
coarse source metadata, restricted raw source IP, receipt time, ciphertext hash,
and outcome. The host chain adds signed request ID, plaintext command hash,
authorization decision, result class, host time, and prior host-event hash.
Neither stores command text, response text, secrets, cookies, authorization
headers, internal addresses, precise location, or full headers.

Production export targets the private, SSE-B2-encrypted Backblaze B2 bucket
`shore-audit-prod` (bucket ID `0c55d2aee29c52e1ae0f051c`) in an account
separate from Cloudflare. Object Lock must be enabled at bucket creation. Before
external users are admitted, default retention must be set to Compliance mode
for 400 days and lifecycle deletion configured for versions after their lock
expires. Shore uses a bucket-scoped write-only application key with no read,
delete, bucket-management, legal-hold, or governance-bypass capability; key
material is never committed. Object names are unique and contain no user data.
The Security Audit Custodian has separate read/export access; compliance-mode
retention cannot be shortened or bypassed even by that role.
Daily signed manifests anchor both chain heads, counts, and gaps. Export lag
over five minutes pages Security; local queues are bounded but security actions
fail closed if their audit record cannot be durably queued. Quarterly restore,
fork, deletion, insertion, and correlation drills are required.

Development and pre-production verification use the private, SSE-B2-encrypted
bucket `shore-audit-test` (bucket ID `fc55027ee2ac52e1ae0f051c`) with Object
Lock enabled and default Governance retention of one day. Its bucket-scoped key
and data are isolated from production, and Shore is not granted governance
bypass. Tests must cover retention, expiry, lifecycle deletion, manifest
verification, restore, and rejection of overwrite/deletion attempts. The two
buckets share the B2 account's free storage allowance; usage alerts are set
before the allowance is exhausted. Test storage is never an audit authority.

Raw IP is restricted to the audit archive and retained 30 days, after which a
daily cryptographic-erasure job destroys its field-encryption key while the
account-scoped opaque network fingerprint and coarse country/region/ASN remain
for 400 days. User security notifications contain time, coarse region/ASN,
known/new status, opaque fingerprint, and client version only. Users can export
their visible security history for the last 400 days; export never includes raw
IP, other users, internal identifiers unnecessary to them, or secret material.

## Threat model

| Threat | Required mitigation | Owner | Residual / gate |
| --- | --- | --- | --- |
| Malicious broker reads or forges commands | E2E envelope, pinned keys, local pairing, host-side replay/capability validation | Security Owner | Metadata remains visible; critical if plaintext or broker-minted trust is possible. |
| Stolen browser account session | Mandatory second factor, short rotation, session revocation; account login grants no pairing | Identity Owner | Attacker can view account metadata, never host state/commands without device keys. |
| Stolen paired browser/device | Non-exportable keys, device-specific grant/revocation, short shell expiry, visible history | Security Owner | Read capability lasts until revocation/session expiry; users must be notified. |
| Replay/reordering/injection | Signed identity, epoch, durable monotonic sequence, request-ID set, expiry and AEAD | Protocol Owner | Storage failure fails closed. |
| Broker key substitution / TOFU | QR binding commits both devices' signing/agreement fingerprints | Protocol Owner | Local display compromise remains host compromise. |
| Compromised host | Dedicated low-privilege OS user, local audit signing, device revocation, no archive-delete credential | Host Runtime Owner | Host can execute/lie about its own outcome; broker record preserves receipt. |
| Malicious web-client update | Immutable versioned assets, restrictive CSP, reproducible hashes, two-person signed release, staged kill switch | Release Owner | Zero-install web delivery cannot eliminate operator supply-chain trust; disclosed risk blocks claims otherwise. |
| Durable Object restart/hibernation | Authoritative durable state, attachments treated as hints, generation checks, idempotent transitions | Shore Service Owner | Restart tests gate Milestone 1/6. |
| Account recovery takeover | Offline verifier, fresh MFA, seven-day cooling-off, repeated alerts/cancel, new trust root | Identity Owner | Administration may recover; old cryptographic trust never does. |
| Host private-key theft / healthy displacement | Immediate high-severity audit/alert, privacy-safe batching, five-minute step-up atomic revoke | Security Owner | Legitimate reconnect ambiguity prevents automatic revoke. |
| Pairing brute force/race | 128-bit random secret, five-minute/single-use/five-attempt bounds, layered rate limits, atomic consume | Protocol Owner | No memorable low-entropy fallback. |
| Quota exhaustion / abuse | Per-route/account/device/IP application limits, reserved security capacity, degradation thresholds, paid spend ceiling | Shore Service Owner | Free WAF alone is insufficient; explicit unavailable response and opt-in Tailscale fallback. |
| Audit tamper or credential compromise | Separate compliance-locked archive, chained signed manifests, least-privilege append credential, drills | Security Audit Custodian | Archive provider/account compromise is accepted third-party risk. |

There is no unowned critical mitigation: interim ownership is stated wherever a
future specialist role is named. Any critical/high security-review finding,
missing owner, failed audit export, reproducibility failure, or inability to
revoke activates the server-side Shore kill switch. Direct loopback/Tailscale
access remains independent.

Incident severity is: SEV-1 for suspected key/signing compromise, plaintext at
broker, forged authorization, mass account access, or audit loss; SEV-2 for
targeted session compromise, sustained pairing abuse, or quota exhaustion;
SEV-3 for isolated availability defects. Security commands SEV-1, preserves
evidence, disables the affected capability or Shore globally, begins user/legal
notification assessment, and publishes a postmortem. Service owns availability
recovery; Privacy owns disclosure content and statutory timing.

## Cloudflare revalidation — 2026-09-01

Only official Cloudflare documentation was used. Revalidate at Milestone 1
implementation freeze and before every production readiness review.

- Workers Free is 100,000 requests/day and 10 ms CPU/invocation; Paid includes
  10 million requests/month, then $0.30/million, with a $5 monthly minimum.
  A WebSocket upgrade is one Worker request; messages through it are not Worker
  requests. <https://developers.cloudflare.com/workers/platform/pricing/>
- SQLite-backed Durable Objects are available on Free. Free includes 100,000
  requests and 13,000 GB-s per day; Paid includes 1 million requests and
  400,000 GB-s/month, then $0.15/million requests and $12.50/million GB-s.
  Incoming WebSocket messages are billed at a 20:1 ratio. SQLite row/storage
  allowances also apply. <https://developers.cloudflare.com/workers/platform/pricing/>
- The Hibernation WebSocket API is required. It preserves accepted inbound
  sockets while memory is reset; the constructor reruns, attachments are capped
  at 16,384 bytes, and protocol ping/pong does not wake the object. Outgoing
  WebSockets cannot hibernate, so the Worker must proxy both host and browser
  upgrades into an account object that acts as the WebSocket server.
  <https://developers.cloudflare.com/durable-objects/best-practices/websockets/>
- A hibernating object can later restart/move while connections remain; no
  in-memory state is authoritative. The documented hard ceiling is 32,768
  hibernating sockets per object, with practical CPU/memory limits lower.
  <https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/>
  <https://developers.cloudflare.com/durable-objects/api/state/>
- Workers Free has 128 MB memory, 50 subrequests/request, 100 MB request-body
  limit on a Free zone, 16 KB URLs, and 1,000 routes/zone. Shore's smaller
  protocol limits control first. <https://developers.cloudflare.com/workers/platform/limits/>
- Free WAF supplies only one rate-limiting rule, path/verified-bot matching,
  IP-only counting, and a 10-second period/mitigation. Therefore WAF is only a
  coarse outer guard; all account/device/session/pairing limits and reserved
  security capacity must be implemented in Worker/account-object logic.
  <https://developers.cloudflare.com/waf/rate-limiting-rules/>

These are operational assumptions, not wire guarantees. Pricing or plan drift
does not weaken authentication, pairing, cryptography, capability, revocation,
or audit; Shore degrades or stops explicitly.
