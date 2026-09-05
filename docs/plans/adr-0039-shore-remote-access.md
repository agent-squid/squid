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

### Implementation plan

Key findings that shape this plan: the capability list is already normative,
not something to design — `docs/shore-protocol-v1.md` §"Initial capability
registry" defines exactly one capability today, `dashboard.read.v1`
(browser-to-host types `subscribe`, `unsubscribe`, `ack`, `ping`, `pong`;
scope limited to the global lifecycle feed and Flow-step resources already
visible locally; no command or HTTP mutation). Nothing implements this
registry yet. There is also no existing capability/permission model to reuse:
`_authorize_realtime_scopes` (`agent/server.py:4048`) is documented as
authorizing "phase-one scopes for the fully trusted local Squid session" and
must not be reused verbatim for a remote, narrower-trust caller. The
`/ws/v1` handler (`realtime_v1`, `agent/server.py:4338`) is not currently
factored for reuse, but its underlying data-layer primitives already are:
`_realtime_snapshot`, `get_realtime_replay`, `_realtime_envelope`,
`get_realtime_cursor` operate on `scopes: list[dict]` and DB state with no
`WebSocket` reference. `ShoreChannel.handle` (`agent/shore_transport.py:72`)
is synchronous, stateless per call, and models only `shore.probe`'s
request/response shape — it cannot yet host a subscription that pushes
unsolicited events, and one host socket must multiplex per-device state for
every paired browser, not just one connection's worth. The browser client
(`shore/browser/src/client.ts`) similarly has only a single-slot
request/response primitive (`exchange()`) with no support for concurrent or
unsolicited traffic. No existing device-scoped, time-limited, revocable grant
flow exists yet for the later shell capability; the closest analogues are
`DeviceTrustStore.revoke()` (`agent/shore_crypto.py:412`) and ADR-0038's
scoped-terminal execution semantics.

**4.1 — Extract a transport-neutral ADR-0040 subscription core.** Pull the
subscribe/unsubscribe/ack/ping/pong/snapshot/replay/rollover logic out of
`realtime_v1` into pure functions operating on a small
`RealtimeConnectionState` (scopes, cursor, last-acked cursor, generation,
principal) plus an injected `authorize_scopes` callback, returning
`(new_state, outbound_frames)` with no `WebSocket` calls inside. `realtime_v1`
keeps its receive/backpressure/heartbeat loop and calls into the extracted
core; behavior for direct/Tailscale clients must be provably unchanged
against the existing `tests/test_realtime.py` suite. Deliberately do not move
`_handle_realtime_mutation`, `_realtime_chat_start`, `_realtime_auth_start`,
or the `_handle_auth_*` functions into the shared core — Shore's dispatcher
must be structurally unable to reach them in this milestone. Import the
shared core lazily from `agent/shore_transport.py`, mirroring the existing
lazy-import pattern used to avoid a `server.py`/`shore_transport.py` cycle
(`agent/server.py:392`). Add a narrow unit test exercising the extracted core
directly (given a state + a `subscribe` frame + an authorizer, assert
snapshot vs. replay vs. rollover selection) so Shore's own tests don't need a
live WebSocket. The central risk here is ending up with two independently
evolving realtime engines instead of one shared implementation — that would
defeat Action 1 entirely.

**4.2 — Define the Shore capability registry as an enforced data structure.**
Add `agent/shore_capabilities.py` encoding the registry from
`docs/shore-protocol-v1.md` as data: a `ShoreCapability` dataclass (name,
allowed protocol versions, allowed browser-to-host types, per-type strict
closed-field payload schemas, a scope-authorizer callback) and a
`SHORE_CAPABILITIES` registry seeded with `dashboard.read.v1`. Add a
`capabilities` column to the `shore_devices` table
(`agent/shore_crypto.py:361`, `DeviceTrustStore._connect`), defaulting newly
paired devices to `["dashboard.read.v1"]`, so a future `shell.exec.v1` grant
is an additive, explicit change rather than a retrofit. Write
`_authorize_dashboard_read_scope` as a stricter, Shore-specific sibling of
`_authorize_realtime_scopes` — per the registry text, it should accept only
`{"lifecycle": "global"}` and reject topic/agent-scoped subscription requests
outright (confirm this reading — see open questions). Dispatch order, per
`docs/shore-protocol-v1.md`'s stated validation sequence: envelope
crypto/replay (already implemented) → closed-schema check on the plaintext
frame, rejecting unknown top-level fields and unsupported `v` before even
inspecting `type` → capability/type lookup against the device's granted
capabilities (`shore_capability_denied` if absent) → strict per-type payload
schema (deliberately stricter than direct `/ws/v1`, which tolerates unknown
optional fields — Shore must not inherit that leniency) → only then call into
the 4.1 shared core. Structure the lookup as an allowlist intersection, never
a denylist, so a future ADR-0040 message type does not become reachable
through Shore without an explicit new registry entry. Tests must prove every
non-listed type, every non-global scope, every unsupported version, and every
frame with an extra field is rejected with no observable side effect (assert
via spy that the 4.1 shared core was never invoked).

**4.3 — Host-side Shore adapter: per-device sessions and a push-capable
transport loop.** Give `ShoreChannel` a `dict[device_id, RealtimeConnectionState]`,
created on first authorized `subscribe` from a device and cleared on
`unsubscribe`, revocation, or key-epoch change (mirroring the existing
epoch-scoped trust invalidation in `_handle_envelope`,
`agent/shore_transport.py:95`). Make `ShoreChannel.handle` async so it can
dispatch into the 4.1 core's asyncio-touching internals, and update its one
caller in `ShoreHostConnection._serve` (`agent/shore_transport.py:277`)
accordingly — benchmark before dropping the current `asyncio.to_thread` wrap,
since the crypto open/seal calls are CPU-bound even if currently cheap (open
question 4 below). Add a `notify_task` in `ShoreHostConnection._serve`,
alongside the existing receive/heartbeat-timeout tasks, that wakes on the
realtime notifier's generation change, computes each subscribed device's
replay/rollover via the 4.1 core, seals a `host_to_browser` envelope per
device, and sends it — this is new logic; there is no existing precedent for
host-initiated Shore traffic beyond the transport lease heartbeat. Because one
physical socket serves N logical device sessions, the host cannot force-close
a single misbehaving device's socket; a slow or offline device should instead
get an application-level `slow_consumer` error frame and have its local
subscription cleared until it resubscribes, and a device that misses two
`ping`/`pong` intervals should be treated as no-longer-live locally rather
than the base protocol's WS-close semantics. **These per-device
overflow/heartbeat behaviors are new protocol surface not described in
`docs/shore-protocol-v1.md` and need a protocol-doc amendment and sign-off
before coding**, following the amendment path already used during Milestone 3
(see open questions). Required tests: a subscribed device receives a
proactively pushed event with no further inbound frame; two concurrently
subscribed devices get independently correct, non-interleaved cursors;
overflow on one device doesn't affect another; revocation and key-epoch
rotation clear only the affected state; a host-socket reconnect drops
in-memory per-device state and a fresh browser `subscribe` with its last
cursor resumes correctly with no gap or duplicate (reusing
`tests/test_realtime.py`'s reconnect/replay assertions through the Shore
path).

**4.4 — Identity plumbing.** `principal` in ADR-0040 is already just an
opaque idempotency/authorization key (`local:{client_id}` today,
`agent/server.py:4478`); construct `principal = f"shore:{device_id}"` inside
`ShoreChannel` (device_id is a UUIDv7, globally unique per paired device) so
it's namespace-isolated from local principals. No mutation types are in
`dashboard.read.v1` yet, so no idempotency-store writes happen in this
milestone, but wiring the correct shape now avoids a second refactor when
mutations are added. `_authorize_dashboard_read_scope` (4.2) is passed into
the shared 4.1 core as an injected parameter — no shared handler code should
special-case Shore inline, since that's what would let the two transports
silently diverge over time.

**4.5 — Browser client: from single-shot request/response to a duplex
dashboard session.** Do not extend `exchange()` (`shore/browser/src/client.ts`,
single-slot `this.pending`) — it's fundamentally one-shot. Add a
`subscribe()`-shaped API (new `shore/browser/src/dashboard-session.ts`) that
seals a `subscribe` envelope and then treats every subsequent inbound
envelope as either a reply to a still-pending request or an unsolicited push
routed to a callback; auto-reply to inbound `ping` with `pong`; send `ack` on
an interval; react to `slow_consumer` by dropping local state and
resubscribing from the last applied cursor (reuse the existing direct-path
browser reconnect logic in `ui/app.js` rather than inventing a second
algorithm); add jittered exponential backoff for the browser's own socket to
the broker, analogous to `ShoreHostConnection.run`
(`agent/shore_transport.py:183`). The most important new test is extending
`shore/browser/test/cross-process.test.ts` (which drives a real Python host
fixture against the real TypeScript client) with a subscribe → snapshot →
live-published-event scenario, proving cross-language interoperability of the
new push path end-to-end through real encryption, not just the pre-existing
probe path — check whether `shore/browser/test/fixtures/shore_host_process.py`
already supports a "publish an event now" command before assuming it does.
Confirm with product/design whether a minimal dashboard UI is in scope for
this milestone or deferred (open question 3) — it materially changes this
step's scope.

**4.6 — Preserve request IDs/idempotency/cursors/acks/replay/heartbeat/backpressure
across Shore's own reconnect layers.** Host-side reconnect (host↔broker)
drops in-memory per-device state by design (4.3); devices detect this via
their own heartbeat/close handling and resubscribe. Browser-side reconnect
must persist the last-applied cursor client-side and resend it on the next
`subscribe`, addressed by the stable per-device identity already in
IndexedDB. No idempotency de-duplication is needed yet since no mutations are
enabled, but note the boundary explicitly so the next milestone doesn't have
to rediscover it. Confirm the existing durable `ReplayStore` sequence
counters (per `account_id`/`host_id`/`key_epoch`/`device_id`/`direction`)
don't race or duplicate against 4.3's new, more frequent
`host_to_browser` dashboard pushes. Write a test that starves a device of
`subscribe` across several published events and confirms the next
`subscribe` produces a complete, correct snapshot or replay — the highest-
likelihood reviewer finding here is silent event loss during that gap.

**4.7 — Transport-parity test harness.** New `tests/test_shore_realtime_parity.py`:
run one fixed scenario (subscribe → snapshot → N published events across the
replayable types → ack) through both a direct `/ws/v1` test client and
`ShoreChannel` in-process, normalize away transport-only fields, and assert
equivalence of snapshot content, event ordering, and — critically — *which*
catch-up mode (replay vs. snapshot) was chosen on both sides, not just the
final payload. Add a negative-parity case documenting that Shore's stricter
denial behavior (4.2) is an intentional divergence, not a bug. Wire this into
CI as a required gate, matching how Milestone 3 made the cross-process
pairing/probe test required.

**4.8 — Authorization/negative test suite.** Exhaustively enumerate: every
ADR-0040 type not in `dashboard.read.v1` (`chat.start`, `chat.cancel`,
`auth.start`, `auth.input`, `auth.resize`, `auth.cancel`,
`worktree.auto_resolve`, browser-sent `hello`, a synthetic future type) is
rejected pre-dispatch with no call into the shared core or any DB mutation;
every non-global scope shape is rejected; unsupported/missing protocol
version is rejected before capability lookup; extra/unknown fields are
rejected; a revoked-or-wrong-epoch device combined with an otherwise-valid
capability still fails at the identity check first (proving check ordering).
Confirm denial reasons don't leak more than `docs/shore-protocol-v1.md`
already allows (it states error detail never distinguishes an unknown key
from a bad signature) and that denied frames still count against the
existing per-socket frame-rate limit in `shore/src/index.ts` so flooding
denials can't become a side channel or a rate-limit bypass.

**4.9 — Documentation.** Update this plan doc's Milestone 4 status and the
ADR-0039 mermaid diagram's "not yet enabled" annotations as each slice lands,
following the narration style used for Milestones 1–3. Amend
`docs/shore-protocol-v1.md` for the 4.3 overflow/heartbeat design once
resolved. Do not mark this milestone's acceptance gate complete until 4.7/4.8
pass and an independent security review finds no unresolved critical/high
findings, matching the project's established pattern.

**Explicitly out of scope for this milestone:** all mutation types remain
disabled (each future one is a separately named, individually reviewed
capability per Action 4); arbitrary shell (`shell.exec.v1`) is fully deferred,
with `DeviceTrustStore`'s atomic approve/revoke pattern, a fail-closed-by-
default local enablement gate, and ADR-0038's scoped-terminal execution
semantics identified as the templates to reuse when it's designed, plus a new
expiry/immediate-revocation surface — none of this should be built now.

**Open questions requiring a decision before implementation starts:**

1. Per-device overflow/heartbeat semantics (4.3): the base protocol describes
   WS-level closes, which don't map onto one socket multiplexing many device
   sessions. This plan proposes an application-level `slow_consumer`/ping-
   timeout equivalent — needs a protocol-doc amendment and sign-off, per
   ADR-0039's own rule that contract changes require an amendment and new
   test vectors.
2. Scope granularity for `dashboard.read.v1`: is the registry's "global
   lifecycle feed" reading correctly limited to `{"lifecycle": "global"}`
   only, denying topic/agent-scoped remote subscriptions that direct local
   access allows? Confirm before finalizing `_authorize_dashboard_read_scope`.
3. Is a minimal read-only dashboard UI in scope for this milestone, or is it
   transport/authorization-only with UI deferred to a later milestone?
4. Confirm removing `ShoreChannel.handle`'s `asyncio.to_thread` offload (4.3)
   doesn't reintroduce event-loop blocking from the CPU-bound crypto calls —
   benchmark, don't assume.
5. Idempotency-key scoping across a device's key-epoch bump (same device_id,
   new epoch) versus revoke-then-repair-as-new-device (new device_id) needs a
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
