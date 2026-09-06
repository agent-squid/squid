# Plan: ADR-0039 Shore remote access

**Status:** In progress (2026-09-04). Milestones 0–3 are complete. Milestone 3's
Action 1, the local pairing and persisted device-trust cores, and the Action 4
validation core are implemented on both the host (Python) and browser
(TypeScript) sides, backed by shared cross-language test vectors; see its
status note for what review rounds, including the independent human security
review that closed its gate, found and fixed along the
way. The host-side pairing coordinator now rate-limits itself locally, and the
broker account object enforces pairing packet and ceremony-churn limits across
account, browser-device, and source-fingerprint identities.
The browser transport client, non-extractable device identity, pinned host trust,
and durable replay/sequence state are implemented. Cross-process browser/host
pairing, encrypted-probe interoperability, and live host-key epoch rotation now
run in CI. Pre-production deployment wiring is complete: a manually triggered,
environment-protected workflow deploys an isolated `workers.dev` Worker only
after type checking, tests, a high-severity dependency audit, and strict
runtime-secret validation, with credentials held as environment secrets. The
runtime secret is uploaded atomically with the reviewed deployment through a
mode-restricted, cleanup-trapped temporary file and declared required in
Wrangler. Version preview URLs are disabled, and browser
attachment fails closed because the isolated hostname cannot satisfy Shore's
same-site cookie requirement; it is a broker/host integration target only.
An independent, qualified human security review passed on 2026-09-04 with no
unresolved critical or high findings, closing Milestone 3's gate. Under that
approval, the production `agentsquid.ai/@*` route is now declared in
`shore/wrangler.jsonc` and a manually triggered, environment-protected
`deploy-production.yml` workflow exists alongside the pre-production one. No
production deployment has run yet: the `shore-prod` GitHub environment now
has its `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `FINGERPRINT_KEY`
secrets configured, but no required-reviewer protection rule (an accepted
interim exception recorded in `docs/shore-security-operations.md` pending a
second contributor). Triggering the workflow remains a separate, explicit
step.
Milestone 4 has not started, so the production route stays opaque-relay only;
no command-capable dispatch is enabled. Milestone 5 remains blocked until
Milestone 4 passes its acceptance gate, and external users must not be
admitted in production until Milestone 5's audit export is also verified.

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

**Status:** Complete (2026-09-04). Action 1 (envelope) and Action 4
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
remote-browser-session attachment checks. Relay upgrades now carry the immutable
account ID learned during authenticated discovery and route directly to that
account object, which rechecks its durable current username; this removes the
global `IdentityIndex` from the live socket path and prevents arbitrary username
traffic from serializing all relay connections. The public `/test-relay` route
and its bootstrap bearer compatibility surface have been removed, while the
broker continues to relay binary bytes opaquely. On the
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
The same scenario rotates the live Python channel to new host keys and epoch,
proves the old browser trust fails closed, proves replacement is rejected without
explicit approval, re-pairs through a new ceremony with a named local-approval
option, and completes an encrypted probe under the new epoch. The browser has
36 passing unit tests plus this passing cross-process test, and its
`tsc --noEmit` check is clean. Explicit host-side broker-injection coverage now
proves malformed plaintext and a cryptographically well-formed envelope from an
untrusted device both fail before application dispatch. The focused host suites
pass 84/84, the broker suite passes 85/85, and `tsc --noEmit` remains clean.
The next deployment slice is complete: Shore has a manual, serialized
pre-production deployment workflow protected by the `shore-dev`
GitHub environment, separate Cloudflare credentials, test/typecheck gates, and
an isolated `workers.dev` hostname and Durable Objects. The workflow refuses to
deploy without a 256-bit hexadecimal `FINGERPRINT_KEY`, preventing an absent or
weak fingerprint-HMAC key. The secret is uploaded atomically by the gated deploy
rather than through `wrangler secret put`, which would itself publish an
ungated Worker version. A high-severity dependency audit also gates deployment,
and version preview URLs
are disabled so superseded versions are not left reachable.
Browser attachment is explicitly disabled there because the cross-site hostname
cannot carry Shore's `SameSite=Strict` session cookie; browser acceptance remains
on the reviewed same-site production route. The default Wrangler
configuration now fails closed with no production custom route and
`workers_dev` disabled, correcting the prior contradiction where the
`agentsquid.ai/@*` route was configured while the README said not to deploy it.
The independent, qualified human security review required for this
milestone's acceptance gate was completed on 2026-09-04 with no unresolved
critical or high findings. Under that approval, the production
`agentsquid.ai/@*` route is now declared in `shore/wrangler.jsonc` and a
manually triggered `deploy-production.yml` workflow, gated by the
`shore-prod` GitHub environment, has been added. That environment's
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `FINGERPRINT_KEY` secrets
are configured, though it has no required-reviewer protection rule yet (an
accepted interim exception recorded in `docs/shore-security-operations.md`
pending a second contributor). No production deployment has been run;
triggering the workflow is a separate, explicit step outside this review.

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

**Status:** In progress. 4.0 (pairing and approval UI), 4.1 (transport-neutral
subscription core), and 4.2 (capability registry) are landed; 4.3–4.9 have not
started. No production dispatch path exists yet — 4.0's UI drives the same
pairing ceremony `tests/test_shore_crypto.py` already exercised, and
4.1/4.2 are shared/foundational units with no caller until 4.3/4.4 wire them
into `ShoreChannel`, so the production route stays opaque-relay only until
this milestone's acceptance gate passes.

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

### Implementation plan

**Key findings**

- The capability list is already normative, not something to design —
  `docs/shore-protocol-v1.md` §"Initial capability registry" defines exactly
  one capability today, `dashboard.read.v1` (types `subscribe`, `unsubscribe`,
  `ack`, `ping`, `pong`; scope limited to the global lifecycle feed and
  Flow-step resources already visible locally; no command/HTTP mutation).
  Nothing implements this registry yet.
- No existing capability/permission model to reuse: `_authorize_realtime_scopes`
  (`agent/server.py:4048`) authorizes "phase-one scopes for the fully trusted
  local Squid session" and must not be reused verbatim for a narrower-trust
  remote caller.
- `/ws/v1` (`realtime_v1`, `agent/server.py:4365`) isn't factored for reuse,
  but its data-layer primitives already are: `_realtime_snapshot`,
  `get_realtime_replay`, `_realtime_envelope`, `get_realtime_cursor` all
  operate on `scopes: list[dict]` and DB state with no `WebSocket` reference.
- `ShoreChannel.handle` (`agent/shore_transport.py:72`) is synchronous,
  stateless per call, and models only `shore.probe`'s request/response shape
  — it can't host a push subscription, and one host socket must multiplex
  per-device state for every paired browser, not just one connection.
- The browser client (`shore/browser/src/client.ts`) has only a single-slot
  request/response primitive (`exchange()`), no concurrent/unsolicited
  traffic support.
- No device-scoped, time-limited, revocable grant flow exists for the later
  shell capability; closest analogues: `DeviceTrustStore.revoke()`
  (`agent/shore_crypto.py:412`), ADR-0038's scoped-terminal execution.
- ~~No UI exists anywhere for pairing itself~~ — resolved by 4.0 below. At the
  time 4.1/4.2 landed, `ShoreChannel.begin_pairing()` and the approval path
  (`PairingCoordinator.accept_browser_packet`/`accept_browser_confirmation`)
  were only ever called from `tests/test_shore_crypto.py`; `shore/browser` was
  a headless library with no HTML; `shore/wrangler.jsonc` had no Workers
  Static Assets binding.

#### 4.0 — Pairing and approval UI (prerequisite for the rest of Milestone 4)

**Status:** Landed. Both sides of the ceremony now have real UI, and
`ShoreChannel.begin_pairing`/`PairingCoordinator.accept_browser_packet`/
`accept_browser_confirmation` have a caller outside the test suite for the
first time.

**Host side** (`agent/shore_crypto.py`, `agent/shore_transport.py`,
`agent/server.py`, `ui/index.html`, `ui/app.js`):

- `DeviceTrustStore.list_paired()` enumerates currently-trusted devices (new;
  no such enumeration existed — `get()` only looked up one device at a time).
- `PairingCoordinator` gained bounded, TTL'd ceremony-outcome memory
  (`_record_outcome`/`status()`, 200 entries / 1 hour) so a local UI can poll
  "pending / paired / failed / expired" for a ceremony after it leaves the
  live `_ceremonies` dict — outcomes are recorded at all three terminal
  transitions (confirmed, failure-exhausted, expired-via-timer).
- `ShoreChannel` exposes `pairing_status`, `list_devices`, `revoke_device` as
  thin wrappers, mirroring the existing `begin_pairing` shape.
- `agent/server.py` gained a module-level `_shore_connection` (set/cleared by
  `_lifespan`, previously a function-local the rest of the module couldn't
  reach) and four loopback-gated endpoints reusing the existing
  `_request_is_loopback` gate from `/config/creds/auto`: `POST
  /shore/pairing/begin`, `GET /shore/pairing/status`, `GET /shore/devices`,
  `POST /shore/devices/revoke`. `begin` returns the ceremony's crockford32
  code, offer, expiry, and a `pair_url` with the offer+code JSON-encoded into
  the URL **fragment** (never the query string or path) so the ceremony
  secret never reaches server access logs for the pairing page itself.
- Dashboard UI is a new `/pair` chat command (`ui/app.js`, mirroring the
  existing `/remote` Tailscale-QR command's exact modal pattern —
  hand-built DOM, not a system dialog, per this repo's modal convention) that
  renders the pairing QR (reusing the already-vendored `qrcode.min.js`, the
  same library `/remote` uses) and code, polls `/shore/pairing/status` every
  2s until terminal, and lists/revokes trusted devices with a
  click-again-to-confirm revoke button (no `window.confirm`). Added `pair` to
  `agent/server.py`'s `_SQUID_CHAT_COMMANDS`, matching `remote`'s existing
  entry. PWA cache version bumped in all 5 spots (`ui/sw.js` ×3,
  `ui/index.html` ×2).

**Remote browser side** (new `shore/pairing-app/` package):

- A separate, self-contained npm package (own `package.json`/lockfile/
  tsconfig), not folded into `shore/browser` (which stays a headless,
  build-free library) or the squid repo — encapsulation was an explicit
  choice so the pairing UI's build tooling doesn't leak into either.
- `src/app.ts` decodes the URL fragment (or accepts a pasted link/fragment as
  the manual-entry fallback for QR-less pairing), shows the offer's host key
  fingerprints for confirmation, then drives the already-existing
  `ShoreBrowserClient.pair()`/`loadAuthenticatedShoreRoute` from
  `shore/browser/src/client.ts` unchanged — 4.0 added no new browser crypto,
  only UI around what Milestone 3 already built. Handles
  `host_trust_conflict` with a second, explicit "approve host key change"
  step rather than silently retrying with `approveHostKeyChange: true`.
- Bundled with `esbuild` (new devDependency, scoped to this package only)
  into a single `dist/pair-app.js`, since static assets are served as-is with
  no processing pipeline and `shore/browser`'s sources have no `.js`
  extensions for browser-native multi-file ESM loading.
- **Hosting decision: resolved and landed.** `shore/wrangler.jsonc` gained a
  Workers Static Assets binding (`ASSETS`, directory
  `./pairing-app/dist`) rather than hosting on the separate `agentsquid.ai`
  marketing site. `src/index.ts`'s `parseShoreRoute` now accepts `/pair` and
  `/pair-app.js` as recognized subpaths (sibling-of-username paths, not
  nested under `/pair/`, so the HTML's plain relative `<script
  src="pair-app.js">` resolves correctly without a `<base>` tag or absolute
  URL — nesting would have required either an extra route just for the
  trailing slash or embedding the JS inline).
- **Quota finding revised.** The earlier plan text asserted static-asset
  requests are free and Worker-invoking (billable) requests are the only
  ones that count — true in general, but landing this exposed a routing bug
  that changes the practical answer for this specific page: Cloudflare's
  default assets-first routing (`run_worker_first: false`) does its own
  clean-URL canonicalization *inside* `env.ASSETS.fetch()`, and calling that
  binding with the literal `/pair.html` path made it 307-redirect to `/pair`
  — an absolute-path `Location` scoped to the internal lookup, not the real
  `/@<username>/pair` the browser is on, so the redirect silently stripped
  the username and 404'd. Fixed by requesting the already-clean `/pair`/
  `/pair-app.js` paths directly (no `.html` suffix) *and* setting
  `assets.run_worker_first: true`, since without it the platform's own
  assets-first matcher intercepts non-extensioned paths before the Worker
  ever runs, independent of the redirect bug (confirmed by reproducing 404s
  that vanished only when a query string was present, before either fix was
  in place). Net effect: `/pair` and `/pair-app.js` now invoke the Worker
  like any other route on `shore-prod`, not free static-asset serving as
  originally planned — accepted, since pairing is a rare, one-time-per-device
  action, not page-view traffic, so the billing difference is negligible in
  practice even though the earlier "quota isn't a concern" framing was too
  broad as stated.
- CI (`ci.yml`, `deploy-production.yml`, `deploy-preproduction.yml`) gained
  `pairing-app` as a third package alongside the root Worker and `browser`:
  `npm ci`/`typecheck`/`test`/`audit`, plus `npm run build` before any
  `wrangler deploy`/`--dry-run`, since the assets directory must exist at
  deploy time.

**Tests:** `tests/test_shore_crypto.py` gained `list_paired()` coverage
(returns only currently-paired devices, wraps DB failure in
`ShoreProtocolError`) and `PairingCoordinator.status()` coverage (unknown →
pending → paired/failed/expired transitions, the background-timer expiry
path, and TTL eviction of a terminal outcome). `tests/test_shore_transport.py`
gained a `ShoreChannel.pairing_status`/`list_devices`/`revoke_device`
round-trip through a real completed ceremony. `tests/test_server.py` gained
loopback-gating, not-configured, and success-path coverage for all four new
`/shore/*` endpoints (mocking `_shore_connection`, not a real `ShoreChannel`)
plus a `pair_url` assertion that the ceremony code never appears before the
URL's `#`. `test/shore.test.ts` gained `parseShoreRoute`/`SELF.fetch`
coverage for `/pair` and `/pair-app.js` (200 + expected content, GET-only
enforced, 405 otherwise). `pairing-app` gained its own `vitest` suite (13
cases) for `decodePayload`/`extractFragment`/`parseUsername` — these were
pulled out of `app.ts` into a dependency-free `pairing-link.ts` specifically
so they're testable without a DOM. Full suites re-verified: `pytest -k "shore
or realtime"` 215/215; shore repo `npm test` 91/91; `browser` repo `npm test`
37/37 (1 cross-process test skipped, as before, needing a real squid
process); `pairing-app` `npm test` 13/13, `typecheck` and `build` both clean.
**Not done:** the Playwright/equivalent end-to-end ceremony test originally
scoped here (driving both UI surfaces against a real host process) — the
above proves the pieces work in isolation, not that a human can complete the
flow through the actual rendered pages. Also not done: the *dashboard view*
itself (open question 3 in this milestone's Open Questions) remains out of
scope, deferred to 4.5 as planned.

A pre-publish review caught four real issues, all fixed. (1) This section
originally claimed `test_shore_crypto.py`/`test_shore_transport.py` "cover
the new ... additions" when the diff being described hadn't touched
`tests/` at all — the only verification had been ad hoc, unpersisted
`TestClient` scripts run by hand during implementation. Fixed by writing the
real tests summarized above, which is what makes the claim in this section
true now rather than aspirational. (2) `GET /shore/devices` returned `200
{"devices": []}` when Shore isn't configured, while the other three new
endpoints return `400 {"error": "shore_not_configured"}` for the identical
condition — a monitoring check or future API consumer could misread the
200 as "configured, zero devices" instead of "not configured at all."
Fixed by making all four endpoints respond identically; `ui/app.js`'s
`_shoreFetchDevices()` already tolerated either shape (`data.devices || []`
regardless of status), so no frontend change was needed. (3)
`openShorePairModal`'s `Escape`-key listener was only detached when the
modal closed via `Escape` itself — closing via the `×` button or a backdrop
click left it permanently attached to `document` (the same pattern
pre-existing in `openRemoteQR`, not fixed here since it's out of this
diff's scope). Fixed by routing all three close paths through one `close()`
that always detaches the listener. (4) CI/deploy workflows built
`pairing-app` (creating `pairing-app/dist`) *after* the root package's own
`npm test` step, but `vitest.config.ts` loads `wrangler.jsonc` directly,
whose `assets.directory` points at that same `dist` — reproduced concretely
by deleting `dist` and rerunning `npm test` from a clean state, which failed
the new `/pair` asset test with 404 instead of 200. Fixed by reordering all
three workflow files so `pairing-app` is built before the root suite runs.
A related, self-caught issue during the fix: adding `pairing-app/test/` to
the repo made the shore-root `npm test` silently pick up and rerun those 13
cases too (`vitest.config.ts` excluded `browser/**` but not `pairing-app/**`)
— fixed by excluding it there as well, so each package's tests run exactly
once, under its own config.

**Risks carried forward, not yet addressed:** no independent security review
of the pairing page has happened (pairing-code-leak-via-logs/autofill/
history, clickjacking, confused-device display) — the fragment-based URL and
explicit host-key-change confirmation step were built with those risks in
mind, but that's not a substitute for the review this plan originally called
for as its own slice.

#### 4.1 — Extract a transport-neutral ADR-0040 subscription core

**Status:** Landed, narrower than originally sketched. Reading the real
`realtime_v1` implementation before designing showed the
`RealtimeConnectionState`/injected-`authorize_scopes` abstraction below was
more machinery than the code needed yet — there was no second caller to prove
its shape against. Instead, the replay/rollover/snapshot decision was already
duplicated **verbatim** in two places inside `realtime_v1` (the initial
subscribe-with-cursor path and the end-of-loop steady-state drain), differing
only in the starting cursor. That duplication is now a single function,
`_realtime_catchup(outbound, from_cursor, scopes, principal,
last_acked_cursor) -> int` (`agent/server.py`, right after
`_realtime_snapshot`), and both call sites in `realtime_v1` now just call it
and assign the returned cursor. This is the exact reusable unit Shore's
per-device push loop (4.3) needs — "given a starting cursor and scopes,
decide replay vs. snapshot vs. rollover, send the result, return the new
cursor" — without speculative state-object/callback-injection machinery.
`_handle_realtime_mutation`, `_realtime_chat_start`, `_realtime_auth_start`,
and `_handle_auth_*` were left untouched, as planned — Shore's dispatcher
still can't reach them. `tests/test_realtime.py` (54 tests) passes unchanged,
confirming no behavior drift for direct/Tailscale clients; the full
`test_shore_*` suite also passes. No narrow standalone unit test of
`_realtime_catchup` was added separately, since the existing suite already
exercises both call paths (subscribe-with-cursor and steady-state drain)
through the public WebSocket surface.

A pre-publish review caught two issues in the first version of this slice: (1)
`_realtime_catchup` still took an unused `websocket: WebSocket` parameter,
forwarded only to `_realtime_send` (which never read it either) — that
directly contradicted the claim above that Shore's per-device push loop,
which has no ASGI `WebSocket` at all, could reuse this unit as-is; fixed by
dropping the parameter from both `_realtime_send` and `_realtime_catchup` and
updating all ~32 call sites (mechanical — the parameter was dead code, not
behavior). (2) this doc's own cross-references to `agent/server.py:4338` and
`:4478` (for `realtime_v1` and the `local:{client_id}` principal assignment)
had gone stale in the same edit that inserted `_realtime_catchup` above them;
corrected to their current lines (`4365`, `4505`). Full suite re-verified
after both fixes: `tests/test_realtime.py` 54/54, full `test_shore_*` suite,
and `test_server.py` all pass (one pre-existing, unrelated flake on this dev
machine: `test_lifecycle_start_backgrounds_server` picks up a real `tailscale`
binary on `PATH`).

- **Objective:** make `/ws/v1`'s replay/snapshot decision callable as a unit
  Shore can reuse, with zero behavior change for direct/Tailscale clients.
- **Files:** `agent/server.py` (new `_realtime_catchup`, replacing the two
  duplicated blocks in `realtime_v1`).
- **Deferred, not abandoned:** the broader `RealtimeConnectionState`/injected-
  authorizer wrapper described below is still the likely shape once 4.3
  actually builds Shore's per-device push loop and needs to hold per-device
  scopes/cursor/generation outside of `realtime_v1`'s local variables — revisit
  then, informed by what 4.3 actually needs, rather than guessing now:
  - Pull subscribe/unsubscribe/ack/ping/pong into pure functions over a small
    per-device state object plus an injected `authorize_scopes` callback,
    returning `(new_state, outbound_frames)` — no `WebSocket` calls inside.
  - Import the shared core lazily from `agent/shore_transport.py`, mirroring
    the existing lazy-import pattern that avoids a `server.py`/
    `shore_transport.py` cycle (`agent/server.py:392`).
- **Tests:** existing `tests/test_realtime.py` passes unchanged (54/54).
- **Risks:** the central risk is ending up with two independently evolving
  realtime engines instead of one shared implementation — `_realtime_catchup`
  being the single, only implementation of the replay decision (not a copy)
  is what keeps that from happening.

#### 4.2 — Define the Shore capability registry as an enforced data structure

**Status:** Landed. New `agent/shore_capabilities.py` encodes the registry as
data exactly as sketched: a frozen `ShoreCapability` dataclass (`name`,
`versions`, `types`, per-type `payload_schemas` of closed field allowlists, an
optional `authorize_scope` callback) and a `SHORE_CAPABILITIES` map seeded
with `dashboard.read.v1` (`subscribe`, `unsubscribe`, `ack`, `ping`, `pong`).
`authorize_capability_frame(capability_names, frame)` is the single pure
function implementing the full fail-closed order below; it takes no
`WebSocket`/session state and isn't wired into `ShoreChannel` yet — that
dispatch integration is 4.3/4.4, which can now inject it as a
`validate_frame`-shaped callback alongside `open_envelope` (mirroring
`_validate_probe`, `agent/shore_transport.py:120`). `agent/shore_crypto.py`'s
`shore_devices` table gained a `capabilities` column (migrated in
`DeviceTrustStore._connect` for any pre-existing local dev database, guarded
by a one-time-per-instance flag alongside the existing `_provisioned` cache to
avoid re-running `PRAGMA table_info` on every connection); `TrustedDevice`
gained a matching `capabilities` field. `DeviceTrustStore.approve()` defaults
a newly-paired device to `DEFAULT_CAPABILITIES = ("dashboard.read.v1",)` and
now preserves an existing device's already-granted capabilities across a
same-device epoch bump (re-pairing never resets or upgrades what a device is
allowed to do). `tests/test_shore_capabilities.py` (38 cases) covers every
non-listed real ADR-0040 type, every server-only/unknown type, every
non-global scope shape, every unsupported/missing/non-integer protocol
version, every frame or payload with an extra field, and a device holding no
granted capabilities — each rejected with the specific stable error code from
`docs/shore-protocol-v1.md` and no mutation of `frame`/`payload` on the
rejected path. `tests/test_shore_crypto.py` gained three cases for the new
column: default-on-pairing, preserved-across-epoch-bump, and migration of a
hand-built pre-4.2 `shore_devices` table missing the column. Full
`test_shore_*`, `test_realtime.py`, and `test_server.py` suites re-verified
(one pre-existing, unrelated flake carried over from 4.1's note, plus one
pre-existing unrelated failure in `test_stats_db.py` confirmed present before
this change with `git stash`).

A pre-publish review caught two real bugs and one drift risk in the first
version of this slice. (1) `DeviceTrustStore.approve()`'s `INSERT` for a
newly-paired device omitted the `capabilities` column, relying on the
column's SQL `DEFAULT` — fixed at `ALTER TABLE` time to whatever
`DEFAULT_CAPABILITIES` was *then*. If a later release ever changes
`DEFAULT_CAPABILITIES` on an already-migrated host database, a brand-new
device would get a `TrustedDevice` return value reporting the new set while
the row actually written (and every subsequent `get()`) silently kept the
stale one; fixed by writing the live default explicitly in the `INSERT`.
Covered by `test_approve_writes_the_live_default_not_a_stale_column_default`,
which reproduces the drift by migrating first and changing the default
after. (2) The one-time-per-instance migration guard (`_capabilities_migrated`)
had no protection against a second `DeviceTrustStore` instance racing the
very first migration of a pre-4.2 database — both could see the column
missing and both attempt to add it, and the loser's `sqlite3.OperationalError:
duplicate column name` would surface through `approve()`'s existing
`except (OSError, sqlite3.Error)` as a spurious `pairing_failed`. Fixed by
treating that specific error as already-migrated rather than a failure.
Covered by `test_duplicate_column_race_during_migration_does_not_fail_pairing`,
which forces the race deterministically (real column added out-of-band,
this store's own `PRAGMA table_info` read patched to under-report it) since
`sqlite3.Connection` is an immutable C type and can't be monkeypatched
directly — `sqlite3.connect` itself is patched instead, to return a thin
wrapper. (3) `ADR0040_BROWSER_TO_HOST_TYPES` hand-mirrors the message types
`realtime_v1` dispatches on in `agent/server.py` with no shared source
tying the two together, so a type added to one without the other silently
misclassifies; not fixed (doing so cleanly means either importing
`server.py`'s dispatch surface into this low-level module or waiting for
4.8's negative-test suite to catch drift for real — a comment now documents
the coupling and points at 4.8).

A second pre-publish review pass caught two more real issues, both fixed,
plus one already-acknowledged deferral it re-flagged. (1) `DeviceTrustStore.
get()`/`approve()` deserialized the `capabilities` column with a bare
`json.loads(...)` outside any exception guard, so a corrupted column (disk
damage, a hand-edited DB) would leak a raw `json.JSONDecodeError`/
`ValueError` instead of this class's otherwise-universal `ShoreProtocolError`
— and `ShoreChannel._serve` (`agent/shore_transport.py:276`) only catches
`ShoreProtocolError` around `channel.handle()`, so an uncaught decode error
would tear down the entire authenticated host transport for every device on
that connection, the exact failure mode the surrounding code is designed to
prevent. Fixed with a new `_parse_capabilities()` helper used by both call
sites, raising `shore_untrusted_device`; covered by
`test_corrupted_capabilities_column_raises_stable_protocol_error`
(malformed JSON, a non-list, a list with a non-string element, and `null`).
(2) The migrated-in `DEFAULT` clause interpolated a JSON string directly into
the `ALTER TABLE` DDL without escaping; harmless today since
`DEFAULT_CAPABILITIES` has no quote characters, but the very next capability
name containing one would corrupt or break the DDL, since SQLite DDL can't
take bound parameters. Fixed by doubling embedded single quotes (the
standard SQL string-literal escape) before interpolating. (3) The reviewer
re-raised `ADR0040_BROWSER_TO_HOST_TYPES`'s hand-mirrored-with-no-shared-
source status from the first pass; still deferred to 4.8 as noted above, not
a new finding.

The same pass separately flagged that `_handle_auth_input`, `_handle_auth_
resize`, and `_handle_auth_cancel` (`agent/server.py`) still carried an
unused `websocket: WebSocket` parameter after 4.1's stated cleanup dropped it
from `_realtime_send`/`_realtime_catchup` and "updated all ~32 call sites" —
these three were missed. Not part of this milestone's scope, but a one-line-
per-function, mechanical completion of already-landed 4.1 work being
published in the same diff, so fixed here rather than left inconsistent:
parameter dropped from all three signatures and their three call sites in
`realtime_v1`. `tests/test_realtime.py`'s existing `auth.input`/`auth.resize`/
`auth.cancel` coverage (9 cases) passes unchanged, confirming no behavior
change.

Full `test_shore_*`, `test_realtime.py` (including all `auth.*` cases), and
`test_server.py` re-verified after all of the above (one pre-existing,
unrelated flake carried over from the first pass's note — same
`test_lifecycle_start_backgrounds_server`/real-`tailscale`-on-`PATH` cause).

A third pre-publish review (a multi-angle pass across 7 independent finder
agents) surfaced one genuine doc/implementation contract mismatch and two
latent fail-open gaps in the registry framework itself, plus re-raised the
same `ADR0040_BROWSER_TO_HOST_TYPES` deferral a third time (still deferred,
no new information). (1) `docs/shore-protocol-v1.md` — the actual normative
cross-language wire contract, not this plan doc — still stated
`dashboard.read.v1`'s authorized scopes as "global lifecycle feed; explicit
Flow-step resources already visible to the local dashboard principal," while
`_authorize_dashboard_read_scope` (confirmed above, Open question 2) only
ever authorizes the exact global scope. A separately-implemented Shore
client reading that table as the wire contract could reasonably expect a
Flow-step-scoped `subscribe` to work; fixed by updating the table row and
adding a sentence noting the narrower grant is deliberate, with widening it
a future protocol-doc amendment rather than an implementation detail. (2)
`ShoreCapability.types` and `.payload_schemas` are two independently
declared collections with nothing checking they name the same message
types; a future capability listing a type in one but not the other would
raise a bare `KeyError` out of `authorize_capability_frame` instead of this
module's `ShoreProtocolError` contract — the same failure class as the
second pass's `json.loads` finding, just in a different spot, dormant only
because there's exactly one capability registered today and it happens to
keep the two in sync by hand. Fixed with a `ShoreCapability.__post_init__`
that raises `ValueError` at construction time (i.e. at import time for the
real registry, immediately for any test-constructed capability) if the two
don't match exactly. (3) The scope-authorization step was gated on a literal
`message_type == "subscribe"` string and skipped entirely — not denied,
skipped — whenever `capability.authorize_scope` was `None`; a future
capability that lists a scoped type but forgets to wire an authorizer would
silently forward the device's requested scopes into the returned frame
unauthorized, exactly the fail-*open* behavior the module's stated
"fail-closed by construction" design goal exists to prevent. Fixed by
driving the check off the schema itself (any type whose payload schema
declares a `scopes` field, not a hardcoded type name) and raising
`shore_capability_denied` when such a type has no authorizer, instead of
silently passing the check. Both (2) and (3) are dormant today — one
capability, one scoped type, both correctly configured — so neither changes
current behavior; they only change what happens when 4.3/4.4 adds a second
capability incorrectly. Covered by
`test_capability_construction_rejects_types_payload_schemas_mismatch` and
`test_a_scoped_type_with_no_authorizer_fails_closed` (the latter
monkeypatches a deliberately-misconfigured capability into the registry,
since the real one can't be reconfigured to reproduce the gap); both
verified to fail without their respective fixes before being accepted.

Full `test_shore_capabilities.py`/`test_shore_crypto.py`/`test_shore_
transport.py`/`test_shore.py`/`test_realtime.py` re-verified (201 tests via
`-k "shore or realtime or capabilit"`), all passing.

**Design decisions made while implementing:**

- **Open question 2 resolved: confirmed.** `_authorize_dashboard_read_scope`
  accepts only a non-empty list where every entry equals exactly
  `{"lifecycle": "global"}`; any topic/agent-scoped entry, mixed list, or
  empty list is rejected. This is now settled, not just the strict reading
  called out in the original plan below — 4.3/4.4 can rely on it.
- **`client_id` dropped from `subscribe`'s payload schema: confirmed.** Direct
  `/ws/v1` requires it to derive `principal = f"local:{client_id}"`; Shore's
  4.4 plan instead derives `principal = f"shore:{device_id}"` from the
  already-authenticated envelope, so a Shore `subscribe` payload only needs
  `scopes` (optional, defaults to `[]`) and `cursor` (optional). 4.4 should
  build identity plumbing against this.
- **New module dependency direction:** `shore_capabilities.py` imports
  `ShoreProtocolError` from `shore_crypto.py`; `shore_crypto.py` needs
  `shore_capabilities.DEFAULT_CAPABILITIES` for `TrustedDevice`'s field
  default, which would cycle at module-import time. Resolved with a lazy
  import inside a `_default_capabilities()` factory function (only called at
  construction time, after both modules have finished loading), mirroring the
  existing lazy-import pattern noted in 4.1 for the `server.py`/
  `shore_transport.py` cycle.
- **`shore_unsupported_type` vs. `shore_capability_denied` are two distinct
  checks**, not one as the bullet list below might read in isolation: a
  message type outside the full known ADR-0040 browser-to-host type universe
  (`ADR0040_BROWSER_TO_HOST_TYPES`, e.g. a browser sending `hello`, or any
  synthetic future type) gets `shore_unsupported_type`; a real ADR-0040 type
  just not present in any of the device's granted capabilities (e.g.
  `chat.start`) gets `shore_capability_denied`. Both fail closed with no
  side effect either way; only the error code differs.

**Original plan (for reference):**

- **Objective:** make "deny unlisted fields, commands, scopes, and protocol
  versions before dispatch" fail-closed by construction.
- **Files:** new `agent/shore_capabilities.py`; `agent/shore_crypto.py`
  (`DeviceTrustStore._connect`, `agent/shore_crypto.py:361`).
- **Actions:**
  - Encode the registry from `docs/shore-protocol-v1.md` as data: a
    `ShoreCapability` dataclass (name, allowed protocol versions, allowed
    browser-to-host types, per-type strict closed-field payload schemas, a
    scope-authorizer callback) and a `SHORE_CAPABILITIES` map seeded with
    `dashboard.read.v1`.
  - Add a `capabilities` column to the `shore_devices` table, defaulting
    newly paired devices to `["dashboard.read.v1"]`, so a future
    `shell.exec.v1` grant is additive, not a retrofit.
  - Write `_authorize_dashboard_read_scope` as a stricter, Shore-specific
    sibling of `_authorize_realtime_scopes` — per the registry text, accept
    only `{"lifecycle": "global"}` and reject topic/agent-scoped requests
    (confirm this reading — open question 2).
  - Dispatch order, per the protocol doc's stated sequence: envelope
    crypto/replay (already implemented) → closed-schema check on the
    plaintext frame (unknown top-level fields, unsupported `v`, before
    inspecting `type`) → capability/type lookup (`shore_capability_denied` if
    absent) → strict per-type payload schema (deliberately stricter than
    direct `/ws/v1`, which tolerates unknown optional fields) → only then the
    4.1 shared core.
  - Structure the lookup as an allowlist intersection, never a denylist, so a
    future ADR-0040 type isn't reachable through Shore without an explicit
    registry entry.
- **Tests:** every non-listed type, every non-global scope, every unsupported
  version, every frame with an extra field is rejected with no observable
  side effect (assert via spy that the 4.1 shared core was never invoked).

#### 4.3 — Host-side Shore adapter: per-device sessions and a push-capable transport loop

- **Objective:** implement Actions 1 and 3 given one host socket must
  multiplex many paired devices.
- **Files:** `agent/shore_transport.py` (`ShoreChannel`, `ShoreHostConnection`).
- **Actions:**
  - Give `ShoreChannel` a `dict[device_id, RealtimeConnectionState]`, created
    on first authorized `subscribe`, cleared on `unsubscribe`, revocation, or
    key-epoch change (mirrors existing epoch-scoped trust invalidation in
    `_handle_envelope`, `agent/shore_transport.py:95`).
  - Make `ShoreChannel.handle` async so it can dispatch into the 4.1 core's
    asyncio-touching internals; update its one caller in
    `ShoreHostConnection._serve` (`agent/shore_transport.py:277`) — benchmark
    before dropping the current `asyncio.to_thread` wrap, since the crypto
    open/seal calls are CPU-bound even if currently cheap (open question 4).
  - Add a `notify_task` in `ShoreHostConnection._serve`, alongside the
    existing receive/heartbeat-timeout tasks, that wakes on the realtime
    notifier's generation change, computes each subscribed device's
    replay/rollover via the 4.1 core, and pushes a sealed `host_to_browser`
    envelope per device. New logic — no existing precedent for host-initiated
    Shore traffic beyond the transport lease heartbeat.
  - Since the host can't force-close one device's socket (it's shared), a
    slow/offline device gets an application-level `slow_consumer` error frame
    and has its local subscription cleared until it resubscribes; a device
    missing two `ping`/`pong` intervals is treated as no-longer-live locally,
    rather than the base protocol's WS-close semantics.
  - **These per-device overflow/heartbeat behaviors are new protocol surface
    not described in `docs/shore-protocol-v1.md` and need a protocol-doc
    amendment and sign-off before coding** (open question 1).
- **Tests:** a subscribed device receives a proactively pushed event with no
  further inbound frame; two concurrent device sessions get independently
  correct, non-interleaved cursors; overflow on one device doesn't affect
  another; revocation/key-epoch rotation clears only the affected state; a
  host-socket reconnect drops in-memory per-device state and a fresh
  `subscribe` with the last cursor resumes with no gap or duplicate (reusing
  `tests/test_realtime.py`'s reconnect/replay assertions through the Shore
  path).

#### 4.4 — Identity plumbing

- **Objective:** get Shore's caller identity into the shared 4.1 core safely.
- **Files:** `agent/shore_transport.py`, the 4.1 shared core.
- **Actions:**
  - `principal` is already just an opaque idempotency/authorization key
    (`local:{client_id}` today, `agent/server.py:4505`); construct
    `principal = f"shore:{device_id}"` inside `ShoreChannel` (device_id is a
    UUIDv7, globally unique per device), namespace-isolated from local
    principals.
  - No mutation types are in `dashboard.read.v1` yet, so no idempotency-store
    writes happen this milestone — wire the shape now to avoid a second
    refactor when mutations are added.
  - Pass `_authorize_dashboard_read_scope` (4.2) into the shared 4.1 core as
    an injected parameter; no shared handler code should special-case Shore
    inline, or the two transports can silently diverge over time.
- **Tests:** idempotency key isolation between `local:x` and `shore:y`
  principals sharing a coincidental `request_id`.

#### 4.5 — Browser client: from single-shot request/response to a duplex dashboard session

- **Objective:** give the browser something that can consume
  `dashboard.read.v1` end to end.
- **Files:** `shore/browser/src/client.ts`; new
  `shore/browser/src/dashboard-session.ts`.
- **Actions:**
  - Don't extend `exchange()` (single-slot `this.pending`) — it's
    fundamentally one-shot. Add a `subscribe()`-shaped API that seals a
    `subscribe` envelope and treats every subsequent inbound envelope as
    either a reply to a still-pending request or an unsolicited push routed
    to a callback.
  - Auto-reply to inbound `ping` with `pong`; send `ack` on an interval.
  - On `slow_consumer`, drop local state and resubscribe from the last
    applied cursor (reuse the existing direct-path reconnect logic in
    `ui/app.js` rather than a second algorithm).
  - Add jittered exponential backoff for the browser's own socket to the
    broker, analogous to `ShoreHostConnection.run` (`agent/shore_transport.py:183`).
- **Tests:** the most important new test extends
  `shore/browser/test/cross-process.test.ts` (real Python host fixture vs.
  real TypeScript client) with a subscribe → snapshot → live-published-event
  scenario, proving cross-language interoperability of the new push path
  through real encryption — check whether
  `shore/browser/test/fixtures/shore_host_process.py` already supports a
  "publish an event now" command before assuming it does.
- **Open dependency:** confirm with product whether a minimal dashboard UI is
  in scope for this milestone or deferred (open question 3) — materially
  changes this step's scope.

#### 4.6 — Preserve IDs/idempotency/cursors/acks/replay/heartbeat/backpressure across Shore's own reconnects

- **Objective:** verify Shore's own reconnect/backoff layer doesn't lose or
  duplicate ADR-0040 state.
- **Actions:**
  - Host-side reconnect (host↔broker) drops in-memory per-device state by
    design (4.3); devices detect this via heartbeat/close handling and
    resubscribe.
  - Browser-side reconnect must persist the last-applied cursor client-side
    and resend it on the next `subscribe`, addressed by the stable per-device
    identity already in IndexedDB.
  - No idempotency de-duplication needed yet (no mutations enabled) — note
    the boundary explicitly so the next milestone doesn't rediscover it.
  - Confirm the existing durable `ReplayStore` sequence counters (per
    account/host/key-epoch/device/direction) don't race or duplicate against
    4.3's new, more frequent `host_to_browser` pushes.
- **Tests:** starve a device of `subscribe` across several published events
  and confirm the next `subscribe` produces a complete, correct snapshot or
  replay — the highest-likelihood reviewer finding here is silent event loss
  during that gap.

#### 4.7 — Transport-parity test harness

- **Objective:** satisfy "identical scenarios over `/ws/v1` and Shore produce
  equivalent normalized state."
- **Files:** new `tests/test_shore_realtime_parity.py`.
- **Actions:** run one fixed scenario (subscribe → snapshot → N published
  events across replayable types → ack) through both a direct `/ws/v1` test
  client and `ShoreChannel` in-process; normalize away transport-only fields;
  assert equivalence of snapshot content, event ordering, and — critically —
  *which* catch-up mode (replay vs. snapshot) was chosen on both sides, not
  just the final payload. Add a negative-parity case documenting Shore's
  stricter denial behavior (4.2) as intentional, not a bug.
- **Tests:** this step is the test; wire into CI as a required gate, matching
  how Milestone 3 made the cross-process pairing/probe test required.

#### 4.8 — Authorization/negative test suite

- **Objective:** satisfy "every non-allowlisted command/scope fails closed
  without side effects."
- **Actions/tests:** exhaustively enumerate —
  - every ADR-0040 type not in `dashboard.read.v1` (`chat.start`,
    `chat.cancel`, `auth.start`, `auth.input`, `auth.resize`, `auth.cancel`,
    `worktree.auto_resolve`, browser-sent `hello`, a synthetic future type) is
    rejected pre-dispatch with no call into the shared core or DB mutation;
  - every non-global scope shape is rejected;
  - unsupported/missing protocol version is rejected before capability
    lookup;
  - extra/unknown fields are rejected;
  - a revoked-or-wrong-epoch device with an otherwise-valid capability still
    fails at the identity check first (proves check ordering).
  - Confirm denial reasons don't leak more than the protocol doc already
    allows (error detail never distinguishes an unknown key from a bad
    signature), and that denied frames still count against the existing
    per-socket frame-rate limit in `shore/src/index.ts` so flooding denials
    can't become a side channel or rate-limit bypass.

#### 4.9 — Documentation

- Update this plan doc's Milestone 4 status and the ADR-0039 mermaid
  diagram's "not yet enabled" annotations as each slice lands, following the
  narration style used for Milestones 1–3.
- Amend `docs/shore-protocol-v1.md` for the 4.3 overflow/heartbeat design
  once resolved.
- Don't mark the acceptance gate complete until 4.7/4.8 pass and an
  independent security review finds no unresolved critical/high findings.

**Explicitly out of scope for this milestone:** all mutation types stay
disabled (each future one is a separately named, individually reviewed
capability per Action 4). Arbitrary shell (`shell.exec.v1`) is fully
deferred — templates to reuse when it's designed: `DeviceTrustStore`'s atomic
approve/revoke pattern, a fail-closed-by-default local enablement gate, and
ADR-0038's scoped-terminal execution semantics, plus a new
expiry/immediate-revocation surface. None of this should be built now.

**Open questions requiring a decision before implementation starts:**

1. **Per-device overflow/heartbeat semantics (4.3):** the base protocol
   describes WS-level closes, which don't map onto one socket multiplexing
   many device sessions. This plan proposes an application-level
   `slow_consumer`/ping-timeout equivalent — needs a protocol-doc amendment
   and sign-off, per ADR-0039's own rule that contract changes require an
   amendment and new test vectors.
2. **Scope granularity for `dashboard.read.v1`:** is the registry's "global
   lifecycle feed" reading correctly limited to `{"lifecycle": "global"}`
   only, denying topic/agent-scoped remote subscriptions that direct local
   access allows? Confirm before finalizing `_authorize_dashboard_read_scope`.
3. **Dashboard view scope — resolved partially:** pairing/approval UI is now
   explicit scope (4.0), since neither pairing initiation, local approval,
   nor a remote pairing surface exist today. Still open: whether the
   *dashboard view* itself (rendering pushed events once 4.1–4.3 land) ships
   in this milestone's UI work or as a thin follow-on once 4.0's pairing page
   exists — building both together gives an actual end-to-end demo, but
   confirm scope/timeline with product first.
4. **`asyncio.to_thread` removal (4.3):** confirm dropping
   `ShoreChannel.handle`'s thread offload doesn't reintroduce event-loop
   blocking from the CPU-bound crypto calls — benchmark, don't assume.
5. **Idempotency-key scoping across epoch bumps:** same device_id with a new
   key-epoch vs. revoke-then-repair-as-new-device (new device_id) needs a
   decision before the mutation-enabling milestone, not during it.

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
