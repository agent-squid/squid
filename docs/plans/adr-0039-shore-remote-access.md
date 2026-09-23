# Plan: ADR-0039 Shore remote access

**Status:** In progress. Milestones 0–5 are complete; Milestone 6 is in
progress. External production users remain blocked pending Milestone 6. See
the [implementation log](./adr-0039-implementation-log.md).

This is the implementation plan for
[ADR-0039](../decisions/0039-remote-access-via-shore-relay.md). The ADR owns
the architectural and security decisions; this document owns sequencing,
acceptance gates, and verification. Implement the milestones in order. Do not
enable remote command execution until every production gate is satisfied.

ADR-0040's local WebSocket migration is complete enough to begin this work.
Shore must relay that versioned application protocol rather than introduce a
second command, event, replay, or snapshot model.

## Non-negotiable invariants

- The relay never receives keys that can decrypt or forge command payloads.
- Account login alone never authorizes a device to issue host commands.
- Host and browser device keys are generated and retained locally.
- Host identities are immutable and append-only; a different key cannot replace
  or displace an existing host identity through account login.
- Each account has exactly one current host; a second host is rejected unless
  the current identity is revoked through the defined replacement flow.
- Pairing establishes trust through a host-displayed code that commits to both
  device-key fingerprints; relay-provided trust-on-first-use is insufficient.
- Account recovery cannot recover cryptographic trust or inherit old pairings
  and capabilities; replacement hosts are visibly new trust roots.
- Every command is signed, encrypted, capability-checked, replay-protected,
  expiry-checked, and correlated with tamper-evident audit records.
- Remote access is disabled by default. Arbitrary shell capability is separate,
  device-specific, locally granted, revocable, and expires within 24 hours.
- Every published AgentSquid binary version selects one exact, immutable Shore
  web-client release; the bootstrap never substitutes another version.
- The existing direct local/Tailscale and SSE compatibility paths remain
  operational throughout the rollout.

## Milestone 0 — Resolve specifications and operational ownership

**Status:** Complete (2026-09-01). The accepted contract is
[`shore-protocol-v1.md`](../shore-protocol-v1.md), its
[`test vectors`](../shore-protocol-v1-vectors.json),
[`shore-state-machines.md`](../shore-state-machines.md), and
[`shore-security-operations.md`](../shore-security-operations.md). ADR-0039 is
accepted. Later milestones must not weaken these gates.

**Objective:** turn the proposed ADR into an accepted, implementable contract
without embedding unsettled security choices in code.

**Actions:**

1. Define the encrypted outer envelope, canonical serialization, algorithms,
   key formats, sequence and request-ID rules, expiry/skew policy, and error
   behavior in a versioned Shore protocol document. The ciphertext payload is
   an ADR-0040 v1 frame.
2. Define single-host registration/routing, browser session, pairing, key
   epochs, host replacement, recovery, revocation, username rename, and account
   deletion state machines.
3. Define the initial capability allowlist and the exact ADR-0040 commands and
   scopes available to each capability. Default-deny unknown and future types.
4. Define audit retention/export, append-only destination, credential ownership,
   privacy fields, and incident-response ownership.
5. Threat-model a malicious relay, stolen browser session, stolen device,
   replay, key substitution, compromised host, malicious client update, Durable
   Object restart, and account recovery.
6. Revalidate current Cloudflare limits, pricing, routing, Durable Object
   hibernation behavior, WebSocket constraints, and rate-limiting availability
   before relying on them operationally.

**Audit archive deployment:** use private Backblaze B2 buckets with default
SSE-B2 encryption and Object Lock enabled. Pre-production uses
`shore-audit-dev` (`3cc542ee223c72f1ae0f051c`) with one-day Compliance
retention. Production uses `shore-audit-prod`
(`0c55d2aee29c52e1ae0f051c`); enable default 400-day Compliance retention and
post-retention lifecycle deletion only after exporter verification, but before
admitting external users. B2 credentials exist only in Shore-managed
infrastructure, never on AgentSquid hosts. Use separate bucket-scoped keys for
production and test, and never commit key IDs or secrets. The production writer
is write-only and has no read, delete, retention-management, legal-hold,
governance-bypass, or bucket-management capability.

**Acceptance:** the protocol and state machines have test vectors, including
pairing and offline recovery verifier vectors; the threat model has no unowned
critical mitigation; ADR-0039 is updated and accepted.

## Milestone 1 — Relay skeleton and opaque relay

**Status:** Complete (2026-09-02). Relay boundaries, identity routing, single-host lifecycle, and opaque forwarding passed acceptance coverage. Remote commands remain disabled. [History](./adr-0039-implementation-log.md#milestone-1--relay-skeleton-and-opaque-relay).

**Objective:** establish deployable Worker/Durable Object boundaries without
remote execution.

**Actions:**

1. Add a separately testable Cloudflare Worker project with local development,
   type checking, unit tests, migration configuration, and environment bindings.
2. Implement strict `/@<username>` route parsing while leaving all non-`/@*`
   traffic untouched.
3. Add the identity-index object and immutable-account-ID keyed account object,
   including normalized username uniqueness and rename transactions.
4. Add authenticated host and browser WebSocket attachment, one-current-host
   enforcement, immutable host IDs, hibernation-safe attachment metadata,
   bounded queues, relay-observed socket health, same-key reconnect/displacement
   rules, and deterministic offline/overload errors. A different key must never
   replace an existing host connection or identity outside the replacement
   flow. A healthy same-key displacement must terminate the older socket,
   create a high-severity correlated audit event, notify the user with the ADR's
   privacy-safe metadata (never raw IP/precise location/full headers), and expose
   step-up-protected atomic host revocation; stale reconnects are audit-only.
   The first alert is immediate. Later alerts in the incident window are batched
   and delivered with counts/times, an exact distinct-fingerprint count, and a
   bounded sample of opaque network fingerprints, never dropped, while every
   event and its fingerprint remain individually auditable.
5. Relay only opaque test frames with size, rate, origin, and lifetime limits.
   Do not expose a command-capable production route yet.

**Acceptance:** local integration tests cover routing isolation, concurrent
username claims, rejection of a second current host, same-key reconnect,
healthy same-key displacement/older-socket termination, stale-socket quiet
reconnect, first-alert delivery, lossless repeated-alert batching, notification
metadata redaction, five-minute step-up freshness, atomic revocation and its
availability during quota degradation, different-key displacement rejection,
replacement only after revocation, hibernation restore, host offline,
backpressure, malformed frames, and byte-for-byte opaque relay behavior without
payload decoding. Milestone 1 does not provide cryptographic confidentiality
from the relay: test frames remain inspectable in principle until Milestone 3
implements end-to-end encryption, and no command-capable route may be enabled
before that gate passes.

## Milestone 2 — Account authentication and signed host registration

**Status:** Complete (2026-09-03). Authentication, host registration, recovery, revocation, and deletion state machines passed acceptance coverage. Remote commands remain disabled. [History](./adr-0039-implementation-log.md#milestone-2--account-authentication-and-signed-host-registration).

**Objective:** authenticate accounts and prove host possession of its private
key without treating either as command authorization.

**Actions:**

1. Implement email magic-link signup/login with short-lived, rotating sessions,
   CSRF protection, secure cookie settings, rate limits, and mandatory second
   factor before remote-access session or initial host registration issuance.
2. Add CLI account login and local host-key generation/storage with explicit
   permissions and no private-key export.
3. Register each host public key under an immutable host ID and require
   nonce-bound signed challenges on each host WebSocket connection. Reject
   stale, replayed, mismatched, or in-place key-replacement registrations.
4. Add account, session, current/revoked-host, key-epoch, and browser-device
   listing and revocation surfaces.
5. Implement the ADR recovery split: account recovery restores administration,
   while host loss creates a new trust root, revokes the old host's sessions,
   pairings, and capabilities, and never inherits its identity or trust.
6. Implement the account-deletion state machine from `shore-state-machines.md`:
   a fresh-second-factor-gated seven-day cooling-off period, cancellable by
   session or notification cancel token, that on completion revokes the
   current host/sessions/devices, cryptographically erases personal fields,
   and permanently tombstones the account and username so neither is ever
   reused.

**Acceptance:** tests cover token replay/expiry, session rotation/revocation,
login and registration throttling, second-factor enforcement, host
impersonation, mismatch/displacement rejection, same-key reconnect, recovery
notifications/seven-day cooling-off/cancellation, lost-host replacement, proof
that old pairings/capabilities are not inherited, account-deletion
fresh-second-factor enforcement, idempotent start/cancellation, alarm-driven
completion (host/session/device revocation, personal-field erasure, and
permanent username tombstoning verified against the identity index), and the
24-hour pre-completion warning. Passing this milestone still does not permit
commands.

## Milestone 3 — End-to-end channel and local pairing

**Status:** Complete (2026-09-04). Host/browser interoperability, pairing, durable device trust, rate limits, and negative tests passed. The independent review found no unresolved critical or high findings. [History](./adr-0039-implementation-log.md#milestone-3--end-to-end-channel-and-local-pairing).

**Objective:** establish relay-blind, mutually authenticated communication
between a paired browser device and the host.

**Actions:**

1. Implement the specified crypto envelope on host and browser using audited
   platform cryptography, canonical bytes, and published cross-language test
   vectors.
2. Generate a non-extractable browser key and perform the reviewed local pairing
   protocol. Its QR/human representation has at least 128 bits of entropy,
   expires after five minutes, is single-use, allows at most five failures, is
   rate-limited at every relevant identity layer, resists offline guessing, and
   binds account, immutable host, browser device, protocol version, nonce, and
   both public-key fingerprints.
3. Persist approved device keys and replay state on the host. Pin the host key
   in the browser. Require local approval for key changes; recovery revokes old
   device trust.
4. Enforce expiry, monotonic sequence, unique request ID, signature, and
   authenticated-encryption validation before decoding any ADR-0040 frame.

**Acceptance:** interoperability and negative tests cover tampering, wrong keys,
replay across connections, reordered/duplicate IDs, clock skew, key
substitution, pairing expiry/reuse/attempt exhaustion/races/rate limits/offline
guessing, host-key epoch changes, revocation, recovery, and relay frame
injection. An independent security review has no unresolved critical or high
findings.

## Milestone 4 — Capability-scoped ADR-0040 relay

**Status:** Complete (2026-09-07). All acceptance tests and the independent security review passed with no unresolved critical or high findings. Production enablement remains a separate deployment gate. [Implementation history](./adr-0039-implementation-log.md#milestone-4--capability-scoped-adr-0040-relay).

**Objective:** expose a minimal safe subset of the existing real-time protocol.

**Actions:**

1. Add a host-side Shore adapter that feeds decrypted, authorized frames into
   the same ADR-0040 command and subscription handlers used by `/ws/v1`.
2. Map each allowed message type and subscription scope to an explicit,
   versioned capability. Deny unlisted fields, commands, scopes, and protocol
   versions before dispatch.
3. Preserve ADR-0040 request IDs, idempotency, event cursors, acknowledgements,
   replay/snapshot semantics, heartbeat, frame limits, and backpressure through
   reconnects. Relay routing metadata must remain outside encrypted content.
4. Initially enable read-only dashboard/state operations. Add non-destructive
   mutations individually only after authorization and parity tests exist.
5. Keep arbitrary shell disabled. Its separate grant flow must be local,
   device-bound, warned, audited, immediately revocable, and time-limited.

**Acceptance:** transport-parity tests run identical permitted scenarios over
direct `/ws/v1` and Shore and produce equivalent normalized state. Authorization
tests prove every non-allowlisted command/scope fails closed without side
effects.

### Implementation history

The completed work log, test evidence, deployment notes, and remaining sub-gates are maintained in the [ADR-0039 implementation log](./adr-0039-implementation-log.md).

## Milestone 5 — Correlated tamper-evident audit

**Status:** Complete (2026-09-22). Preproduction B2 verification, Cloudflare
Access provisioning and operator-plane deployment, disposable-account repair,
the complete live verifier/archive acknowledgement, and the final independent
security review (codex) all passed with no unresolved critical, high, or
medium findings. Production enablement of the operator control plane's
mutating operations remains a separate deployment gate.
[Implementation history](./adr-0039-implementation-log.md#milestone-5--correlated-tamper-evident-audit).

**Objective:** make account, pairing, capability, and command activity
attributable without storing command plaintext.

**Actions:**

1. Relay records account/device/session IDs, a keyed pseudonymous source
   fingerprint, receipt time,
   request ID, and ciphertext/command commitment as a hash-chained event.
2. Host records a signed event with the same request ID, command hash,
   authorization decision, outcome, and host time.
3. Send host-signed batches over the authenticated Shore channel; Shore verifies
   them and exports both streams to append-only storage using Shore-only
   credentials. Add signed per-frame relay receipts so the host detects chain
   gaps, regression, or conflict before dispatch.
4. Add user-visible session/device/capability history and security notifications
   for pairing, key changes, healthy same-key host displacement, recovery,
   revocation, and privileged grants. Displacement notifications include an
   immediate-access revoke-host action protected by recent step-up and correlate
   to the relay audit event. Raw IP and precise location are not retained or
   copied into browser/out-of-band notifications.
5. Add a separately authenticated operator control plane: a narrow admin API,
   web console, and CLI for account lookup/listing and explicit recovery
   operations. It must support the preproduction TOTP repair required to rerun
   the live verifier without exposing arbitrary Durable Object mutation.

**Acceptance:** tests detect deletion, insertion, mutation, receipt-chain gaps,
regression/conflict, missing correlation, and forged host events; receipt
failure disables Shore remote dispatch without affecting local/direct access;
no AgentSquid host configuration contains B2 credentials; a healthy same-key displacement produces
both the correlated audit record and first user notification while stale
reconnect does not alert; batching preserves and later surfaces every repeated
event; revocation is step-up protected and atomically invalidates the host
connection, browser sessions, pairings, and capabilities; retention and
redaction tests show command text, secrets, raw IP, precise location, and full
headers are absent from user notifications by default. Operator actions require
independent operator authentication and authorization, are environment-bound,
use closed-schema commands, and create immutable audit records; production
operator mutation remains disabled until the final Milestone 5 review closes.

**Storage model:** host SQLite and each account's Durable Object SQLite are
bounded hot stores, not the long-term archive. Shore exports deterministic
signed/hash-chained batches to Object-Locked B2. Only after a successful B2
write or a verified Shore-signed host-batch acknowledgement may either hot
store compact acknowledged rows. Both retain the newest 1,000 acknowledged
events plus all unacknowledged events and a durable chain-base checkpoint
(`seq`, `hash`; host checkpoints are signed) so retained-tail verification and
future appends remain continuous. Export failure never authorizes deletion.
The archive retention policy, rather than SQLite capacity, defines long-term
retention. Export lag over five minutes remains an operational alert.

### Implementation history

The completed work log, test evidence, deployment notes, and remaining sub-gates are maintained in the [ADR-0039 implementation log](./adr-0039-implementation-log.md).

## Milestone 6 — Production hardening and staged rollout

**Status:** In progress; Milestone 5 is complete, 6.1 is partially complete,
and 6.2 is under implementation.

**Objective:** prove the system fails closed and is operable before enabling
remote mutations.

**Actions:**

1. Add end-to-end tests for Worker/Durable Object restarts, region changes,
   network loss, duplicate connections, cursor rollover, offline hosts, overload,
   revocation during execution, recovery, and multi-device convergence.
2. Add abuse controls, CSP, exact-version client selection, immutable release
   manifests and content-hashed assets, reproducible-build hashes, dependency
   scanning, secret rotation, alerts, metrics, runbooks, backups,
   migration/rollback procedures, and kill switches. Keep the bootstrap small
   and version-independent. Have each host advertise
   `required_client_version`; load only the matching release manifest and show
   a non-command-capable update/unavailable screen when it is missing,
   malformed, revoked, or mismatched. Publish and verify the binary, client
   manifest, and referenced assets as one release transaction, preserving the
   prior pair for rollback. Retain stable release manifests by default and
   allow byte-identical assets to be shared by content hash.
   Implement ADR-0039's per-route/per-account traffic accounting, quota
   projections, 50/70/85/95-percent degradation thresholds, reserved security
   capacity, paid-plan spend ceiling, and explicit Shore-unavailable response.
   Document and test the opt-in local/Tailscale fallback before launch.
3. Perform independent cryptographic/application security review and remediate
   all critical/high findings.
4. Roll out in stages: internal opaque relay, paired read-only access,
   allowlisted non-destructive mutations, then separately granted shell access.
   Each stage has an immediate server-side disable path.

**Acceptance:** production readiness review signs off security, operations,
privacy, cost limits, recovery, and rollback. Release tests prove that every
supported binary resolves to its exact immutable client, mismatches and missing
or revoked clients fail closed without opening a command-capable session,
published manifests cannot be overwritten, reproducible hashes match, and a
binary/client rollback restores the previous pair. No remote mutation ships
before the paired read-only stage is stable and its gates pass.

## Definition of done

ADR-0039 is implemented only when every milestone acceptance gate passes,
documentation and recovery paths are usable, direct access remains compatible,
and production remote commands cannot bypass pairing, encryption, capabilities,
replay protection, revocation, or audit.
