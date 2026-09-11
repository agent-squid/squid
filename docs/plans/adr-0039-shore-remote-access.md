# Plan: ADR-0039 Shore remote access

**Status:** In progress (2026-09-11). Milestones 0–4 are complete, each closed
by an independent security review with no unresolved critical/high findings
(Milestone 3: 2026-09-04; Milestone 4: 2026-09-07) — see each milestone's own
**Status** line below for what those reviews, and the review rounds before
them, found and fixed. The production `agentsquid.ai/@*` route is declared in
`shore/wrangler.jsonc` and a manually triggered `deploy-production.yml`
workflow exists, gated by the `shore-prod` environment; no production
deployment has run. Milestone 5's implementation is complete; preproduction
deployment and live B2 upload/retry/retention-delete-rejection verification
against `shore-audit-dev` both landed on 2026-09-11 (see Milestone 5's status
and 5.12). Only Milestone 5's final independent security review remains
before the production job can be enabled and Milestone 6 can begin. External
users must not be admitted in production until that review closes.

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

**Status:** Complete (2026-09-02). The relay skeleton, identity index,
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
(`agent/shore_crypto.py`) and browser/relay (`shore/src/crypto.ts`) sides.
Action 2 (pairing) and the durable device-trust core in Action 3 are also
implemented on both sides, after a model-assisted review round found and the
team fixed, in order: the original pairing design could not fit the 128-bit
secret and 128-bit nonce into one 130-bit human code and required the browser
to know unverified host fingerprints before it could decrypt anything
(circular); the protocol was amended to a bootstrap-key three-packet
ceremony (`docs/decisions/0039-remote-access-via-shore-relay.md`'s
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
just starting a new ceremony. The relay now recognizes only the public outer
schema of browser-to-host pairing packets and, without inspecting ciphertext,
atomically caps packets per ceremony and distinct ceremonies per five-minute
window across the account, browser-device, and source-fingerprint identity
layers (`shore/src/index.ts`). Pairing rate records expire through the existing
alarm cleanup, and source fingerprints use keyed HMAC rather than reversible
plain hashes. A pre-publish review of this relay code found and fixed two
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
since the host — not the relay — is responsible for wire-format validity);
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
(`tests/test_shore_crypto.py`), plus relay per-ceremony, churn,
window-boundary, packet-schema, and TTL behavior, persistence/privacy
behavior, session revocation, refresh rotation, attachment races, and revoked
generation ordering (`shore/test/shore.test.ts`); host suite is 32/32 and
relay suite is 82/82, `tsc --noEmit` clean. The first runtime transport slice
is also present: Shore exposes `/relay` through the existing signed-host and
remote-browser-session attachment checks. Relay upgrades now carry the immutable
account ID learned during authenticated discovery and route directly to that
account object, which rechecks its durable current username; this removes the
global `IdentityIndex` from the live socket path and prevents arbitrary username
traffic from serializing all relay connections. The public `/test-relay` route
and its bootstrap bearer compatibility surface have been removed, while the
relay continues to relay binary bytes opaquely. On the
host, `agent/shore_transport.py` joins the pairing coordinator and durable
trust/replay stores to a fail-closed dispatcher. Its only post-pairing plaintext
operation is a harmless `shore.probe` round trip; all other message types are
rejected, so ADR-0040 commands remain disabled. Tests cover the real Durable
Object WebSocket path, pairing through the runtime dispatcher, trust/replay/
outbound-sequence persistence over dispatcher reconstruction, and rejection of
command-shaped input. Still open before the milestone gate can pass: the
host WebSocket connection core now obtains a fresh relay challenge, signs the
canonical connection proof, dispatches binary frames through `ShoreChannel`,
sends relay-consumed lease heartbeats, and reconnects with bounded exponential
backoff (`ShoreHostConnection` in `agent/shore_transport.py`). The relay now
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
disabled. A final review added strict relay-metadata validation before persistence
and prevented malformed `shore.identity_dir` configuration from aborting daemon
startup. The pre-publish review also restored the existing account-activation ID
source after catching an unintended adjacent edit, made the registration metadata's
account ID derive from the Durable Object identity rather than a forwarding header,
and made daemon shutdown prompt while surfacing unexpected connection-task failures.
The second pre-publish review normalized malformed persisted relay types into the
fail-closed configuration path and bound administrative login responses back to the
explicitly requested immutable account ID. A third review made Shore identity-path
validation shared by startup and the config editor, rejecting falsy non-mappings,
empty paths, and cwd-relative paths; it also made normal premature connection-task
termination visible instead of silent. A fourth review hardened relay URL parsing
against deferred invalid-port/IPv6 errors and whitespace, aligned persisted usernames
with the relay's reserved-name rules, and added regression coverage for those cases.
A fifth review centralized those invariants at the persistence boundary itself, so
future callers cannot bypass validation by invoking the atomic writer directly.
A sixth review moved canonical username and UUIDv7 validation ahead of identity
creation and network access, preventing CLI route-identifier injection when a
session bearer is supplied. A seventh review restricted plaintext relay URLs to
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
`tsc --noEmit` check is clean. Explicit host-side relay-injection coverage now
proves malformed plaintext and a cryptographically well-formed envelope from an
untrusted device both fail before application dispatch. The focused host suites
pass 84/84, the relay suite passes 85/85, and `tsc --noEmit` remains clean.
The deployment slice originally completed with a manual, serialized
pre-production deployment workflow protected by the `shore-dev`
GitHub environment, separate Cloudflare credentials, test/typecheck gates, and
an isolated `workers.dev` hostname and Durable Objects. The workflow refused to
deploy without a 256-bit hexadecimal `FINGERPRINT_KEY`, preventing an absent or
weak fingerprint-HMAC key. The secret was uploaded atomically by the gated deploy
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
`shore-prod` GitHub environment, was added. At gate closure that environment's
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and `FINGERPRINT_KEY` secrets
were configured, though it had no required-reviewer protection rule (an
accepted interim exception recorded in `docs/shore-security-operations.md`
pending a second contributor). No production deployment has been run;
triggering the workflow was a separate, explicit step outside this review.
Milestone 5.8 later removed every secret and variable from both GitHub
environments and disabled both deployment jobs while the replacement audit
design was built. Fresh least-privilege credentials have since been
restored (5.9). The preproduction job's disabled condition was removed and
it deployed cleanly to `dev.agentsquid.ai` on 2026-09-11 (Milestone 5, 5.12);
the production job's disabled condition remains until Milestone 5's final
security review closes.

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

**Status:** Complete (2026-09-07). Implementation and documentation (4.0–4.9)
were done first; the milestone's acceptance gate then required an independent
security review, which passed on 2026-09-07 with no unresolved critical or
high findings, closing the gate. 4.0 (pairing and
approval UI), 4.1 (transport-neutral
subscription core), 4.2 (capability registry), and 4.3 (host-side adapter,
including 4.4's identity plumbing) are landed. 4.5 (browser duplex client) is
now landed in full: its orchestration/crypto code and unit tests were already
in place, and the cross-process interop scenario its own plan called the most
important test — subscribe → snapshot → live host-pushed event, proven over
the real encrypted wire protocol rather than a duck-typed fake or either
side's own test suite in isolation — now runs in `test/cross-process.test.ts`
and passes. 4.6 (reconnect/idempotency/sequence durability) is also now
landed — it corrected a stale assumption in its own plan text (host↔relay
reconnect does not drop in-memory session state, contrary to what this
section originally said) and added the dormancy/backlog-replay and
sequence-durability coverage its acceptance criteria called for. 4.7
(transport-parity harness) is also now landed: `tests/test_shore_realtime_parity.py`
drives one fixed subscribe → snapshot → live-events → ack scenario through
both a direct `/ws/v1` `TestClient` and a `ShoreChannel` in-process against
the same shared `stats_db`, and proves byte-identical frames, matching
catch-up-mode selection (fresh-snapshot and replay-gap-rollover-to-snapshot
cases), and a documented negative-parity case (Shore's capability registry
denies `chat.cancel` pre-dispatch with no side effect, while the identical
command reaches real dispatch over `/ws/v1` — the intentional narrower
surface from 4.2, not a bug). 4.8 (authorization/negative test suite) is also
now landed: `tests/test_shore_authorization_negative.py` adds identity-vs-
capability check ordering (a revoked or wrong-epoch device fails closed even
for an otherwise fully-granted command) and a static, exhaustive proof that
every `ShoreProtocolError` call site across the Shore host stack passes only
a closed string literal, never per-request dynamic detail; the relay
frame-rate-limit bullet needed no new test, since `shore/src/index.ts`
already counts every relayed frame before any content inspection is even
possible (relayed content is E2E ciphertext). 4.9 (documentation) is also
landed: this plan doc and the ADR-0039 mermaid diagrams now narrate 4.0–4.8
as built, and the 4.3 overflow/heartbeat protocol-doc amendment turned out to
already be done (it landed in 4.3 itself, before that section's code was
written). A capability-gated `dashboard.read.v1` dispatch path exists end to
end (`subscribe`/`unsubscribe`/`ack`/`ping`/`pong`, snapshot/replay catch-up,
and proactive push); the browser client and host dispatch have now been
proven against each other for the read-only, single-device, single-push case
that gap called out, and both 4.7's transport-parity harness and 4.8's
negative-authorization suite pass. Per 4.9's own rule, the milestone's
acceptance gate required those tests to pass *and* an independent security
review to find no unresolved critical/high findings before it could be
marked complete — that review ran and passed on 2026-09-07, so the gate is
now closed. Enabling the production route to actually serve
opaque-relay-plus-probe-plus-read-only-dashboard traffic (rather than
opaque-relay-plus-probe only) is a separate, explicit deployment step outside
this milestone, not automatic from the gate closing.

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

**Status:** Landed, together with 4.4's identity plumbing (there was no
natural seam to land them separately once real dispatch existed). Open
question 1 is resolved: `docs/shore-protocol-v1.md` gained a "Per-device push
liveness and backpressure" section defining the application-level
`slow_consumer`/ping-timeout equivalent this slice needed, amended before
this code was written, per ADR-0039's own contract-change rule.

`ShoreChannel` gained `sessions: dict[device_id, _DeviceSession]` (scopes,
cursor, last-acked cursor, last-ping/last-inbound monotonic timestamps),
cleared on `unsubscribe`, `revoke_device`, key-epoch mismatch, overflow, or
ping-timeout — not the sketched `RealtimeConnectionState` (that type was never
built; 4.1 deliberately deferred it, see 4.1's own note), so this uses a
small module-local dataclass instead. `ShoreChannel.handle` is now `async def`
and returns `list[bytes]` rather than one `bytes | None`, since a `subscribe`
can produce `subscribed` plus a snapshot or several replayed events; its one
caller, `ShoreHostConnection._serve`, is updated to send each. The
`shore.probe`/`shore.probe.result` echo the real browser client
(`shore/browser/src/client.ts`) still sends is checked first and stays
byte-for-byte unchanged — it was Milestone-3 scaffolding for the channel
itself, but the real client also uses it as a connectivity probe today, so it
could not simply be retired the way this section originally implied. Every
other decrypted frame goes through `authorize_capability_frame` (4.2), then a
new `_dispatch_adr0040` implements `subscribe`/`unsubscribe`/`ack`/`ping`/
`pong` against the 4.1 shared core (`_realtime_catchup`/`_realtime_snapshot`),
constructing `principal = f"shore:{device_id}"` inline (4.4's only real
action, folded in here).

**Open question 4 (`asyncio.to_thread` removal): resolved conservatively, not
benchmarked.** Rather than assume the CPU-bound crypto (`open_envelope`,
`seal_envelope`/`_seal`, `PairingCoordinator.accept_packet`) is safe to run
directly on the event loop now that `handle` is async, each of those specific
calls is individually wrapped in `asyncio.to_thread` — the same offload
`handle` as a whole used to get, just scoped tighter so the surrounding async
orchestration (which must run on the loop to await the 4.1 core) isn't
dragged into a thread with it. This is a deliberately unproven-safe default,
not a benchmarked one; revisit if push/dispatch load ever makes the
per-frame thread-pool round trip itself the bottleneck.

`ShoreHostConnection._serve` gained a `notify_task` (waits on
`agent.server._realtime_notifier`, lazily imported to avoid the
`server.py`/`shore_transport.py` cycle, mirroring the existing pattern at
`agent/server.py:392`) and a periodic `_push_sweep` (every 5s, independent of
the notifier and of the 30s relay transport-lease heartbeat) rather than one
task per subscribed device — a device count-scaling concern the original
per-device-task sketch didn't address. `_push_sweep` iterates
`ShoreChannel.sessions`, and per device: evicts on key-epoch mismatch or
>40s since last inbound frame (any type); otherwise runs the 4.1 core's
catchup through a fresh `_RealtimeOutbound`, sending a `slow_consumer` error
and clearing the session on overflow, or sending due replayed/live events and
a ping every 20s. One device's overflow, eviction, or send never touches
another's session or aborts the sweep for the rest. `last_sent` (which gates
the 30s lease heartbeat) only advances on an actual send, not a no-op sweep —
an early draft of this got that backwards, which would have let a busy
sweep loop silently starve the required lease heartbeat; caught before
landing, not after.

**Tests:** `tests/test_shore_transport.py`'s existing pairing/probe/
epoch/revocation coverage is updated for the `async`/`list[bytes]` signature
(probe's negative case, previously "anything but `shore.probe` fails
`shore_unsupported_frame`", now demonstrates the registry's own
`shore_capability_denied`/`shore_unsupported_type` split instead, since
`subscribe` etc. are legitimately dispatchable now). New coverage: a real
`subscribe` dispatches through the capability registry into the shared core
and returns decryptable `subscribed`+`snapshot` frames; a subsequently
disallowed type still fails closed; `_push_sweep` delivers a newly published
event to a subscribed device with no further inbound frame (the core 4.3
acceptance criterion) and advances its cursor; overflow on one device sends
it `slow_consumer` and clears only its session while a second, caught-up
device on the same host socket is completely unaffected; ping-timeout evicts
a stale session. Not covered by a new test, and worth flagging rather than
implying otherwise: concurrent non-interleaved cursors under real parallel
load (the sweep processes devices sequentially in-process, so this is
believed but not load-tested), and a full host-socket-reconnect-then-resume
scenario through `ShoreHostConnection.run` end to end (the reconnect drops
`ShoreChannel.sessions` by construction — a fresh object per connection isn't
actually true; `sessions` lives on `ShoreChannel`, not per-connection, so a
reconnect that keeps the same `ShoreChannel` instance currently does *not*
drop in-memory session state the way 4.6 assumes it will — flagging this as a
real open item for 4.6 to resolve, not silently papering over it).

Full suite re-verified: `tests/test_shore_transport.py` 30/30,
`test_shore_*`/`test_realtime.py` 219/219 (`-k "shore or realtime"`, stable
test order), full `tests/` 735/735 aside from two pre-existing failures
already documented as unrelated machine-specific flakes in 4.1/4.2's own
notes above (`test_lifecycle_start_backgrounds_server`'s real-`tailscale`-on-
`PATH` dependency; `test_stats_db.py`'s pre-existing shadow-mode failure).

A pre-publish review caught two real bugs and one carried-forward efficiency
concern, before any of this had been reviewed at all (this whole slice landed
across several sessions without a review pass in between — a process gap
worth naming, not just the bugs it let through). (1) `ShoreHostConnection.
_serve` reassigned `notify_task` to a fresh task, then checked whether that
*new* task was in `done` on the very next line — always false, since `done`
was computed against the old task. This silently made the "push immediately
on realtime notification" path dead code; Shore pushes only ever happened on
the unrelated 5s periodic sweep, not the responsive path the design intended.
Fixed by capturing `notified = notify_task in done` before reassigning.
(2) `revoke_device` mutates `ShoreChannel.sessions` (a plain dict) but runs on
a worker thread (`agent/server.py`'s revoke endpoint calls it via
`asyncio.to_thread`), racing the event-loop thread's own dict access in
`_dispatch_adr0040`/`_push_sweep` — a session could be resurrected by a
same-device `subscribe` landing in the gap, contradicting this method's own
"must drop immediately" comment. Fixed with a `threading.Lock` guarding all
dict-level access (not per-session-field mutation, which never crosses
threads) through four new `_session_snapshot`/`_get_session`/
`_get_or_create_session`/`_drop_session` helpers, so the lock can't be
bypassed by a future direct `self.sessions[...]` access. (3) `_seal`
(`_next_sequence`) opens a fresh SQLite connection and re-runs `CREATE TABLE
IF NOT EXISTS` per sealed frame — pre-existing since Milestone 3's probe path,
but now called far more often (every pushed event, every 20s ping, per
device) than before. Not fixed here: a real fix means either a persistent
per-instance connection (which `asyncio.to_thread`'s multi-worker-thread
execution makes unsafe without its own locking, since sqlite3 connections
aren't safe to share across threads by default) or a dedicated writer task —
either is its own reviewed slice, not something to bolt on while responding
to an unrelated review pass on security-critical sequence-integrity code.
Deferred, tracked here explicitly rather than dropped. Full suite
re-verified after (1) and (2): `tests/test_shore_transport.py` 30/30,
`-k "shore or realtime"` 219/219.

#### 4.5 — Browser client: from single-shot request/response to a duplex dashboard session

**Status:** Landed in full, including the one test this section itself
called "the most important new test" (see below). Before any of this work
started, this slice's own prerequisite
check ("check whether `shore_host_process.py` already supports a 'publish an
event now' command before assuming it does") turned up a real regression:
4.3 made `ShoreChannel.handle` async and list-returning, and
`shore_host_process.py` still called it synchronously and treated the result
as a single `bytes | None` — the existing cross-process pairing/probe/rotation
test was silently broken (nobody had run the `shore` repo's own test suite
after 4.3 landed). Fixed first, independent of 4.5: `main()` is now `async def`
run via `asyncio.run`, and `channel.handle`'s result is treated as a list
(only its first entry is relayed, matching every existing scenario that never
produces more than one). `test/cross-process.test.ts` passes again.

**What landed:** `shore/browser/src/client.ts` gained `listenDashboard`/
`unlistenDashboard` (duplex inbound routing, mutually exclusive with the
`pending` single-slot `exchange()` used by `probe`/`pair`), `sendDashboard`
(seals and sends one `subscribe`/`unsubscribe`/`ack`/`ping`/`pong` frame,
fire-and-forget), and `openHostEnvelope` (the decrypt+replay-check half of
what `probe()` used to do inline, now reusable). `probe()` itself was
refactored to use the same new `resolveTrustedPeer`/`sealBrowserFrame` helpers
instead of duplicating that logic, with no behavior change (existing
`probe()`/`pair()` tests and the cross-process test pass unchanged). New
`shore/browser/src/dashboard-session.ts` adds `ShoreDashboardSession`, which
owns the connection lifecycle once started: subscribes on connect (resuming
from a persisted cursor or fresh), auto-replies to `ping`, sends `ack` on an
interval, drops the saved cursor and resubscribes fresh on the protocol's
`slow_consumer` error (per the per-device liveness section landed earlier
this milestone), and reconnects with jittered exponential backoff —
`baseBackoffMs`/`maxBackoffMs` are constructor-configurable the same way
`ShoreHostConnection.__init__` exposes `base_backoff`/`max_backoff`, so tests
don't need fake timers. `shore/browser/src/trust-store.ts` gained a
`dashboard_cursor` IndexedDB store (`getDashboardCursor`/`setDashboardCursor`/
`clearDashboardCursor`, DB version bumped 2→3), mirroring `ui/app.js`'s own
localStorage cursor persistence for the direct path.

**Deviation from the sketch above, similar in spirit to 4.1's:** the
duplex/crypto plumbing (`listenDashboard`, `sendDashboard`, `openHostEnvelope`)
lives on `ShoreBrowserClient` itself, not in `dashboard-session.ts` — that
file only holds `ShoreDashboardSession`'s orchestration (scopes, cursor,
backoff, ack timing). `client.ts`'s `socket`/`send`/`receive` are private to
that class; the crypto plumbing needs them the same way `probe()` already
did, so splitting it into a separate file would have meant either exposing
that private surface or duplicating it. `probe()`'s "reply to a still-pending
request" framing in the sketch above also doesn't apply as written:
`dashboard.read.v1` traffic is never request/response-paired the way
`exchange()` is (a `subscribe` can produce zero, one, or many inbound
envelopes with no fixed count) — `listenDashboard` instead routes every
inbound envelope unconditionally to the session, which itself decides what
each decrypted frame means.

**Tests:** `test/trust-store.test.ts` gained cursor persistence coverage
(unset → set → cleared, rejects negative/non-finite values, treats a
corrupted stored value as unset rather than throwing). New
`test/dashboard-session.test.ts` (8 cases) covers `ShoreDashboardSession`'s
own state machine — fresh vs. resumed subscribe, `onAvailable`/`onEvent`/
`onSnapshot` dispatch and cursor persistence, `ping`→`pong`, `slow_consumer`
recovery, reconnect-with-backoff, and `stop()`'s generation guard against a
frame already in flight when it's called — against a duck-typed fake
`ShoreBrowserClient` (this repo's own established pattern, e.g. the fake
`WebSocket` in `cross-process.test.ts`), not a fake host. That boundary is
deliberate, not a shortcut: this repo's own convention proves wire-crypto
correctness only through real Python-host interop
(`test/cross-process.test.ts`), never a hand-rolled TS-simulated host, since a
matching bug on both sides of a self-written fake would pass and prove
nothing — see the undone item below. `npx tsc --noEmit` clean; full `browser`
suite 48/49 (1 pre-existing cross-process skip when the env vars aren't set,
same as before); `test/cross-process.test.ts` 1/1 with them set; shore-root
`npm test` 91/91 (after `pairing-app`'s own one-time `npm run build`, needed
in any fresh checkout per 4.0's own note about build/test ordering — not a
regression).

A pre-publish review caught two real bugs, one real gap, and one carried-
forward efficiency concern — this was the first review pass on 4.5, same
process gap already named in 4.3's notes above. (1) `ShoreDashboardSession.
attempt()` only re-checked `stopped`/`generation` once, right after `client.
connect()` resolved, not again after the second await (`getDashboardCursor()`).
A `start()` immediately followed by `stop()` (e.g. mount/unmount, or a
StrictMode double-invoke) landing in that window let the resumed `attempt()`
call `listenDashboard`/`sendDashboard` against an already-closed client and
leak a `setInterval` that `stop()` had no further chance to clear, since it
had already run. Fixed by re-checking immediately before touching the client.
(2) `onEnvelope` cast the decrypted plaintext straight to an object and read
`.type` with no guard, unlike `probe()`'s explicit shape check for the
equivalent case — `openEnvelope`'s own canonical-JSON check accepts a literal
`null` (or any JSON scalar) as valid plaintext, so a host push decrypting to
`null` threw inside a fire-and-forget async callback with no `.catch`,
producing an unhandled rejection instead of the intended "drop and keep the
session alive." Fixed with an explicit `typeof opened !== "object"` guard.
Same pass separately caught the snapshot branch computing `Number(event_id ??
cursor ?? 0)` with no finiteness check, unlike the plain-event branch's
`typeof === "number"` guard — a non-numeric value would poison
`lastAppliedCursor` with `NaN` permanently (it only ever grows via
`Math.max`), which `sealEnvelope` then rejects on every subsequent `ack`,
silently disabling acking for the rest of the session. Fixed by gating
`markApplied` on `Number.isFinite(cursor)`. (3) `client.ts`'s new dashboard
surface (`listenDashboard`, `sendDashboard`, `openHostEnvelope`, the
`dashboardListener` branch in `receive()`) had zero coverage against the real
class — `dashboard-session.test.ts` only ever drives a duck-typed fake, and
the cross-process fixture doesn't exercise subscribe/push at all. Fixed with
four new `client.test.ts` cases that pin a real, freshly generated host
keypair via `pinHostTrust` and use `sealEnvelope`/`openEnvelope` directly to
play the host's role for real — genuine `ShoreEnvelope` bytes exercising
`client.ts`'s own routing/decrypt code, not a simulated host implementation
(that distinction matters: this still isn't a substitute for real
cross-process interop, which is the undone item below, only for "does
`client.ts`'s own plumbing work against authentic envelopes"). The same pass
also surfaced, while fixing this, that `exchange()` had no guard symmetric to
`listenDashboard`'s: a `probe()`/`pair()` call made while a dashboard listener
was active would still set `pending` and send, but `receive()` checks
`dashboardListener` first and would misroute the reply there, hanging the
`exchange()` promise until its 30s timeout instead of failing closed
immediately — fixed by rejecting with `shore_dashboard_active` up front in
`exchange()` too, covered by one of the four new cases. (4) Not fixed:
`resolveTrustedPeer()` re-reads identity/trust from IndexedDB and re-imports
both host public keys via WebCrypto on every single `sealBrowserFrame`/
`openHostEnvelope` call, including a signing-key import that
`sealBrowserFrame` never uses — under a high-frequency push stream this is
real, repeated, redundant IDB/crypto work per message. Deferred rather than
patched here: caching resolved keys safely needs explicit invalidation on a
host-key-change/rotation mid-session, and getting that wrong would mean using
a stale host key silently — exactly the kind of mistake this repo's own
"require local approval for key changes" design goal exists to prevent.
Belongs in its own reviewed slice, not bolted on while responding to an
unrelated review pass on security-critical crypto code. Full suite
re-verified after (1)-(3): `npx tsc --noEmit` clean, `browser` suite 52/53 (4
new `client.test.ts` cases, 1 pre-existing cross-process skip),
`test/cross-process.test.ts` 1/1, shore-root `npm test` 91/91.

**Closed: the subscribe → snapshot → live-published-event cross-process
scenario this section itself called out as the most important new test.**
It could not be added as a small extension the way the original plan
implied, because `shore_host_process.py`'s wire protocol with the JS harness
was strictly one stdout line per one stdin line (the old `nextLine()` in
`cross-process.test.ts` read exactly one line per `send()`), and a
spontaneous host-initiated push (the whole point of 4.3) arrives with no
corresponding inbound line to pair it with. Landed concretely as: (1) the
fixture now holds open a real `ShoreHostConnection` and calls its actual
`_push_sweep` (reusing production code, not reimplementing the push logic in
the fixture) against a `stats_db` it owns (`stats_db._DB_PATH` pointed at the
fixture's own state dir), driven by a new `{"command": "publish", "text":
...}` stdin message that inserts one `run_events` row and sweeps; (2)
`_push_sweep`'s sends go to a `_PushSocket` that writes a distinctly-tagged
`{"push": ...}` line, while ordinary `channel.handle()` responses now emit
one `{"frame": ...}` line *per returned frame* (previously only
`responses[0]` was relayed, silently dropping `subscribe`'s second
`snapshot` frame — fixed as part of this slice, not a pre-existing correct
behavior); (3) `cross-process.test.ts` replaced the one-shot `nextLine()`
model with a persistent `HostLineDispatcher`: out-of-band control commands
(the initial pairing offer, `rotate`, `begin_approved_pairing`) still get
exactly one reply each via `nextControlLine()`, while every
`{"frame"/"push"/"error": ...}` line is routed straight to the fake socket's
`onmessage()`/`onerror()` on arrival regardless of what triggered it — this
one mechanism handles both multi-frame command responses and truly
unsolicited pushes with no special-casing between them. The existing
scenario was extended (not duplicated into a second `it()`, to avoid two
tests sharing one process-wide fake IndexedDB with divergent host keys) to
close the paired epoch-2 client, open a fresh one, drive a real
`ShoreDashboardSession` against it, await its `onSnapshot` callback
(`cursorReset === true`), issue `{"command": "publish", "text":
"cross-process-live"}`, and await `onEvent` delivering
`{type: "chat.text", payload: {text: "cross-process-live"}}` — the exact
`chat.text`/`{text: ...}` shape `stats_db.insert_run_event` produces for a
`"text"`-kind event, matching the existing Python-side push-sweep test's own
assertion. Verified: `browser`'s `tsc --noEmit` clean; full `browser` suite
53/53 with `SHORE_REQUIRE_CROSS_PROCESS=1`/`SQUID_SOURCE_ROOT` set (52/53,
1 skip, without); shore-root `npm test` 91/91 (after `pairing-app`'s own
one-time `npm run build`, the pre-existing fresh-checkout requirement noted
above); `pytest -k "shore or realtime"` 202 passed, including
`test_push_sweep_*`/`test_subscribe_dispatches_*` unchanged (17 unrelated
failures in this sandbox are a missing `websockets` pip package, needed only
by `ShoreHostConnection.run()`'s reconnect loop, which this fixture never
calls — no production code was touched by this slice, fixture- and
test-only). What this still doesn't prove, left to 4.7: multiple concurrent
devices, reconnect/resubscribe resuming from a persisted cursor, or that
Shore's behavior is *equivalent* to direct `/ws/v1`'s (only that it works in
isolation).

- **Open dependency, still unresolved:** confirm with product whether a
  minimal dashboard UI is in scope for this milestone or deferred (open
  question 3 above resolved this as a follow-on after 4.5, not bundled into
  4.0 — but no UI exists yet either way, and none was built in this slice).

#### 4.6 — Preserve IDs/idempotency/cursors/acks/replay/heartbeat/backpressure across Shore's own reconnects

**Status:** Landed.

**The plan's own first action bullet was wrong and is corrected here, not
carried forward.** It assumed "Host-side reconnect (host↔relay) drops
in-memory per-device state by design (4.3); devices detect this via
heartbeat/close handling and resubscribe" — mirroring how the direct `/ws/v1`
path's connection-scoped state works. That doesn't match what 4.3 actually
built: `ShoreHostConnection.__init__` receives one `ShoreChannel` and stores
it in `self.channel`; `run()`'s reconnect loop only ever constructs a new
`socket` per attempt and calls `self._serve(socket, stop)` again on the same
`self.channel` — so `channel.sessions` (each device's scopes, cursor, and
last-acked cursor) is untouched by a host↔relay reconnect. This was already
flagged as an unresolved open item in 4.3's own write-up above ("a reconnect
that keeps the same `ShoreChannel` instance currently does *not* drop
in-memory session state the way 4.6 assumes it will"). Resolved by keeping
the actual (better) behavior — no forced resubscribe/snapshot churn on a
transient host-side network blip, since any events published during the gap
are delivered as an ordinary replay by the first `_push_sweep` on the new
socket, the same as any other catch-up — and correcting this section's own
stated design to match, rather than changing working code to fit a stale
assumption. Proven directly by
`test_session_state_survives_host_relay_reconnect`
(`tests/test_shore_transport.py`): subscribes a device, sweeps an idle
socket (nothing to send), publishes two events with no sweep running (the
"host offline" gap), then sweeps a second, distinct fake socket standing in
for the post-reconnect connection — both events arrive on the second socket,
in order, with no frame ever sent on the first, and the device never
resends `subscribe`.

The other three action bullets held up as originally stated: browser-side
reconnect cursor persistence was already built in 4.5
(`shore/browser/src/trust-store.ts`'s `dashboard_cursor` IndexedDB store,
read/written by `ShoreDashboardSession`); idempotency de-duplication is still
correctly out of scope (no mutation types exist yet); and the durable
outbound sequence counter's race/duplicate safety is now proven through the
push path specifically (previously only exercised through the single-frame
probe/dispatch path in `test_live_channel_pairs_persists_trust_and_probe_round_trips`)
by `test_push_sweep_backlog_sequence_numbers_are_strictly_increasing_and_durable`:
a single `_push_sweep` call sealing five backlog frames back to back for one
device issues five strictly consecutive sequence numbers, and a `ShoreChannel`
rebuilt against the same `state_dir` (simulating a daemon restart) continues
from exactly where the durable `outbound.sqlite3` counter left off.

This section's own named acceptance test — starve a device of `subscribe`
across several published events and confirm the next `subscribe` produces a
complete, correct replay, since silent event loss during that gap was called
the highest-likelihood reviewer finding — is
`test_resubscribe_after_dormancy_replays_full_backlog_no_loss`: a fresh
subscribe snapshot's cursor is captured, three events publish with the
device dormant, and a resubscribe carrying that cursor (exactly what
`ShoreDashboardSession.attempt()` sends on its own reconnect) replays all
three, in order, as individually addressable `chat.text` events rather than
silently starting from a later point.

**Tests:** the three tests above, plus the full existing `test_shore_*` suite
unaffected. Verified: `pytest -k "shore or realtime"` 221/222 (33/33 in
`test_shore_transport.py`; the one unrelated failure,
`test_realtime.py::test_resize_touches_idle_timer`, is a pre-existing
test-ordering/event-loop-policy flake — it passes in isolation and this
slice touched no file outside `tests/test_shore_transport.py`). No
production code changed; this was a test-only slice plus the doc correction
above.

#### 4.7 — Transport-parity test harness

**Status:** Landed. `tests/test_shore_realtime_parity.py` pairs one Shore
device and opens one direct `/ws/v1` connection against the *same* shared
`stats_db` (not two independently seeded databases compared after the fact —
one event log, two transports, as in production), then asserts: (1) a fresh
subscribe on both sides produces byte-identical `subscribed`/`snapshot`
frames with `cursor_reset: true`; (2) the same two published events
(`chat.text`, `message.changed`) arrive as identical frames on both, and
`ack` is fire-and-forget on both with no reply; (3) a pruned/incontinuous
cursor forces the `replay_gap` rollover reason on both sides identically,
proving the *mode* (replay vs. snapshot), not just the final payload, is
shared; and (4) the required negative-parity case — `chat.cancel` denied by
Shore's capability registry pre-dispatch (`shore_capability_denied`, no
`_dispatch_adr0040` call, no DB mutation) while the identical command reaches
real dispatch and mutates state over `/ws/v1` — documenting 4.2's narrower
grant as intentional. No CI wiring needed: this repo has no pytest CI job at
all yet, so the file just runs wherever `pytest` already runs, same as every
other `tests/test_shore_*.py` file.

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

**Status:** Landed. `tests/test_shore_capabilities.py` already exhaustively
covered `authorize_capability_frame`'s own contract (every real ADR-0040
type outside `dashboard.read.v1`, every non-global scope shape, version
ordering, extra/unknown fields at both frame and payload level) before this
milestone started. New `tests/test_shore_authorization_negative.py` fills
what that unit-level module can't see on its own: a revoked device and a
wrong-key-epoch device each fail `ShoreChannel`'s identity check
(`shore_untrusted_device`) even when sending an otherwise fully-granted
`subscribe`, proving the identity check runs before capability authorization
rather than merely existing; and a static AST scan of every
`raise ShoreProtocolError(...)` call site across
`agent/shore_capabilities.py`, `agent/shore_transport.py`, and
`agent/shore_crypto.py` proves each passes a closed string literal (a bare
code, or a ternary between two fixed codes) and never dynamic per-request
detail — a structural guarantee, not a few example assertions, backed by a
concrete pair showing an identity-layer and a capability-layer denial are
equally opaque. The remaining bullet — denied frames still counting against
`shore/src/index.ts`'s per-socket frame-rate limit — needed no new test:
`webSocketMessage` increments `meta.rateCount` for every non-empty binary
frame before any role- or content-based branching, since relayed content is
E2E ciphertext the relay can't decrypt, so there is no code path where a
frame's eventual host-side authorization outcome could exempt it from that
counter; `test/shore.test.ts`'s existing `rate_limited` coverage already
exercises that same unconditional counter.

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

**Status:** Landed, including the gate. This plan doc's Milestone 4
status (below) and its 4.7/4.8 subsections narrate each slice as it landed,
matching Milestones 1–3's style. `docs/decisions/0039-remote-access-via-
shore-relay.md`'s system-flow mermaid diagram now reflects reality: step 4
of the "System and protocol flow" diagram reads "probe + read-only
dashboard.read.v1 (mutations still disabled)" instead of "probe" only, and
leg B of the "Target steady-state operation" diagram is relabeled
"IMPLEMENTED (read-only, Milestone 4)" — leg C stays "NOT YET ENABLED" since
`dashboard.read.v1` grants no mutation type, and leg A stays "NOT YET
ENABLED" since it's unbuilt and unscheduled. The 4.3 overflow/heartbeat
amendment to `docs/shore-protocol-v1.md` (its "Per-device push liveness and
backpressure" section) was already done in 4.3 itself, before that section's
code was written, per open question 1's resolution note above — nothing left
to amend there. The acceptance gate itself, which per this section's own
instruction stayed open until an independent security review also found no
unresolved critical/high findings, is now closed: that review ran and passed
on 2026-09-07.

- Update this plan doc's Milestone 4 status and the ADR-0039 mermaid
  diagram's "not yet enabled" annotations as each slice lands, following the
  narration style used for Milestones 1–3.
- Amend `docs/shore-protocol-v1.md` for the 4.3 overflow/heartbeat design
  once resolved.
- Don't mark the acceptance gate complete until 4.7/4.8 pass and an
  independent security review finds no unresolved critical/high findings.
  (Done: both passed, review closed 2026-09-07.)

**Explicitly out of scope for this milestone:** all mutation types stay
disabled (each future one is a separately named, individually reviewed
capability per Action 4). Arbitrary shell (`shell.exec.v1`) is fully
deferred — templates to reuse when it's designed: `DeviceTrustStore`'s atomic
approve/revoke pattern, a fail-closed-by-default local enablement gate, and
ADR-0038's scoped-terminal execution semantics, plus a new
expiry/immediate-revocation surface. None of this should be built now.

**Open questions requiring a decision before implementation starts:**

1. **Resolved (2026-09-06).** Per-device overflow/heartbeat semantics (4.3):
   the base protocol describes WS-level closes, which don't map onto one
   socket multiplexing many device sessions. `docs/shore-protocol-v1.md`
   gained a "Per-device push liveness and backpressure" section defining an
   application-level `slow_consumer`/ping-timeout equivalent — amended before
   4.3's code was written, per ADR-0039's own rule that contract changes
   require an amendment (this one reuses the base protocol's existing test
   vectors' values, 20s/2 missed intervals, rather than introducing new ones,
   since only the enforcement mechanism differs, not the timing).
2. **Resolved.** Scope granularity for `dashboard.read.v1`: the registry's
   "global lifecycle feed" reading is confirmed correctly limited to
   `{"lifecycle": "global"}` only, denying topic/agent-scoped remote
   subscriptions that direct local access allows — settled in 4.2's own
   write-up above (`_authorize_dashboard_read_scope`'s behavior is fixed and
   4.3 was built against it). This entry previously read "still open" after
   already being resolved elsewhere in this doc; corrected here rather than
   left inconsistent.
3. **Resolved (2026-09-06): ships as a follow-on, not bundled into 4.0.**
   Dashboard view scope: pairing/approval UI is 4.0's explicit scope; the
   *dashboard view* itself (rendering pushed events now that 4.1–4.3 exist)
   is deferred to 4.5 as a separate slice once the browser client is
   subscribe-capable, rather than folded into 4.0's pairing UI. This is a
   scope call, not a technical one — flag if product timeline actually needs
   the combined end-to-end demo sooner than 4.5's place in this sequence.
4. **Resolved conservatively (2026-09-06), not benchmarked.**
   `asyncio.to_thread` removal (4.3): `ShoreChannel.handle` is now `async def`
   so it can call into the 4.1 shared core, but each CPU-bound crypto call
   inside it (`open_envelope`, `_seal`/`seal_envelope`,
   `PairingCoordinator.accept_packet`) is individually still wrapped in
   `asyncio.to_thread`, rather than assuming the now-inlined dispatch logic
   around them is cheap enough to share the loop safely. No benchmark was
   run; this default should be revisited under real push/dispatch load
   before being treated as validated.
5. **Idempotency-key scoping across epoch bumps:** same device_id with a new
   key-epoch vs. revoke-then-repair-as-new-device (new device_id) needs a
   decision before the mutation-enabling milestone, not during it — still
   correctly deferred, since `dashboard.read.v1` has no mutation types and
   thus no idempotency-store writes yet (4.4).

## Milestone 5 — Correlated tamper-evident audit

**Status:** Implementation complete; preproduction deployed and B2-verified
(2026-09-11, see 5.12); final security review pending. Milestones 3 and 4 are both complete, including
Milestone 4's acceptance gate, which closed on 2026-09-07 (see Milestone 4's
status), unblocking this work. 5.0 (relay-side hash-chained audit log) is
landed: every existing account-lifecycle audit event (magic links, sessions,
second-factor, recovery, deletion, host registration/revocation,
displacement) is chained via a monotonic per-account `seq` plus a
`prevHash`/`hash` pair recomputed from each event's own canonicalized
content, with the running chain tip held in its own storage key so a
rewritten final event can't silently re-anchor itself; it also migrates any
pre-chain legacy audit records the first time a new event is logged, so no
account is left with corrupted history. 5.1 (host-side signed audit log,
Action 2) is also landed: `agent/shore_audit.py`'s `ShoreAuditLog` records a
locally Ed25519-signed, hash-chained event for every ADR-0040 frame the host
dispatches (granted or capability-denied), correlated to the relay's chain
by the shared envelope `request_id`. Action 1's scope turned out to need a
correction discovered while implementing 5.1 — see 5.1's write-up below.
Action 3's host-side durable batching foundation and former B2 transport landed
as 5.2a/5.2b; the relay's own B2 export (5.2c, `shore/src/index.ts`'s
`Account.exportAuditBatch`/`auditExportLagMs`) is also now landed, uploading
each account's chain to `relay/accounts/<accountId>/events/...` with the same
create-only, cursor-gated design as the host writer, plus the same five-minute
export-lag alert on sustained failure. The former cross-stream daily manifest
can correlate relay and host events because 5.3 records every valid
opaque relayed envelope before forwarding, including its public request,
host, device, and session identifiers, direction, ciphertext commitment, and
forwarding outcome. The signed manifest core landed in 5.4, but the target
architecture now retires both it and the unbuilt collector. Because Shore has
not been released and there is no production audit history to migrate, 5.8
removes these superseded paths and their obsolete credentials before replacement
work begins. Host B2 credentials and direct host-to-B2 export are retired;
signed host batches instead flow
over the authenticated Shore channel and Shore alone writes B2. Live signed
relay receipts replace daily comparison and fail closed for remote access only.
5.5 separately lands host-authenticated, live read access
to the relay's own audit chain for a single account -- groundwork for
Action 4, not the 5.4 collector. Action 4 (user-visible history/notifications)
has its backend already in place (relay notification delivery, step-up-
protected host revoke), 5.6 lands a browser client that can actually receive a
live notification, and 5.7 lands a security-history page
(host/alerts/notifications/devices/sessions) at `/@<username>/security`, also
exposing displacement alerts through `/auth/security` for the first time. The
step-up-protected revoke-host action is now wired into that page. Healthy
same-key displacement atomically creates a correlated audit record, alert, and
durable notification, and the page receives live notification control frames
over its authenticated browser socket and refreshes with bounded reconnect.

**Objective:** make account, pairing, capability, and command activity
attributable without storing command plaintext.

**Actions:**

1. Relay records account/device/session IDs, source metadata, receipt time,
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
   to the relay audit event. Raw IP and precise location remain restricted to
   the audit system and are never copied into browser/out-of-band notifications.

**Acceptance:** tests detect deletion, insertion, mutation, receipt-chain gaps,
regression/conflict, missing correlation, and forged host events; receipt
failure disables Shore remote dispatch without affecting local/direct access;
no AgentSquid host configuration contains B2 credentials; a healthy same-key displacement produces
both the correlated audit record and first user notification while stale
reconnect does not alert; batching preserves and later surfaces every repeated
event; revocation is step-up protected and atomically invalidates the host
connection, browser sessions, pairings, and capabilities; retention and
redaction tests show command text, secrets, raw IP, precise location, and full
headers are absent from user notifications by default.

### Implementation plan

**Key findings**

- Action 1's scope splits cleanly in two: chaining the *existing* relay audit
  log (account/session/pairing/host lifecycle events, already recorded via
  `Account.audit()`/`auditEntry()` in `shore/src/index.ts`) needed no new
  infrastructure decision and is a direct prerequisite for everything else in
  this milestone. Extending that log to cover every relayed *command* frame
  is a separate, real cost decision (a storage write per relayed frame), now
  explicitly accepted and landed in 5.3 so receipts/checkpoints can detect missing
  relay/host correlations.
- Action 3's archive target is not an open decision — it remains
  Milestone 0's accepted spec (`docs/shore-security-operations.md`, echoed in
  this doc's own Milestone 0 section): private, SSE-B2-encrypted, Object-Lock
  Backblaze B2 buckets in an account separate from Cloudflare
  (`shore-audit-prod` / `shore-audit-dev`), a write-only bucket-scoped
  application key with no read/delete/retention-management/legal-hold/
  governance-bypass capability, 400-day Compliance retention in production
  (enabled before external users are admitted) and 1-day Compliance retention
  in test and a 5-minute export-lag paging threshold. B2 credentials now live
  only in Shore; live signed receipts and host checkpoints replace daily
  manifests. That doc also fixes the per-event field
  schema for both chains (relay: prior hash, event ID, account/host/device/
  session IDs, coarse source metadata, restricted raw IP, receipt time,
  ciphertext hash, outcome; host: signed request ID, plaintext command hash,
  authorization decision, result class, host time, prior host-event hash) and
  the explicit exclusion list (no command/response text, secrets, cookies,
  auth headers, internal addresses, precise location, full headers). 5.0's
  `Audit` type now carries the correlation-critical request, host, device,
  session, direction, ciphertext-commitment, and outcome fields. Coarse source
  metadata and restricted raw-IP archival remain separate privacy work.
- Durable Object storage transactions are the right place to chain events:
  reading the prior chain tip and writing the new one inside the same
  `storage.transaction()` callback that already writes each audit record
  keeps the chain atomic under the runtime's optimistic-concurrency retries,
  with no separate locking needed.

**5.0 — Hash-chain the relay's existing audit log (landed)**

- `Audit` gained `seq`, `prevHash`, and `hash`; `auditEntry()` now reads the
  `audit-chain-tip` key inside the caller's transaction, computes
  `hash = sha256(jcs({seq, prevHash, ...event fields}))`, and writes the new
  tip alongside the event so both land atomically. All ~20 existing call
  sites were updated to pass the active transaction.
- Added `verifyAuditChain()` (exported from `shore/src/index.ts`), which
  replays a full event list and recomputes each hash, flagging
  `sequence_gap` (deletion), `sequence_fork` (a duplicated/out-of-order seq,
  including two internally-consistent divergent branches), `hash_mismatch`
  (content mutated without a matching hash), and `chain_break` (a
  prevHash/tip that no longer lines up, including an insertion that can't
  chain from the true prior hash, or a rewritten tip event whose separately-
  stored tip pointer was left stale). Exposed at `GET /internal/audit/verify`
  (DO-internal only, not on the public `/@username/...` route surface, like
  `/internal/state`).
- `shore/test/shore.test.ts`'s new "Milestone 5 tamper-evident audit" suite
  proves detection of all four tamper classes above by mutating DO storage
  directly, plus a clean-chain case. This is intentionally a same-storage
  self-consistency check: an attacker or bug with direct storage write access
  who correctly recomputes an entire alternate history from genesis is not
  detectable by hash-chaining alone — that residual gap is exactly what
  Action 3's Shore-only archive and Action 2's independent host-signed record
  are for. "Missing correlation" and "forged host
  events" from this milestone's acceptance criteria are cross-checks against
  Action 2's host-side log, which doesn't exist yet.
- An independent review (codex) caught two issues in the first pass, both now
  fixed and covered by tests: (1) accounts with pre-chain `audit:<timestamp>:...`
  history from before this landed would have corrupted `verifyAuditChain`'s
  replay and `/internal/state`'s key-sort ordering the moment a new event was
  logged, since `auditEntry()` started a fresh chain at seq 1 and ignored the
  older unchained records under the same `audit:` prefix — fixed with
  `migrateLegacyAuditChain()`, which rewrites any pre-chain records in place
  (oldest first, by their existing chronologically-sortable keys) into the new
  scheme the first time `auditEntry()` finds no `audit-chain-tip`, so every
  account converges on one uniform chained log with no ops-run migration step;
  (2) `auditEntry()`'s `data` parameter was `Partial<Audit>` spread before the
  trusted fields, so a future caller could have overridden `id`/`type`/`at`/
  `seq`/`prevHash` or injected a `hash` — narrowed to
  `Pick<Audit, "connectionId" | "correlationId">` and spread first so the
  trusted fields set afterward always win.

**5.1 — Host-side signed audit log, and a correction to Action 1's scope (landed)**

- **Key finding that changed Action 1's plan:** the relay cannot tell ADR-0040
  message types apart at all. `shore/src/index.ts`'s `webSocketMessage` never
  decrypts a relayed frame — `type`/`payload` live inside the AEAD ciphertext,
  opaque to the relay by design (E2E, per this doc's non-negotiable
  invariants). So "audit only subscribe/unsubscribe, skip ping/pong" — the
  scope floated before this sub-step — isn't implementable at the relay: it
  is structurally blind to which relayed frame is which. The relay's only
  per-frame options are "audit every relayed frame identically" (the real
  cost question from before, now confirmed to mean literally every frame,
  heartbeats included, not just a rare subset) or "don't add per-frame
  relay auditing yet." Given `dashboard.read.v1` has no mutating commands
  at all today (Milestone 4: `subscribe`/`unsubscribe`/`ack`/`ping`/`pong`
  only), the per-frame relay cost/value tradeoff stays deferred rather than
  decided by default — Action 1's connection-level events (`socket_attached`,
  displacement, stale reconnect, already in 5.0) remain the relay's audit
  coverage for now. The host, by contrast, decrypts every frame and already
  runs a fail-closed capability check on each one (`agent/shore_capabilities.py`'s
  `authorize_capability_frame`) — so Action 2 (the host chain) had real,
  current content to audit today, unlike a relay-side per-frame log, and
  became the higher-value next step.
- Added `agent/shore_audit.py`'s `ShoreAuditLog`: a local SQLite-backed chain
  mirroring 5.0's design (monotonic `seq`, `prevHash`/`hash` over each
  event's own canonicalized content, computed inside the same `BEGIN
  IMMEDIATE` transaction that reads the prior tip) plus an Ed25519 signature
  over each event from the host's own pinned identity key — a guarantee the
  relay's chain doesn't have, since only the host holds that private key.
  Records: `requestId` (correlates to the relay's chain), `deviceId`,
  `hostId`, `messageType` (the closed ADR-0040 type tag, not payload
  content), `commandHash` (a commitment to the full decrypted frame, never
  the frame itself), `decision` (granted/denied), `outcome`, and `at`.
  `verify_chain()` replays the log and flags `sequence_gap`, `sequence_fork`,
  `hash_mismatch`, `chain_break` (as in 5.0) plus `bad_signature`. Because
  `seq` is the SQLite primary key, a literal duplicate-seq fork can't even be
  written through the store's own `record()` path (stronger than 5.0's
  KV-backed chain) — `tests/test_shore_audit.py`'s fork test exercises
  `verify_chain()` directly with a hand-built pair of events for that reason,
  documented inline.
- Wired into `agent/shore_transport.py`'s `ShoreChannel._handle_envelope`:
  every ADR-0040 frame (not `shore.probe`, which predates capability
  dispatch and isn't a real command) is audited, after
  `authorize_capability_frame` either returns or raises. A capability denial
  records `decision="denied"` best-effort (the command is already failing,
  so a queuing failure there doesn't need to escalate further) and re-raises
  the original error unchanged.
- Not done here: actually cross-checking the two chains against each other
  (matching a relay `request_id` to a host `request_id`) — that requires
  Action 3's export pipeline to get both chains into one place to compare,
  so "missing correlation" and "forged host events" from this milestone's
  acceptance criteria were still open at the time 5.1 landed, until Action 3's
  export pipeline existed. Now resolved by 5.11: every archived host event's
  request ID must resolve to the durable relay receipt allocated for that
  immutable host before archival, so a signed batch cannot invent relay
  correlation records.
- An independent review (codex) caught three issues in the first pass, all
  now fixed and covered by tests: (1) High — "fail closed" was checked
  *after* `_dispatch_adr0040` already ran, so a subscription/session
  mutation could take effect before an audit-durability failure raised
  `shore_audit_unavailable`, and an unexpected (non-`ShoreProtocolError`)
  dispatch exception produced no audit record at all — fixed by recording a
  `decision="granted", outcome="pending"` event *before* dispatch runs (fail
  closed here blocks dispatch entirely, since `record()` only ever appends,
  never rewrites), then a second, separately verifiable `outcome` event
  after dispatch succeeds or raises (best-effort on that second write, since
  the command already ran and the durable "pending" record already proves
  what was authorized). (2) High — `verify_chain()` only checked linkage
  between the rows it was handed, so deleting the last row, or every row,
  of an otherwise-consistent chain verified as clean — fixed by adding a
  separately-signed single-row `audit_tip` checkpoint, updated atomically
  with every `record()`, that `verify()` checks the replayed chain's actual
  final event against; `tests/test_shore_audit.py` adds final-event and
  whole-table deletion tests, both now caught as `chain_break`. (3) Medium —
  events carried no key epoch or signer identity, and `verify()` took one
  fixed key for the whole chain, so a host key rotation reusing the same
  `audit.sqlite3` (a new `ShoreChannel` with a new `host_signing` key but
  the same `state_dir`) would sign new events onto the old chain in a way
  neither the old nor the new key alone could verify — fixed by recording
  `keyEpoch` per event and per tip, and changing `verify_chain()`/`verify()`
  to take a `keys: Mapping[epoch, PublicKey]` pinned-key history instead of
  one key, failing closed as `unknown_key_epoch` for any event whose epoch
  isn't pinned rather than silently skipping or misattributing it. Note this
  reuses the existing ADR-0040 `key_epoch` concept rather than inventing a
  parallel one — the protocol as implemented today never actually increments
  it for a live host (only a full host replacement gets a new `host_id`), so
  this is currently latent correctness, not something reachable through the
  deployed registration flow yet; it's still real because nothing in
  `ShoreAuditLog` itself enforced that constraint, and the fix costs little.
  Verified: the full Shore suite (181 tests, up from 177) and the full
  Python suite (759 passing) both green; the two failures elsewhere
  (`test_lifecycle_start_backgrounds_server`,
  `test_init_db_marks_pre_activation_flow_runs_as_shadow`) are confirmed
  pre-existing on the unmodified backing repo, unrelated to Shore.

**Architecture update — live receipts and Shore-only archival (accepted; implementation pending)**

Current repository state as of 2026-09-10:

| Capability | State in code | Target action |
| --- | --- | --- |
| Host signed SQLite chain, atomic signed tip, deterministic batches | Landed in `agent/shore_audit.py` | Keep and reuse |
| Direct host-to-B2 exporter and `SQUID_SHORE_AUDIT_B2_*` startup wiring | Removed in 5.8 | Do not reintroduce |
| Relay per-account audit chain and B2 exporter | Landed in `shore/src/index.ts` | Keep; extend to host batches |
| Authenticated relay audit challenge/events endpoint | Landed in `shore/src/index.ts` | Reuse for bounded catch-up from an already trusted tip |
| Daily manifest builder/verifier | Removed in 5.8; no operational collector existed | Do not reintroduce |
| Dedicated per-host receipt chain and relay signing key | Not implemented | Build |
| Outer relay frame and receipt verification/persistence | Not implemented | Build in Shore and host transport |
| Host-batch WebSocket ingestion and signed archive acknowledgement | Not implemented | Build |
| Continuity-loss recovery/reinstall UX | Not implemented | Build before enforcing fail-closed behavior |

- Host B2 configuration and direct host-to-B2 transport were removed in 5.8.
  `SQUID_SHORE_AUDIT_B2_*` is not part of the supported host
  configuration.
- Reuse the deterministic, host-signed batches from 5.2a, but send them as a
  bounded protocol control message over the authenticated host WebSocket.
  Shore verifies the pinned host key and chain continuity before archiving the
  opaque batch under an account/host-specific prefix with its own B2 key.
- Wrap each relayed application envelope in a versioned outer frame containing
  a Shore-signed receipt: envelope commitment/request ID, disposition, prior
  relay tip, and new relay tip. The receipt does not modify the E2E envelope.
  Hosts pin the relay audit public key and persist the highest verified tip.
- Missing, invalid, regressed, or conflicting receipts close the Shore channel
  and block remote dispatch until explicit user-authorized recovery or
  re-pairing. Local/direct access is outside this gate.
- The daily collector, manifest implementation, GitHub credentials, and
  separate manifest authority were removed in 5.8.
- This detects operational faults and observable equivocation in real time but
  does not independently prove honesty after complete Shore compromise. An
  independent witness is a separate higher-assurance option.

**5.2a — Host export batches (landed; transport target revised above)**

- `ShoreAuditLog.pending_export()` emits bounded canonical-JSON batches with
  the complete signed host events plus a separately signed manifest anchoring
  the range, prior/head hashes, event count, payload hash, host/key epoch, and
  final event time. Object names derive only from sequence coordinates and the
  head commitment, contain no user data, and both name and body are stable
  across retries for safe create-only uploads.
- The SQLite `audit_export_state` cursor advances only through the explicit
  `mark_exported()` acknowledgement after an uploader succeeds. Failed or
  interrupted uploads leave the same rows pending; acknowledgements are
  checked against the local chain and cannot move the cursor backward.
- This batching format remains useful, but its transport target is now Shore's
  authenticated host channel. Direct host-to-B2 delivery is superseded.

**5.2b — Host B2 transport and retry loop (landed, now superseded for removal)**

- Added a dependency-free AWS Signature V4 `PutObject` client for Backblaze's
  HTTPS S3-compatible endpoint. It signs the payload and all relevant headers,
  explicitly requests SSE-B2 (`AES256`), performs no list/read/delete or bucket
  operation, and obtains its endpoint, bucket, region, key ID, and application
  key exclusively from `SQUID_SHORE_AUDIT_B2_*` environment variables.
- The daemon starts the exporter only when all archive variables are present.
  It drains deterministic batches in order, acknowledges each only after a
  successful upload, retries transient failures without advancing the cursor,
  and logs an error once the oldest pending event exceeds the specified
  five-minute lag threshold. Shutdown cancels and awaits the exporter with the
  other lifespan-owned tasks.
- Backblaze documents HTTPS path-style S3 endpoints, Signature V4, `PutObject`,
  SSE-B2, and bucket-default Object Lock retention. Its documented `PutObject`
  headers do not include `If-None-Match`, so the writer does not pretend B2
  offers AWS conditional-create semantics: retry-stable names/bodies may create
  another immutable version after an ambiguous success. Bucket Object Lock and
  a credential lacking delete/retention-management capability provide the
  append-only boundary; live test-bucket verification remains required.

**5.2c — Relay B2 export and lag alert (landed; Action 3 remains in progress)**

- `shore/src/index.ts`'s `Account.exportAuditBatch()` uploads each account's
  hash-chained events to `relay/accounts/<accountId>/events/<fromSeq>-
  <throughSeq>-<headHash>.json` via the existing `createAuditArchiveRequest`
  Signature V4 `PutObject` builder, mirroring the host writer's create-only,
  cursor-gated design: the `audit-export-cursor` storage key only advances
  after a successful upload, and a sequence-gap or chain-break against the
  local log throws rather than exporting a torn batch. `auditEntry()` schedules
  the DO alarm about a second after any new event when B2 is configured, so
  new activity drains promptly instead of waiting for an unrelated timer.
- Closed a completeness gap versus the host writer while reviewing this: the
  relay's `alarm()` caught export failures with a flat `console.error` and
  retried every five seconds forever, with no way to distinguish "still
  within normal retry" from the five-minute export-lag paging threshold
  Milestone 0's accepted spec requires (`docs/shore-security-operations.md`)
  and which the host side already implements
  (`agent/shore_audit_export.py`'s `MAX_EXPORT_LAG_MS`). Added
  `auditExportLagMs()` (age of the oldest un-exported event) and had `alarm()`
  log a distinct "exceeds five minutes" message once that threshold is
  crossed, so a stuck exporter pages instead of scrolling past in ordinary
  retry logs.
- This landed with no direct test coverage of `exportAuditBatch`/the alarm
  integration at all — only the pure `createAuditArchiveRequest` builder had a
  test. Added three: a successful upload that advances the cursor to the
  chain tip and asserts the uploaded manifest's shape, a failed upload that
  leaves the cursor untouched so a retry resends the same batch, and an
  `alarm()` case proving the five-minute escalation fires only once the
  oldest pending event is actually that stale, not on the first retry
  (`test/shore.test.ts`, "Milestone 5 tamper-evident audit" describe block).
  These patch `env`/`globalThis.fetch` on the already-constructed DO instance
  rather than the file-wide miniflare bindings, since the latter would make
  every other test's `runDurableObjectAlarm` call attempt a real network
  fetch (almost all of them generate audit events). Full suite verified:
  102/102 (was 98/98 before these tests existed; the file's one other
  known-environmental failure, the static pairing page 404 when
  `pairing-app/dist` hasn't been built locally, is unaffected and matches
  4.0's documented CI build-order requirement), `tsc --noEmit` clean.
- Still open: live retention/overwrite/deletion-rejection verification against
  the real `shore-audit-dev` bucket and ingestion of host-signed batches.

**5.3 — Relay per-envelope correlation records (landed)**

- The relay records each valid ordinary encrypted envelope before forwarding
  it. The chained event contains only public routing metadata (`requestId`,
  host/device/session IDs, and direction), a SHA-256 ciphertext commitment,
  and a pre-forward `pending` outcome. A second best-effort event records the
  actual `forwarded`, `peer_offline`, `backpressure`, or `send_failed` result;
  command and response plaintext remain opaque.
- Audit persistence is fail-closed: if the relay cannot append the event, it
  drops that frame without forwarding it. The WebSocket remains open so one
  transient storage failure does not disconnect every multiplexed device or
  amplify load through reconnect/resnapshot churn, matching the host-side
  frame-local failure boundary. Transport heartbeats and pairing packets remain
  outside this path.
- Relay coverage verifies targeted forwarding, actual backpressure outcomes,
  and the redacted correlation record. TypeScript typecheck and 103 applicable
  tests pass; the single
  excluded static-assets test requires the documented pairing-app build step.

**5.4 — Signed cross-stream manifest core (landed, now retired)**

- `agent/shore_audit_manifest.py` verifies the relay hash chain and host
  signed chain against independently supplied tips, correlates relay request
  IDs whose outcome confirms `forwarded` with host decisions/outcomes, surfaces
  missing IDs on either side, and signs the daily heads/counts/gap report with
  a separate Ed25519 manifest authority. Offline, backpressured, and failed-send
  frames are correctly excluded because they never reached the host.
- Manifest verification rejects content mutation, while construction refuses
  a broken relay chain or forged host event. Each manifest commits to the
  prior signed manifest and refuses a relay or host head that regresses, so a
  valid historical prefix cannot be substituted for the latest anchored
  history. Creating or verifying the first manifest requires an explicit
  genesis declaration; every later manifest requires its prior signed
  manifest, preventing an omitted predecessor from silently resetting the
  manifest chain. The host now also records
  `shore.probe`, fail-closed, so valid relay inbound envelopes do not create
  systematic false correlation gaps.
- The operational collector is cancelled. Do not provision its B2 credentials
  or manifest authority. Live receipts and Shore-side ingestion of host-signed
  batches replace this design as specified in the architecture update above.

**5.5 — Host-authenticated read access to the relay's own live audit chain (landed)**

- Added `shore/src/index.ts`'s `GET /@<username>/host/audit-events` (paginated,
  `?cursor=`) and its prerequisite `POST /@<username>/host/audit-challenge`,
  gated by the same single-use, signed proof-of-possession the host already
  performs to attach its WebSocket (`verifySocketProof`, generalized to
  `verifyHostProof(request, host, purpose)` and reused with a new
  `"audit_read"` `HostChallenge` purpose so a challenge issued for one use
  can't authenticate the other). Returns this account's own `Audit` events
  plus the current chain tip; scoped to the requesting host's own account,
  never another's.
- This gives the host live, authenticated read access to the relay's version
  of its own account chain. It is useful groundwork for live receipt recovery
  and user-visible history, but is not itself the signed receipt protocol.
- Tests (`shore/test/shore.test.ts`, "Milestone 5 host audit-events
  endpoint"): a full round trip returning events and a matching tip, rejection
  with no proof, rejection of a proof signed against a `"websocket"`-purpose
  challenge, and single-use enforcement (a second request with the same
  challenge fails). Typecheck is clean; 107 applicable tests pass, with the
  known environment-dependent static pairing-assets test excluded.

**5.6 — Browser client recognizes relay security notifications (Action 4, first slice)**

- **Key finding:** the backend half of Action 4 already exists and was never
  reachable. The relay already builds and delivers `SecurityNotification`
  events (`deliverSecurityNotification` in `shore/src/index.ts`) for recovery,
  deletion, host revocation, and scheduled warnings, and `/@<username>/auth/
  revoke` already implements the step-up-protected immediate host-revoke
  action Action 4's text calls for. But `shore/browser/src/client.ts` had no
  code path for receiving them at all: the relay sends a notification as a
  plaintext WebSocket **text** frame (`ws.send(JSON.stringify(...))`, no
  binary framing), while every encrypted envelope is relayed as a **binary**
  frame (`socket.binaryType = "arraybuffer"`). `receive()` never checked
  which kind it had gotten -- in dashboard mode a notification would fail
  `decodeFrame`'s binary-only parsing and be silently dropped; outside
  dashboard mode (e.g. mid-`probe()`/`pair()`) it would incorrectly reject
  that pending exchange with a misleading `shore_invalid_frame`, since
  `receive()` unconditionally tried to treat the string as an encrypted
  reply.
- Fixed by checking `typeof data === "string"` first, before either the
  dashboard-listener or pending-exchange branch: a text frame is parsed and
  validated as `{type: "security_notification", notification: {id, type,
  at}}` and delivered to a new `onSecurityNotification(listener)` registration
  (independent of, and unaffected by, `listenDashboard`/`probe()`/`pair()`
  state); anything else on a text frame is dropped rather than corrupting
  either mode.
- Tests (`shore/browser/test/client.test.ts`, "security notifications"): a
  real notification reaches the listener; malformed/unrelated text frames are
  dropped without throwing; a notification during an active dashboard session
  reaches the listener without being misrouted into the envelope stream; a
  notification arriving mid-`probe()` reaches the listener without rejecting
  the still-pending exchange (this last case is the one that would have
  failed against the pre-fix code with `shore_invalid_frame`). Full browser
  suite (excluding the cross-process test, which needs a real host process):
  56/56 (was 52/52 before this slice), `tsc --noEmit` clean.
- Still open: this only makes notifications *receivable*. No UI renders them,
  no page exists for session/device/capability history at all (`shore/browser`
  remains a headless, build-free library; only `pair.html` exists as a real
  page, from Milestone 4's pairing-app), and the revoke-host button Action 4
  calls for has nothing to attach to yet. That page is comparable in scope to
  Milestone 4's `pairing-app` slice and is the next piece of Action 4, not
  done here.

**5.7 — Read-only security-history page (Action 4, second slice)**

- **Key finding, blocking assumption corrected:** `pairing-app/src/app.ts`
  already assumes a browser session exists before it does anything
  (`confirmPairing` just shows "Log in to agentsquid.ai... first" on a 401)
  -- there is genuinely no login/signup web page anywhere in this repo, and
  none is planned here: building one would duplicate or conflict with
  whatever the actual product site (a separate repo, outside this session's
  code roots) is responsible for. This page follows that same established
  precedent -- it assumes a session cookie already exists and shows the same
  kind of message if not, rather than attempting to build login itself.
- **Second finding:** `securityState()` (the DO method behind `/auth/
  security`) never returned `alert-incident:` records at all. Milestone 1's
  own acceptance criteria call healthy same-key host displacement a
  high-severity, user-notified event, but `healthy_same_key_displacement`
  never calls `notificationEntry`/`deliverSecurityNotification` (only
  `magic_link`/`recovery`/`deletion`/`host_revoked`-type events do) --
  displacement evidence lived only in `alert-incident:` storage with no read
  route at all before this. Fixed by adding an `alerts` array to
  `securityState()`'s response containing incident timing and counts while
  deliberately omitting its internal keyed-fingerprint samples. Covered by a
  new test proving a real displacement
  produces an alert visible through the authenticated `/internal/security`
  surface (not just the test-only `/internal/state` route the existing
  displacement tests already checked).
- Added `pairing-app/src/security.html` + `security.ts`: fetches `/@<username>/
  auth/security` with the same-origin session cookie (mirroring
  `loadAuthenticatedShoreRoute`'s fetch options, but returning the full state
  rather than the narrow `ShoreRoute` that function deliberately discards it
  down to) and renders host status, security alerts, notifications, paired
  browser devices, and active sessions as plain read-only lists. Pure
  formatting logic (`notificationLabel`, `formatRelativeTime`, `parseUsername`)
  lives in a dependency-free `security-link.ts`, mirroring `pairing-link.ts`'s
  existing split so it's testable without a DOM.
- `pairing-app/src/app.ts` renamed to `pair.ts` (mechanical; nothing outside
  the build config referenced its path) so `build.mjs`'s now-two-entry-point
  esbuild config can derive both output names (`pair-app.js`,
  `security-app.js`) from `entryNames: "[name]-app"` instead of one hardcoded
  `outfile`.
- `shore/src/index.ts`'s `parseShoreRoute` and static-asset serving branch
  extended to recognize `/security` and `/security-app.js` as siblings of
  `/pair`/`/pair-app.js`, same reasoning as 4.0 (assets binding, not a
  Durable Object; same-origin session scope). Both static UIs are served with
  a restrictive CSP, frame denial, MIME-sniffing protection, no-referrer, and
  no-store response headers.
- The revoke-host action now uses an inline, click-again confirmation and TOTP
  step-up, obtains a fresh same-origin CSRF token without exposing the HttpOnly
  session token, and calls the existing atomic `/auth/revoke` path. No system
  modal is used. Live updates use the existing authenticated native-browser
  relay: plaintext security-notification control frames trigger a no-store
  refresh, binary E2E application frames are ignored, and reconnect uses
  bounded exponential backoff. No independent security review of this page has
  happened (same caveat 4.0 recorded for the pairing page: pairing-code/
  session-detail-in-logs). Clickjacking is mitigated on both static pages by
  CSP `frame-ancestors 'none'` plus `X-Frame-Options: DENY`.
- Tests: `test/shore.test.ts` gained `parseShoreRoute`/static-serving coverage
  for `/security`/`/security-app.js` (mirroring `/pair`'s) and the
  `securityState` alerts-exposure test above; `pairing-app`'s new
  `security-link.test.ts` (11 cases) covers username parsing, notification
  labeling (including an unmapped-type fallback so a future type isn't hidden),
  and relative-time formatting (including negative/clock-skew and rounding
  edges). Full suites re-verified: shore worker 110/110 (was 108/108 before
  this slice), `tsc --noEmit` clean; `pairing-app` 24/24 (was 13/13), `tsc
  --noEmit` clean and `npm run build` produces both `pair-app.js` and
  `security-app.js`.

**5.8 — Remove the unreleased legacy audit design and credentials (complete
2026-09-11)**

- Shore has not been released, no
  production deployment has run, and no production audit history or supported
  host configuration depends on the direct-to-B2 or daily-manifest designs.
  Delete them before implementing their replacements.
- Remove the direct host-to-B2 exporter, its server startup/configuration
  wiring, and its tests. Preserve the signed host audit log and deterministic
  batching from 5.1/5.2a; those are inputs to Shore ingestion.
- Remove the daily manifest builder/verifier and its tests. No collector was
  deployed, so there is no collector state to migrate.
- Remove every currently configured Shore secret and variable from both GitHub
  deployment environments. Revoke/delete the corresponding Cloudflare API
  tokens and Backblaze application keys at their providers; deleting a GitHub
  secret alone is not revocation. Remove the B2 inputs from the deployment
  workflows during this cleanup. Provision fresh, least-privilege deployment,
  fingerprint, audit-signing, and Shore-only archive credentials only when the
  replacement design reaches its applicable deployment gate.
- Keep the relay-side B2 exporter and archive configuration: the replacement
  design still requires Shore alone to write both relay and verified
  host-signed streams to append-only storage. Removing the current credentials
  does not remove that target architecture; it ensures the replacement starts
  with newly issued, narrowly scoped credentials instead of inheriting the
  unreleased deployment's secret set.
- Landed by removing `agent/shore_audit_export.py`,
  `agent/shore_audit_manifest.py`, their tests, and the host startup wiring.
  Shore's workflows and Wrangler required-secret declarations no longer carry
  B2 inputs, and both deployment jobs fail closed through an explicit disabled
  condition until the replacement credential gate lands. All secrets and
  variables were removed from both GitHub deployment environments. On
  2026-09-11, the operator confirmed that the corresponding obsolete
  Cloudflare API tokens and Backblaze application keys had also been deleted
  at their providers, closing the final 5.8 gate.
- Acceptance: repository search finds no host B2 or manifest runtime path; host
  audit/batch tests still pass; Shore type checks and tests pass; both obsolete
  Cloudflare and B2 keys are confirmed revoked provider-side; and both GitHub
  environments contain no Shore secrets or variables.

**5.9 — Receipt protocol and relay chain (in progress; protocol and durable
allocation cores landed)**

- Define canonical receipt vectors and a dedicated monotonic chain per immutable
  `host_id`. Cover ordinary encrypted envelopes in both directions and exclude
  pairing packets and lease heartbeats. Allocate sequence/tip and the existing
  pre-forward audit record atomically in the account Durable Object.
- Add a Shore audit-signing key and pin its public key in AgentSquid releases.
  Define old-key-signed rotation and explicit recovery for loss of the old key.
- Return the receipt beside inbound E2E envelopes and as an acknowledgement for
  host-originated envelopes. Retrying the same request ID and envelope must
  return the same receipt rather than allocate a second entry.
- Landed first: the exact receipt schema, hashing/signing inputs, genesis,
  idempotency/conflict semantics, rotation/recovery requirements, normative
  domain-separated Ed25519 vectors, cross-language vector verification, and Shore
  receipt and old-key rotation signing/verification cores with closed-schema,
  mutation, nonconsecutive-epoch, and wrong-key tests. The Durable Object now
  also has host-scoped monotonic receipt allocation: receipt tip, stable
  `(host_id, request_id)` idempotency record, and the pre-forward relay audit
  event commit in one storage transaction. Byte-identical retries return the
  stored signed receipt without advancing either chain; changed bytes or an
  opposite direction fail with `shore_receipt_conflict`; separate immutable
  hosts retain independent chains. A retry also revalidates its stored
  receipt's closed schema, key fields, canonical encodings, hash, and signature
  against the pinned key for its recorded epoch before returning it, failing
  closed on corrupt durable state. New receipts are likewise verified before
  commit so a signing/public-key configuration mismatch cannot poison the
  chain. Tests cover allocation/signature validity, atomic audit
  linkage and rollback, stable retries, both conflict forms, corrupt stored
  records, and host scoping.
  The wire slice is also landed behind receipt-key configuration: exact browser
  envelope bytes are base64url-preserved in a canonical `relay_delivery`
  wrapper beside their receipt; host-originated envelopes retain their browser
  wire format and receive a canonical `relay_receipt_ack` on the host socket.
  Receipt acknowledgements use the same bounded socket queue as relayed
  payloads; if an acknowledgement is backpressured or cannot be queued, Shore
  records that outcome, closes the host socket, and does not forward the
  corresponding envelope so a reconnect can retry the stable receipt safely.
  The live relay invokes the allocator only when a complete, internally
  consistent signing-key epoch and pinned public-key history are configured;
  absent configuration preserves the pre-enforcement transport, while partial
  or mismatched configuration drops ordinary remote frames closed. Key
  coordinates must be canonical unpadded 32-byte base64url, and malformed JWK
  imports map to the same deterministic configuration failure. Pairing
  packets and lease heartbeats remain unchanged. Host-side verification and
  persistence landed in 5.10. The disabled deployment workflows now require
  and cryptographically validate the replacement receipt and Shore-only B2
  credential set before atomically uploading it. A publish-gate review on
  2026-09-11 corrected a trust-root flaw: non-loopback AgentSquid connections
  no longer accept receipt public keys from the process environment; only keys
  pinned in a reviewed AgentSquid release are trusted. Those release pins are
  scoped by canonical relay origin, keeping preproduction and production
  trust roots independent; after any release pins are populated, unknown
  non-loopback origins fail configuration closed. Loopback development may
  still use the environment override. Independent epoch-1 Ed25519 keys are now
  provisioned in the `shore-dev` and `shore-prod` GitHub environments and their
  public coordinates are release-pinned for `https://dev.agentsquid.ai` and
  `https://agentsquid.ai`, respectively. Independent write-only B2 keys are
  scoped to the existing `shore-audit-dev` and `shore-audit-prod` buckets;
  provider inspection verified private access, SSE-B2, Object Lock compliance
  retention of one and 400 days, and `writeFiles` as the keys' sole capability.
  The preproduction DNS/Worker route is provisioned. The preproduction
  deployment job's disabled gate was removed and `deploy-preproduction.yml`
  ran clean on 2026-09-11, shipping to `dev.agentsquid.ai`. The production job
  remains explicitly disabled pending the final security review.

**5.10 — Host verification and remote-only fail-closed gate (landed)**

- Verify signature, envelope commitment, host/epoch, sequence, and previous tip
  before application dispatch. Persist the receipt tip and pending host audit
  decision in one SQLite transaction; repeated identical receipts are
  idempotent.
- Support bounded catch-up only when signed receipts extend the locally trusted
  tip. Buffer/reject out-of-order delivery deterministically. Never adopt a
  current Shore tip when the local checkpoint is absent.
- On invalid, missing, regressed, or conflicting evidence, close Shore and block
  remote dispatch only. Surface `audit continuity unavailable` separately from
  `confirmed receipt conflict`; do not label either as proof of compromise.

Implemented in `agent/shore_receipt.py`, `agent/shore_audit.py`, and
`agent/shore_transport.py`: configured hosts unwrap only canonical receipt wire
frames, verify the pinned epoch key, exact envelope commitment, host/direction,
content hash, signature, sequence, and prior tip before dispatch. The inbound
tip and pending/denied host audit event share one `BEGIN IMMEDIATE` transaction;
identical retries do not dispatch twice. Outbound acknowledgements verify
against the exact sent bytes and advance the same chain. Missing wrappers,
unknown epochs, gaps, and absent local genesis close only the Shore socket as
`shore_audit_continuity_unavailable` (including a non-genesis sequence when no
local checkpoint exists); signed mutations, regressions, and forks
close it as `shore_receipt_conflict`. Direct/local transport is unchanged, and
receipt enforcement remains configuration-gated for the 5.12 rollout ceremony.

**5.11 — Shore ingestion of host-signed batches (landed; provider archive
verification remains part of the Milestone 5 gate)**

- Add a bounded host control message carrying 5.2a's stable signed batch. Verify
  the pinned host key, key epoch, batch payload, and extension from the last
  accepted host tip before storing or archiving it under an account/host prefix.
- Return a Shore-signed archive acknowledgement. Advance the host export cursor
  only after that acknowledgement verifies. Preserve retry-stable bodies and
  request IDs across disconnects and ambiguous acknowledgements.
- Implemented with canonical bounded `host_audit_batch` frames, closed-schema
  event and manifest validation, host signature/epoch/chain verification,
  durable per-host tips and retry records, account/host-scoped B2 objects, and
  Shore-signed acknowledgements. The host sends retry-stable 25-event batches
  and advances SQLite only after verifying the acknowledgement against its
  pinned Shore receipt keys. Review tightened canonical base64url/signature
  checks, rejected unexpected event fields, required the registered host epoch,
  and added acknowledgement backpressure handling. Live provider retention and
  overwrite/deletion rejection are still an external acceptance-gate check.
  The publish-gate review also made archival mandatory before acknowledgement:
  missing or failed B2 configuration now leaves the host cursor untouched, and
  the replacement deployment contract requires the complete Shore-only B2
  configuration rather than permitting finite Durable Object retention alone.
  Before archival, every distinct host event request ID must also resolve to
  the durable relay receipt allocated for that immutable host, so a signed
  batch cannot invent relay correlation records.

**5.12 — Recovery, migration, and enforcement (implementation complete;
preproduction deployed and B2-verified; final security review pending)**

- Treat missing/rolled-back host SQLite as lost continuity. Local/direct access
  stays available; Shore access requires a locally authorized recovery or
  re-pairing ceremony. A reinstall creates a new `host_id`; reuse of old keys
  without the checkpoint does not bypass recovery.
- Record old and new epochs/tips when known without pretending the new genesis
  extends lost history. Test crash boundaries, retries, concurrent devices,
  restored backups, Shore rollback, key rotation, and reinstall.
- Roll out receipt verification in observe-only mode first. After migration and
  recovery work is proven, enforce the gate. The superseded host-B2 and
  daily-manifest paths and credentials were already removed in 5.8; do not
  reintroduce either as a fallback.

The release pin activates enforcement only for the two named non-loopback
origins; loopback remains the observe/compatibility path used by local tests,
and any other remote origin fails configuration closed. Receipt persistence
rejects a non-genesis Shore tip when the local checkpoint is absent, rejects a
forward jump after a coherent older SQLite backup is restored, and retains the
per-receipt epoch history across a signing-key rotation. Reinstall continues to
create a new host identity and therefore a new relay chain rather than
silently adopting an old host's tip. Focused tests cover these cases together
with crash rollback, stable retries, reconnects, concurrent-device behavior,
unknown epochs, and direct/local-path availability.

Live preproduction acceptance (2026-09-11): `deploy-preproduction.yml`'s
disabled gate was removed and the workflow deployed cleanly to
`dev.agentsquid.ai` (Go tests/vet, browser/pairing-app/root typecheck+test+
audit, `wrangler deploy --env preproduction`, all green). Separately, direct
provider-API verification against the live `shore-audit-dev` bucket, using a
freshly created key scoped to that bucket only (`listFiles`/`readFiles`/
`writeFiles`/`deleteFiles`/`readFileRetentions`, no `bypassGovernance`,
1-hour expiry) confirmed: uploads succeed and are automatically placed under
Object Lock Compliance retention (one day) with no app-side action required;
retried identical uploads succeed without error; and `b2_delete_file_version`
against both resulting versions was rejected with `access_denied` while the
objects remained listed, proving deletion is refused even though the key
itself was granted `deleteFiles`. This also caught a spec/deployment mismatch:
`shore-audit-dev` runs Object Lock in Compliance mode, not Governance mode as
earlier drafts of this doc and `docs/shore-security-operations.md` stated
(now corrected) — Compliance is the strictly stronger mode, so this was a
documentation error, not an operational gap. This exercised B2's storage-layer
guarantees directly; it did not exercise Shore's own application-level batch
idempotency (5.2a/5.11's stable retry via `request_id`), which still requires
a real host/browser pairing session against the live deployment and remains
unexercised live. Remaining acceptance work: complete the final independent
security review before enabling the production job.

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
