# Plan: ADR-0039 Shore remote access

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
- Pairing establishes trust through a host-displayed code that commits to both
  device-key fingerprints; broker-provided trust-on-first-use is insufficient.
- Every command is signed, encrypted, capability-checked, replay-protected,
  expiry-checked, and correlated with tamper-evident audit records.
- Remote access is disabled by default. Arbitrary shell capability is separate,
  device-specific, locally granted, revocable, and expires within 24 hours.
- The existing direct local/Tailscale and SSE compatibility paths remain
  operational throughout the rollout.

## Milestone 0 — Resolve specifications and operational ownership

**Objective:** turn the proposed ADR into an accepted, implementable contract
without embedding unsettled security choices in code.

**Actions:**

1. Define the encrypted outer envelope, canonical serialization, algorithms,
   key formats, sequence and request-ID rules, expiry/skew policy, and error
   behavior in a versioned Shore protocol document. The ciphertext payload is
   an ADR-0040 v1 frame.
2. Define host registration, browser session, pairing, recovery, revocation,
   username rename, and account deletion state machines.
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

**Acceptance:** the protocol and state machines have test vectors; the threat
model has no unowned critical mitigation; ADR-0039 is updated and accepted.

## Milestone 1 — Broker skeleton and opaque relay

**Objective:** establish deployable Worker/Durable Object boundaries without
remote execution.

**Actions:**

1. Add a separately testable Cloudflare Worker project with local development,
   type checking, unit tests, migration configuration, and environment bindings.
2. Implement strict `/@<username>` route parsing while leaving all non-`/@*`
   traffic untouched.
3. Add the identity-index object and immutable-account-ID keyed account object,
   including normalized username uniqueness and rename transactions.
4. Add authenticated host and browser WebSocket attachment, hibernation-safe
   attachment metadata, bounded queues, connection replacement rules, and
   deterministic offline/overload errors.
5. Relay only opaque test frames with size, rate, origin, and lifetime limits.
   Do not expose a command-capable production route yet.

**Acceptance:** local integration tests cover routing isolation, concurrent
username claims, reconnect/replacement, hibernation restore, host offline,
backpressure, malformed frames, and broker inability to inspect test payloads.

## Milestone 2 — Account authentication and signed host registration

**Objective:** authenticate accounts and prove host possession of its private
key without treating either as command authorization.

**Actions:**

1. Implement email magic-link signup/login with short-lived, rotating sessions,
   CSRF protection, secure cookie settings, rate limits, and mandatory second
   factor before remote-access session issuance.
2. Add CLI account login and local host-key generation/storage with explicit
   permissions and no private-key export.
3. Register the host public key and require nonce-bound signed challenges on
   each host WebSocket connection. Reject stale, replayed, or mismatched
   registrations.
4. Add account, session, and host-device listing and revocation surfaces.

**Acceptance:** tests cover token replay/expiry, session rotation/revocation,
login and registration throttling, host impersonation, key replacement, and
restart/reconnect. Passing this milestone still does not permit commands.

## Milestone 3 — End-to-end channel and local pairing

**Objective:** establish broker-blind, mutually authenticated communication
between a paired browser device and the host.

**Actions:**

1. Implement the specified crypto envelope on host and browser using audited
   platform cryptography, canonical bytes, and published cross-language test
   vectors.
2. Generate a non-extractable browser key and perform the local pairing
   ceremony. The displayed code must bind account, host, browser, protocol
   version, and both public-key fingerprints.
3. Persist approved device keys and replay state on the host. Pin the host key
   in the browser. Require local approval for key changes; recovery revokes old
   device trust.
4. Enforce expiry, monotonic sequence, unique request ID, signature, and
   authenticated-encryption validation before decoding any ADR-0040 frame.

**Acceptance:** interoperability and negative tests cover tampering, wrong keys,
replay across connections, reordered/duplicate IDs, clock skew, key
substitution, pairing races, revocation, recovery, and broker frame injection.
An independent security review has no unresolved critical or high findings.

## Milestone 4 — Capability-scoped ADR-0040 relay

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
   for pairing, key changes, recovery, revocation, and privileged grants.

**Acceptance:** tests detect deletion, insertion, mutation, fork, missing
correlation, and forged host events; retention and redaction tests show command
text and secrets are absent by default.

## Milestone 6 — Production hardening and staged rollout

**Objective:** prove the system fails closed and is operable before enabling
remote mutations.

**Actions:**

1. Add end-to-end tests for Worker/Durable Object restarts, region changes,
   network loss, duplicate connections, cursor rollover, offline hosts, overload,
   revocation during execution, recovery, and multi-device convergence.
2. Add abuse controls, CSP, immutable/versioned client assets,
   reproducible-build hashes, dependency scanning, secret rotation, alerts,
   metrics, runbooks, backups, migration/rollback procedures, and kill switches.
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
