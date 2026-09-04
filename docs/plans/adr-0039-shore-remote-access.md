# Plan: ADR-0039 Shore remote access

**Status:** In progress (2026-09-03). Milestones 0–2 are complete. Milestone 3
Action 1, the local pairing and persisted device-trust cores, and the Action 4
validation core are implemented on both the host (Python) and browser
(TypeScript) sides, backed by shared cross-language test vectors; see its
status note for what a model-assisted review round found and fixed along the
way. The host-side pairing coordinator now rate-limits itself locally, and the
broker account object enforces pairing packet and ceremony-churn limits across
account, browser-device, and source-fingerprint identities.
The browser transport client, non-extractable device identity, pinned host trust,
and durable replay/sequence state are implemented. Cross-process browser/host
pairing and encrypted-probe interoperability now run in CI. Remaining live
key-rotation acceptance coverage, production deployment wiring, and an
independent (human) security review remain before the milestone gate passes.
Milestone 4 has not started. No production command-capable route is enabled. Milestone 5 is
blocked until Milestones 3 and 4 pass their acceptance gates.

This is the implementation plan for
[ADR-0039](../decisions/0039-remote-access-via-shore-broker.md). The ADR owns
the architectural and security decisions; this document owns sequencing,
acceptance gates, and verification. Implement the milestones in order. Do not
enable remote command execution until every production gate is satisfied.

ADR-0040's local WebSocket migration is complete enough to begin this work.
Shore must relay that versioned application protocol rather than introduce a
second command, event, replay, or snapshot model.

## Non-negotiable invariants

- The broker never receives keys that can decrypt or forge command payloads.
- Account login alone never authorizes a device to issue host commands.
- Host and browser device keys are generated and retained locally.
- Host identities are immutable and append-only; a different key cannot replace
  or displace an existing host identity through account login.
- Each account has exactly one current host; a second host is rejected unless
  the current identity is revoked through the defined replacement flow.
- Pairing establishes trust through a host-displayed code that commits to both
  device-key fingerprints; broker-provided trust-on-first-use is insufficient.
- Account recovery cannot recover cryptographic trust or inherit old pairings
  and capabilities; replacement hosts are visibly new trust roots.
- Every command is signed, encrypted, capability-checked, replay-protected,
  expiry-checked, and correlated with tamper-evident audit records.
- Remote access is disabled by default. Arbitrary shell capability is separate,
  device-specific, locally granted, revocable, and expires within 24 hours.
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
5. Threat-model a malicious broker, stolen browser session, stolen device,
   replay, key substitution, compromised host, malicious client update, Durable
   Object restart, and account recovery.
6. Revalidate current Cloudflare limits, pricing, routing, Durable Object
   hibernation behavior, WebSocket constraints, and rate-limiting availability
   before relying on them operationally.

**Audit archive deployment:** use private Backblaze B2 buckets with default
SSE-B2 encryption and Object Lock enabled. Pre-production uses
`shore-audit-test` (`fc55027ee2ac52e1ae0f051c`) with one-day Governance
retention. Production uses `shore-audit-prod`
(`0c55d2aee29c52e1ae0f051c`); enable default 400-day Compliance retention and
post-retention lifecycle deletion only after exporter verification, but before
admitting external users. Use separate bucket-scoped keys, never production
credentials in tests, and never commit key IDs or secrets. The production
writer is write-only and has no read, delete, retention-management, legal-hold,
governance-bypass, or bucket-management capability.

**Acceptance:** the protocol and state machines have test vectors, including
pairing and offline recovery verifier vectors; the threat model has no unowned
critical mitigation; ADR-0039 is updated and accepted.

## Milestone 1 — Broker skeleton and opaque relay

**Status:** Complete (2026-09-02). The broker skeleton, identity index,
single-host lifecycle, opaque relay, and acceptance coverage are implemented.
No production command-capable route is enabled.

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
   bounded queues, broker-observed socket health, same-key reconnect/displacement
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
from the broker: test frames remain inspectable in principle until Milestone 3
implements end-to-end encryption, and no command-capable route may be enabled
before that gate passes.

## Milestone 2 — Account authentication and signed host registration

**Status:** Complete (2026-09-03). Account authentication,
second-factor enforcement, signed host registration, device revocation, and
recovery flows are implemented with acceptance coverage. Account deletion
(Action 6, added 2026-09-03) closes a gap found in review:
`shore-state-machines.md`'s
account-deletion state machine was normative from Milestone 0 but had no
implementation, route, or assigned action item. It is now implemented as
`startDeletion`/`cancelDeletion`/`completeDeletion` in `src/index.ts`,
mirroring the existing recovery flow's fresh-second-factor, seven-day
cooling-off, 24-hour pre-completion warning, and cancellation-token design:
completion revokes the current host, all sessions, and all devices; bumps the
generation counter; cryptographically erases `account-email`, `totp-secret`,
and the recovery verifier (audit/notification records are deliberately
retained as legally/security-required); and calls a new `IdentityIndex`
`/tombstone` route that releases the username and email from resolution and
permanently blocks the username from being claimed or renamed into again
(unlike a rename's 30-day, eventually-reusable tombstone), consistent with
ADR-0039's "deleted IDs ... are never reused."

A final pre-publish review found and closed a browser-session socket
invalidation gap. Hibernation-safe browser attachment metadata now retains the
authenticating session ID; every frame revalidates that durable session before
rate-limit mutation or relay; and logout, administrative revocation, step-up
rotation, and refresh rotation immediately close matching sockets. Tests cover
explicit revocation and refresh rotation. The same remediation migrated the
older signup, login/registration, and attachment-failure source-IP rate keys
from reversible unsalted SHA-256 to the keyed HMAC network fingerprint already
used by pairing and displacement alerts.
Session-changing routes and browser/device attachment now share the attachment
critical section, and the session is revalidated inside that section before a
socket is accepted. This closes the final rotation-versus-attachment race. The
frame path also checks account generation before any durable pairing-rate
mutation, so queued frames from revoked connections cannot consume current
pairing capacity.

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

**Status:** In progress (2026-09-03). Action 1 (envelope) and Action 4
(validation core) are implemented and independently reproduce
`shore-protocol-v1-vectors.json` byte-for-byte on both the host
(`agent/shore_crypto.py`) and browser/broker (`shore/src/crypto.ts`) sides.
Action 2 (pairing) and the durable device-trust core in Action 3 are also
implemented on both sides, after a model-assisted review round found and the
team fixed, in order: the original pairing design could not fit the 128-bit
secret and 128-bit nonce into one 130-bit human code and required the browser
to know unverified host fingerprints before it could decrypt anything
(circular); the protocol was amended to a bootstrap-key three-packet
ceremony (`docs/decisions/0039-remote-access-via-shore-broker.md`'s
2026-09-03 amendment) with real (not placeholder) fingerprint vectors; the
host-side `PairingCoordinator` was rewritten to start blind and learn the
browser's identity only from its first encrypted packet; a matching
browser-side reference implementation was added to `shore/src/crypto.ts`
(previously absent entirely, leaving only one side of the handshake
implemented); and the browser side was found to skip the spec's "compare the
offer's account and host IDs against the authenticated route" check, since
fixed. The host-side `PairingCoordinator` now also rate-limits itself
(`agent/shore_crypto.py`): a sliding 5-minute window caps both ceremony
creation and aggregate failed attempts across ceremonies, closing a gap
where the existing per-ceremony 5-attempt lockout could be reset for free by
just starting a new ceremony. The broker now recognizes only the public outer
schema of browser-to-host pairing packets and, without inspecting ciphertext,
atomically caps packets per ceremony and distinct ceremonies per five-minute
window across the account, browser-device, and source-fingerprint identity
layers (`shore/src/index.ts`). Pairing rate records expire through the existing
alarm cleanup, and source fingerprints use keyed HMAC rather than reversible
plain hashes. A pre-publish review of this broker code found and fixed two
bugs before either side of the diff was published: the churn check re-fetched
the same storage keys twice per identity per packet (once to check, once to
update) for no reason, and — more seriously — the per-ceremony dedupe marker
that keeps a single ceremony from counting twice toward the churn budget was
itself scoped to the rate window, so a ceremony whose packets straddled a
five-minute window boundary got counted as a fresh "ceremony start" in each
window it touched, inflating the churn counter against ordinary slow
ceremonies. Both are fixed and covered by a regression test that drives the
rate limiter directly with controlled timestamps either side of a window
boundary (confirmed to fail against the pre-fix code: it reported 6 counted
starts instead of the correct 3). The final review also closed the browser
session socket-invalidation and older reversible source-IP rate-key gaps
described in Milestone 2. A subsequent review round of that same
source-IP-rate-key migration found three more issues, all fixed before
publish: the new `IdentityIndex` signup-rate fingerprint re-imported its HMAC
key on every request instead of caching it like `Account.networkFingerprint`
does (now fixed the same way); the pairing-packet detector required an exact
5-key/field match, so a packet with one extra or mismatched field still
targeting a real ceremony_id was invisible to the per-ceremony/churn counters
and relayed unthrottled (loosened to key off a valid `ceremony_id` alone,
since the host — not the broker — is responsible for wire-format validity);
and the per-ceremony rate records' expiry was window-aligned rather than
anchored to arrival time, so a ceremony whose first packet landed near a
window boundary could have its dedupe marker swept by the alarm cleanup
before the ceremony's own 5-minute lifetime ended, reintroducing the same
double-counting bug window-boundary straddling had just fixed. All three are
covered by regression tests confirmed to fail pre-fix (the schema-evasion
test times out waiting for a rate-limit close; the TTL test asserts the
dedupe marker outlives one full ceremony lifetime from arrival, which failed
by design against the window-aligned expiry). 15 negative-acceptance tests
cover signature/key substitution, key-epoch mismatch, clock skew, expiry,
replay and reordering at the envelope layer, pairing-confirmation reuse after
completion, device-trust revocation, and the two new rate limits
(`tests/test_shore_crypto.py`), plus broker per-ceremony, churn,
window-boundary, packet-schema, and TTL behavior, persistence/privacy
behavior, session revocation, refresh rotation, attachment races, and revoked
generation ordering (`shore/test/shore.test.ts`); host suite is 32/32 and
broker suite is 82/82, `tsc --noEmit` clean. The first runtime transport slice
is also present: Shore exposes `/relay` through the existing signed-host and
remote-browser-session attachment checks while explicitly rejecting the legacy
test bootstrap bearer, and continues to relay its binary bytes opaquely. On the
host, `agent/shore_transport.py` joins the pairing coordinator and durable
trust/replay stores to a fail-closed dispatcher. Its only post-pairing plaintext
operation is a harmless `shore.probe` round trip; all other message types are
rejected, so ADR-0040 commands remain disabled. Tests cover the real Durable
Object WebSocket path, pairing through the runtime dispatcher, trust/replay/
outbound-sequence persistence over dispatcher reconstruction, and rejection of
command-shaped input. Still open before the milestone gate can pass: the
host WebSocket connection core now obtains a fresh broker challenge, signs the
canonical connection proof, dispatches binary frames through `ShoreChannel`,
sends broker-consumed lease heartbeats, and reconnects with bounded exponential
backoff (`ShoreHostConnection` in `agent/shore_transport.py`). The broker now
consumes zero-length binary lease heartbeats without forwarding them to browser
devices, closing the idle-host expiry gap, and has an integration regression
test for that behavior. Pre-publish review then restricted those heartbeats to
host sockets, put them inside the ordinary per-minute frame budget, normalized
malformed successful challenge responses into retryable protocol failures, and
added direct coverage for fresh challenges across retries, bounded backoff, and
clean stop. A second pre-publish review amended the normative protocol and ADR
to define the host-only empty-byte heartbeat, made 1008/1009 closes terminal as
the protocol requires, and preserved exponential backoff across short-lived
post-handshake failures while resetting it only after a stable connection. The
next review made the heartbeat deadline independent of inbound traffic so
malformed frames cannot suppress the host lease, and made non-transient 4xx
WebSocket upgrade failures terminal while preserving retry for 408, 425, 429,
and server failures; permanent 4xx failures from the preceding challenge HTTP
request follow the same policy. Final review also distinguished routine socket
lifetime and heartbeat expiry (retryable 1001 with a fresh challenge) from
terminal policy/oversize closures. The daemon now starts and cleanly stops that
host connection after an explicit successful login, loading atomically persisted,
mode-0600 public routing metadata and the existing protected host identity from
the configurable Shore identity directory. Registration responses supply the
immutable account ID, normalized username, and host key epoch needed to reconstruct
the channel; absent or invalid persisted configuration fails closed and leaves Shore
disabled. A final review added strict broker-metadata validation before persistence
and prevented malformed `shore.identity_dir` configuration from aborting daemon
startup. The pre-publish review also restored the existing account-activation ID
source after catching an unintended adjacent edit, made the registration metadata's
account ID derive from the Durable Object identity rather than a forwarding header,
and made daemon shutdown prompt while surfacing unexpected connection-task failures.
The second pre-publish review normalized malformed persisted broker types into the
fail-closed configuration path and bound administrative login responses back to the
explicitly requested immutable account ID. A third review made Shore identity-path
validation shared by startup and the config editor, rejecting falsy non-mappings,
empty paths, and cwd-relative paths; it also made normal premature connection-task
termination visible instead of silent. A fourth review hardened broker URL parsing
against deferred invalid-port/IPv6 errors and whitespace, aligned persisted usernames
with the broker's reserved-name rules, and added regression coverage for those cases.
A fifth review centralized those invariants at the persistence boundary itself, so
future callers cannot bypass validation by invoking the atomic writer directly.
A sixth review moved canonical username and UUIDv7 validation ahead of identity
creation and network access, preventing CLI route-identifier injection when a
session bearer is supplied. A seventh review restricted plaintext broker URLs to
explicit loopback development endpoints, preventing account/session credentials and
host attachment traffic from crossing a network without TLS. An eighth review aligned
Python key-epoch validation with TypeScript's safe-integer ceiling at envelope,
pairing, trust-storage, and persisted-configuration boundaries, preventing divergent
cross-language canonical values. A ninth review bound successful registration
responses back to the locally proved host ID and both public keys before persisting
connection state, rejecting stale or misrouted success responses. A tenth review
made Python envelope sealing validate canonical UUIDs, safe sequence/epoch ranges,
timestamps, nonce length, and serializable plaintext before cryptographic work,
with stable fail-closed errors. An eleventh review made sealing enforce the protocol's
strictly-positive, at-most-60-second validity interval, preventing locally generated
frames that every conforming peer must reject. A twelfth review made connection
metadata replacement crash-durable by syncing the containing directory and enforces
mode 0700 on that identity directory at every write. A thirteenth review tightened
registration-response binding from raw key coordinates to the complete expected
Ed25519 and X25519 public JWKs, rejecting altered curve labels or extra key metadata.
The browser application is now implemented in `shore/browser`: it resolves
immutable route metadata through the authenticated security surface, connects
with the same-origin session cookie, generates and persists non-extractable
Ed25519/X25519 device keys in IndexedDB, pins host trust without silently
accepting key changes, persists replay and outbound-sequence state, and performs
the pairing and encrypted probe flows. A cross-process integration test now
drives that TypeScript client against the real Python `ShoreChannel`, completing
pairing and an encrypted probe without replacing either implementation with a
test double; Shore CI checks out both repositories and runs this required gate.
The browser has 35 passing unit tests plus this passing cross-process test, and its
`tsc --noEmit` check is clean. Explicit host-side broker-injection coverage now
proves malformed plaintext and a cryptographically well-formed envelope from an
untrusted device both fail before application dispatch. The focused host suites
pass 84/84, the broker suite passes 85/85, and `tsc --noEmit` remains clean.
Still open before the milestone gate can pass: live host-key epoch-rotation
coverage; production
deployment wiring; and an independent, qualified human security review. The
review rounds so far were model-assisted, not the required human review.

**Objective:** establish broker-blind, mutually authenticated communication
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
guessing, host-key epoch changes, revocation, recovery, and broker frame
injection. An independent security review has no unresolved critical or high
findings.

## Milestone 4 — Capability-scoped ADR-0040 relay

**Status:** Not started; depends on Milestone 3.

**Objective:** expose a minimal safe subset of the existing real-time protocol.

**Actions:**

1. Add a host-side Shore adapter that feeds decrypted, authorized frames into
   the same ADR-0040 command and subscription handlers used by `/ws/v1`.
2. Map each allowed message type and subscription scope to an explicit,
   versioned capability. Deny unlisted fields, commands, scopes, and protocol
   versions before dispatch.
3. Preserve ADR-0040 request IDs, idempotency, event cursors, acknowledgements,
   replay/snapshot semantics, heartbeat, frame limits, and backpressure through
   reconnects. Broker routing metadata must remain outside encrypted content.
4. Initially enable read-only dashboard/state operations. Add non-destructive
   mutations individually only after authorization and parity tests exist.
5. Keep arbitrary shell disabled. Its separate grant flow must be local,
   device-bound, warned, audited, immediately revocable, and time-limited.

**Acceptance:** transport-parity tests run identical permitted scenarios over
direct `/ws/v1` and Shore and produce equivalent normalized state. Authorization
tests prove every non-allowlisted command/scope fails closed without side
effects.

## Milestone 5 — Correlated tamper-evident audit

**Status:** Blocked (2026-09-02). Milestone 3 remains incomplete and Milestone
4 has not started; both preceding acceptance gates must pass before this work
begins.

**Objective:** make account, pairing, capability, and command activity
attributable without storing command plaintext.

**Actions:**

1. Broker records account/device/session IDs, source metadata, receipt time,
   request ID, and ciphertext/command commitment as a hash-chained event.
2. Host records a signed event with the same request ID, command hash,
   authorization decision, outcome, and host time.
3. Export both streams to append-only storage under separate credentials that
   neither a remote session nor the host runtime can erase.
4. Add user-visible session/device/capability history and security notifications
   for pairing, key changes, healthy same-key host displacement, recovery,
   revocation, and privileged grants. Displacement notifications include an
   immediate-access revoke-host action protected by recent step-up and correlate
   to the broker audit event. Raw IP and precise location remain restricted to
   the audit system and are never copied into browser/out-of-band notifications.

**Acceptance:** tests detect deletion, insertion, mutation, fork, missing
correlation, and forged host events; a healthy same-key displacement produces
both the correlated audit record and first user notification while stale
reconnect does not alert; batching preserves and later surfaces every repeated
event; revocation is step-up protected and atomically invalidates the host
connection, browser sessions, pairings, and capabilities; retention and
redaction tests show command text, secrets, raw IP, precise location, and full
headers are absent from user notifications by default.

## Milestone 6 — Production hardening and staged rollout

**Status:** Not started; depends on Milestone 5.

**Objective:** prove the system fails closed and is operable before enabling
remote mutations.

**Actions:**

1. Add end-to-end tests for Worker/Durable Object restarts, region changes,
   network loss, duplicate connections, cursor rollover, offline hosts, overload,
   revocation during execution, recovery, and multi-device convergence.
2. Add abuse controls, CSP, immutable/versioned client assets,
   reproducible-build hashes, dependency scanning, secret rotation, alerts,
   metrics, runbooks, backups, migration/rollback procedures, and kill switches.
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
privacy, cost limits, recovery, and rollback. No remote mutation ships before
the paired read-only stage is stable and its gates pass.

## Definition of done

ADR-0039 is implemented only when every milestone acceptance gate passes,
documentation and recovery paths are usable, direct access remains compatible,
and production remote commands cannot bypass pairing, encryption, capabilities,
replay protection, revocation, or audit.
