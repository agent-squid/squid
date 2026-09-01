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

- Verified email plus second factor moves `pending_email` to `active` and
  atomically claims a normalized username for a random immutable account ID.
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
`closed`. Every socket proves a fresh broker nonce. A different key fails. With
no healthy current socket, the proven same-key socket becomes current and emits
an audit-only reconnect. If the old socket is broker-observed healthy, the new
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
broker/account authorization and never device trust.

Browser devices are `unpaired`, `pairing_pending`, `paired`, or `revoked`.
Only the ceremony in `shore-protocol-v1.md` transitions an unpaired device to
paired. A ceremony is `unused`, `used`, `expired`, `cancelled`, or `exhausted`;
success is a single atomic `unused` to `used` transition. Expiry, cancellation,
five failures, host epoch change, host revocation, recovery, or account deletion
prevents later success. Revocation closes the device socket, invalidates grants
and replay state, and cannot be undone; the device must receive a new ID and
pair again.

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
