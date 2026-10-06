# Shore identity and lifecycle state machines

This document is normative for ADR-0039. Every transition is serialized by the
named Durable Object, committed with its audit event, and fails closed if that
commit fails. Records are append-only; “delete” below means tombstone and
cryptographic erasure of separately encrypted personal fields, not reuse of an
identity.

## Account, routing, and username

The identity-index object serializes account creation and username changes.
Account states are `pending_email`, `active`, `recovery_pending`,
`deletion_pending`, and `deleted`.

- Signup is email-first (ADR-0052). `pending_email` has three sub-steps, each
  bounded by a deadline the index and account alarms enforce:
  1. *Address held.* Starting signup holds only the normalized address, for the
     10-minute life of its emailed code. Starting again with the same address
     reuses the pending account. An address that already has an account holds
     nothing; its owner is emailed a reminder, and the requester sees the same
     response.
  2. *Address verified.* Consuming the code creates an email-only session and
     extends the hold to that session's 15 minutes.
  3. *Username chosen.* Only that verified session can choose a username,
     which is then held until the same deadline. Choosing again releases the
     previous choice, and another account's held username is refused.

  The first confirmed second factor (an authenticator code, or registering a
  first passkey) moves `pending_email` to `active` and atomically claims the
  chosen username for the random immutable account ID. A lapsed signup releases
  its address and username and erases its personal fields; audit records
  remain.
- A rename reserves the new normalized name, commits it to the account object,
  swaps the index binding, and tombstones the old name in one transaction
  protocol. Until commit, the old route remains authoritative; after commit it
  returns `username_moved` for 30 days without disclosing the new name to an
  unauthenticated caller. Crash recovery completes or rolls back from the
  durable transaction record. Names are never aliases to two accounts.
- Account deletion requires a second factor no more than five minutes old,
  enters `deletion_pending` for seven days, notifies every channel, and is
  cancellable during that period. Completion revokes sessions, current host,
  pairings, capabilities, and recovery verifier; closes sockets; cryptographically
  erases personal fields; tombstones the account and username; and retains only
  legally/security-required audit data. Deleted IDs and host IDs are never reused.

Routes are strictly `/@<username>` plus specified subpaths. Normalize one time
with Unicode NFKC, ASCII lowercase, and an allowlist of `[a-z0-9]` with length
3–32; reject input that changes under normalization, percent-encoded separators,
empty/dot segments, and reserved words. An authenticated session carries both
account ID and current normalized username. A mismatch is rejected; normal
dashboard routing never consults the identity index.

## Host registration, connection, epochs, and replacement

An account has zero or one `current` host and any number of immutable `revoked`
hosts. A host is `registration_pending`, `current`, `revocation_pending`, or
`revoked`. Registration requires account authentication, fresh second factor,
and a nonce-bound Ed25519 proof over the candidate immutable host ID and both
public keys.

- With no current host, a valid candidate becomes `current` at key epoch 1.
- With a current host, another host ID or different key is rejected. It cannot
  displace or update the record.
- A current host may rotate keys only with signatures from the old keys plus
  local confirmation. The epoch increments, every pairing/capability is revoked,
  browsers block on the key change, and re-pairing is required.
- Revocation with a five-minute-fresh step-up atomically marks the host revoked,
  closes its socket, revokes host-bound browser sessions, pairings and grants,
  and clears the current-host pointer. Security revocation has reserved capacity
  and remains available during degradation.
- Only after that commit may a new immutable host enter `registration_pending`.
  It never inherits the prior ID, epoch, pairing, sequence state, or capability.

Each connection is `challenged`, `proven`, `current_socket`, `stale`, or
`closed`. Every socket proves a fresh relay nonce. A different key fails. With
no healthy current socket, the proven same-key socket becomes current and emits
an audit-only reconnect. If the old socket is relay-observed healthy, the new
same-key socket wins atomically, the older socket closes, and Shore emits a
correlated high-severity event and immediate privacy-safe alert. Further events
in ten minutes remain individually audited and are losslessly batched in user
notifications. A heartbeat-expired/closed old socket is stale and does not alert.

Hibernation attachment metadata contains only immutable connection ID, role,
account/host/device IDs, epoch, authenticated-at, heartbeat deadline, and queue
watermark and stays below 16 KiB. Constructor restart reconstructs socket roles
from attachments and authoritative identity, replay, grant, and queue state
from storage; attachment claims never override storage.

## Browser sessions and pairing

Browser sessions are `login_pending`, `account_authenticated`,
`remote_authenticated`, `revoked`, or `expired`. Magic-link login alone reaches
`account_authenticated`. A passkey or TOTP promotes it to
`remote_authenticated`; sessions rotate on promotion and refresh, are short
lived, and bind CSRF state and secure same-site cookies. Session state is only
relay/account authorization and never device trust.

Second factors (ADR-0052):

- An `account_authenticated` session may enroll a factor only while the account
  has none. Once any factor is confirmed, adding a passkey requires a step-up no
  more than five minutes old. TOTP is only ever enrolled as the first factor.
- A TOTP secret stays pending until its first valid code. It can never be
  confirmed once a passkey exists, and registering the first passkey deletes it.
- The last remaining factor cannot be removed by the user; only an operator
  second-factor reset can do that.
- Passkey assertions require user presence and verification, an exact origin
  and relying-party match, and a single-use per-session challenge. A signature
  counter that stops increasing is rejected. Failures share TOTP's five-failure
  per-session and per-account lockout.

A terminal (`agentsquid login`) cannot run WebAuthn. For a passkey account, its
`account_authenticated` session may request a *login approval*. An approval is
`pending`, then `approved` or `expired`, and is consumed once. It carries an
8-character code shown only in the terminal, is bound to the requesting
session, and expires within ten minutes or with that session. A different
`remote_authenticated` session with a step-up no more than five minutes old
approves it by entering the code. The requesting session then rotates to
`remote_authenticated` with a fresh step-up, exactly as if it had verified a
factor itself. No other session can collect it.

After the first full email-plus-second-factor login and pairing, an expired browser
session may be restored without repeating those factors. The relay issues a
one-minute, single-use challenge bound to the account and paired device ID;
the browser signs the domain-separated challenge with its non-exportable
Ed25519 device key. Successful verification creates a short-lived
`remote_authenticated` session without `stepUpAt`. It therefore permits normal
remote connection but cannot start or complete recovery or account deletion,
revoke trust, replace a host, or authorize any other operation requiring a
fresh second factor. Cancellation of an already-pending recovery or deletion
remains available to any fully authenticated session as a protective action,
as described below.
Revoked/unpaired devices, unknown keys, expired challenges, and replayed
signatures fail generically.

Browser devices are `unpaired`, `pairing_pending`, `paired`, or `revoked`.
Only the ceremony in `shore-protocol-v1.md` transitions an unpaired device to
paired. A ceremony is `unused`, `used`, `expired`, `cancelled`, or `exhausted`;
success is a single atomic `unused` to `used` transition. Expiry, cancellation,
five failures, host epoch change, host revocation, recovery, or account deletion
prevents later success. Revocation closes the device socket, invalidates grants
and replay state, and cannot be undone; the device must receive a new ID and
pair again.

The host's authenticated pairing approval supplies the device's public signing
key and granted capability names. Shore persists both, emits a durable pairing
security notification, and exposes the capability names (but not the signing
key) in the account security view.

## Recovery

Recovery is `idle`, `verified`, `cooling_off`, `cancelled`, or `completed`.
Account recovery can restore administration but never advances device trust.

- A valid unconsumed offline verifier plus fresh account authentication and
  second factor atomically consumes the verifier, enters `verified`, and sends
  notifications. If the old host provides a signed local approval, revocation
  may proceed immediately.
- Without trusted-host approval, `verified` enters a seven-day `cooling_off`.
  Shore notifies at initiation and at least 24 hours before completion. Any
  existing trusted host, recovery cancellation token, or fully authenticated
  account session may cancel; support cannot shorten or bypass the delay.
- Completion revokes the old host and everything bound to it before clearing
  the current-host pointer. The replacement registers as a visibly new host and
  starts with no pairings or capabilities.
- Losing account factors and the recovery secret may restore account
  administration only through the same delayed process; it does not restore
  cryptographic trust. All transitions are high-severity audit events.

## Revocation propagation and deletion races

Revocation/deletion generation numbers live in account storage. A socket or
command captures a generation, then checks it again in the same transaction as
authorization/dispatch. A changed generation rejects the operation. Closing
sockets is a consequence, not the security boundary. Recovery, deletion,
rename, key rotation, pairing, and host connection replacement use idempotent
transition IDs so retry after a restart cannot repeat or partially apply them.
