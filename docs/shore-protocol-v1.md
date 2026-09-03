# Shore protocol v1

This document is the normative wire and trust contract for ADR-0039. The words
MUST, MUST NOT, SHOULD, and MAY have their RFC 2119 meanings. Shore relays one
ADR-0040 v1 JSON frame inside each encrypted envelope; it does not define a
second application protocol.

## Encoding and primitives

- JSON is UTF-8 and canonicalized with RFC 8785 JSON Canonicalization Scheme
  (JCS). Duplicate keys, non-I-JSON numbers, invalid UTF-8, and unknown fields
  in a closed object are rejected before cryptographic processing.
- Binary values use unpadded base64url. IDs are lower-case UUIDv7 strings.
  `seq` is a canonical unsigned decimal string because JavaScript cannot
  represent every 64-bit integer exactly. Timestamps are UTC RFC 3339 with
  exactly three fractional digits.
- Signing is Ed25519. Key agreement is X25519. Payload encryption is
  AES-256-GCM with a 96-bit nonce and 128-bit tag. Key derivation is
  HKDF-SHA-256. Fingerprints are
  `sha256:` plus unpadded base64url SHA-256 of the raw 32-byte public key.
- Public keys are transported as JWK (`OKP`, `crv` `Ed25519` or `X25519`, `x`)
  and stored as their validated raw 32 bytes. Private keys are non-exportable
  platform keys where supported and never enter broker storage, logs, URLs, or
  recovery material.
- A host and browser each have separate signing and agreement keypairs. Crypto
  agility requires a new Shore version; receivers never negotiate down.

## Encrypted outer envelope

The WebSocket frame is a JSON object with exactly these fields:

```json
{
  "v": 1,
  "account_id": "uuidv7",
  "host_id": "uuidv7",
  "device_id": "uuidv7",
  "key_epoch": 1,
  "direction": "browser_to_host",
  "seq": "1",
  "request_id": "uuidv7",
  "issued_at": "2026-09-01T12:00:00.000Z",
  "expires_at": "2026-09-01T12:00:30.000Z",
  "nonce": "base64url(12 bytes)",
  "ciphertext": "base64url(ciphertext || 16-byte GCM tag)",
  "signature": "base64url(64 bytes)"
}
```

`direction` is `browser_to_host` or `host_to_browser`. The AAD is JCS of the
object without `ciphertext` and `signature`. The signature input is JCS of the
object without `signature`, including `ciphertext`. Verification precedes key
derivation and decryption. Plaintext is exactly one JCS-encoded ADR-0040 v1
frame and is limited to 256 KiB; the outer frame is limited to 384 KiB.

The X25519 shared secret is derived from the sender's private agreement key and
the pinned receiver public agreement key. Define:

```text
salt = SHA-256("shore-envelope-v1\0" || account_id || host_id || decimal(key_epoch))
info = "shore-envelope-v1\0" || direction
key  = HKDF-SHA-256(shared_secret, salt, info, 32)
```

Concatenated strings are their UTF-8 bytes without separators other than the
shown NUL. A sender MUST generate a fresh random nonce and MUST persist its
next sequence before sending. Nonce reuse under a derived key is fatal: revoke
the sender key, increment the epoch through local re-pairing, and alert.

Sequence state is per `(account_id, host_id, key_epoch, device_id, direction)`.
It starts at 1, increases strictly, and is durably committed before dispatch.
Receivers persist the greatest accepted sequence and a seven-day set of
request IDs atomically with the authorization decision. A gap is allowed; a
duplicate or lower sequence is not. Every command has a UUIDv7 request ID.
Server events use the originating request ID when one exists and otherwise a
new UUIDv7. A retry reuses the same encrypted plaintext and request ID but uses
a new sequence and nonce; ADR-0040 idempotency determines the result.

`issued_at` may be at most 30 seconds in the receiver's future. `expires_at`
must be after `issued_at`, no more than 60 seconds later, and strictly after the
receiver time allowing 30 seconds of negative skew. Messages outside those
bounds fail closed. Clocks more than 30 seconds apart produce `clock_skew` and
no dispatch.

Validation order is: frame/encoding limits; closed schema; route/session
binding; pinned identity and epoch; signature; time window; sequence/request
replay; key derivation and AEAD; ADR-0040 closed allowlist; capability; dispatch.
The host performs all steps even if the broker claims it already did.

Stable pre-dispatch errors are `shore_invalid_frame`, `shore_identity_mismatch`,
`shore_key_epoch_mismatch`, `shore_bad_signature`, `shore_expired`,
`shore_clock_skew`, `shore_replay`, `shore_decrypt_failed`,
`shore_unsupported_type`, `shore_unauthorized_scope`, and
`shore_capability_denied`. Error detail never distinguishes an unknown key from
a bad signature to an untrusted peer. Authentication failures close with 1008;
oversize closes with 1009; overload closes with 1013. No automatic retry occurs
for 1008 or 1009.

## Initial capability registry

Unknown capability names, versions, message types, fields, and scopes are
denied. Capabilities are grants to one `(host_id, key_epoch, device_id)`, never
to an account session.

| Capability | Browser-to-host ADR-0040 types | Authorized scopes | Notes |
| --- | --- | --- | --- |
| `dashboard.read.v1` | `subscribe`, `unsubscribe`, `ack`, `ping`, `pong` | global lifecycle feed; explicit Flow-step resources already visible to the local dashboard principal | Initial and only production capability. Allows receipt of the v1 snapshot and replayable events listed in ADR-0040. No command or HTTP mutation. |

`chat.start`, `chat.cancel`, every `auth.*` type, arbitrary HTTP/RPC forwarding,
filesystem access, terminal access, and types introduced after ADR-0040 v1 are
not in the registry. Future non-destructive mutations require a new named,
versioned capability and parity tests. Shell requires a distinct
`shell.exec.v1` grant made locally for one device, with an explicit warning,
immediate revocation, and expiry no later than 24 hours; naming it here does
not register or enable it.

Server-to-browser frames are limited to `hello`, `subscribed`, `snapshot`,
`command.result`, `error`, `ping`, `pong`, and the replayable server events
enumerated by ADR-0040 v1, filtered to the authorized subscription. Because the
initial capability sends no domain command, `command.result` can only report a
connection operation. `auth.output` and `auth.done` are always denied remotely.

## Pairing

Pairing is an out-of-band high-entropy ceremony, not trust on first use. The
host creates a random 128-bit `pairing_secret` and an independent 128-bit
`ceremony_nonce`, and expires the ceremony after five minutes. The secret is
displayed locally as a 26-character Crockford-base32 code (130 encoded bits;
the leading two bits are zero). The QR encodes that code plus a public pairing
offer containing exactly `v`, `ceremony_id`, `ceremony_nonce`, `account_id`,
`host_id`, `host_sign_fingerprint`, and `host_enc_fingerprint`. For manual
entry the browser obtains the same public offer from the broker and the user
enters only the 26-character secret. Thus the code carries all 128 secret bits;
it does not purport to encode the independent nonce. Altering the public offer
causes the host binding comparison or finished verification to fail.

The binding object contains exactly `v`, `account_id`, `host_id`, `device_id`,
`ceremony_nonce`, and SHA-256 fingerprints of both signing and agreement keys
for both devices. After scanning/entering the secret, both peers compute:

```text
pair_key = HKDF-SHA-256(pairing_secret, ceremony_nonce,
                       "shore-pair-v1\0" || JCS(binding), 32)
finished(role) = HMAC-SHA-256(pair_key, role || "-finished\0" || JCS(binding))
```

The defined roles are `browser`, `host`, and `browser-confirmed`; the last is
used only for the final key-confirmation packet.

To avoid the circular dependency between decrypting the browser binding and
deriving `pair_key`, the browser's first packet uses a bootstrap key:

```text
bootstrap_key = HKDF-SHA-256(pairing_secret, ceremony_nonce,
                            "shore-pair-bootstrap-v1\0", 32)
```

Pairing packets have the exact outer schema `v`, `ceremony_id`, `direction`,
`nonce`, and `ciphertext`. `v` is integer `1`; `ceremony_id` is UUIDv7;
`direction` is `browser_to_host` or `host_to_browser`; `nonce` is a fresh
12-byte base64url value; and `ciphertext` is AES-256-GCM ciphertext plus its
16-byte tag. The AAD is JCS of the packet without `ciphertext`. Any other field,
non-canonical encoding, reused nonce, wrong direction, or unknown/used ceremony
fails as generic `pairing_failed` before trust is stored.

Before sending, the browser MUST compare the offer's account and host IDs with
its authenticated route and use the advertised host fingerprints in the
binding. The browser packet is encrypted with `bootstrap_key`. Its plaintext has exactly
`v`, `binding`, `browser_keys`, and `finished`; `browser_keys` has exactly
`signing` and `agreement`, each a 32-byte base64url public key, and `finished`
is `finished(browser)`. The host decrypts it, reconstructs the binding from the
locally pinned account/host identity, ceremony nonce, received device ID and
public keys, compares the supplied binding byte-for-byte, then verifies the
browser finished value.

The host also verifies that the binding's account ID, host ID, ceremony nonce,
and host fingerprints match its local ceremony and keys, and that both browser
fingerprints match the received keys. The host response uses a different random nonce and is encrypted with
`pair_key`. Its plaintext has exactly `v`, `binding`, `host_keys`, and
`finished`; `host_keys` has the same closed schema and `finished` is
`finished(host)`. The browser verifies that the returned host keys hash to the
fingerprints in the binding and public offer, verifies the host finished value,
and pins both host keys. The broker sees the public offer plus the outer
ceremony ID, direction, packet nonce, ciphertext length, and timing, but never
the secret.

After verifying and pinning the host keys, the browser sends a final packet
encrypted with `pair_key`, with a new nonce and the same outer schema and
`browser_to_host` direction. Its plaintext contains exactly `v`, `binding`, and
`finished`, where `finished` is `finished(browser-confirmed)`. The host verifies
that confirmation before atomically storing device approval. Every packet nonce,
including failed attempts and the host response, is unique within a ceremony.

Both peers verify the binding, pinned account/host identity, and the other
side's `finished` value before the host atomically stores approval. The secret
is uniformly random rather than memorable, so a captured transcript has at
least 128 bits of offline work. It is erased after success, expiry, cancellation,
or five failures. Creation and attempts are limited per account, host, session,
device, opaque network fingerprint, and broker source IP. Concurrent success is
resolved by an atomic unused-to-used transition; losers receive the same generic
failure as expired ceremonies.

## Offline recovery verifier

The client generates a random 256-bit recovery secret and displays its
52-character Crockford-base32 form once. The service stores only:

```text
verifier = SHA-256("shore-recovery-v1\0" || account_id || recovery_secret)
```

Recovery submission occurs only over TLS after account authentication and a
fresh second factor. The service hashes the decoded 32 bytes and compares in
constant time, then atomically consumes the verifier. The secret's 256-bit
entropy makes an exported verifier unsuitable for offline guessing without
relying on a password KDF. Rotation invalidates the prior verifier. It never
derives, wraps, restores, or proves continuity with a host/device key.

Valid recovery starts the state machine in `shore-state-machines.md`; it does
not itself grant commands or device trust.

## Test vectors

Normative positive and negative vectors are in
[`shore-protocol-v1-vectors.json`](shore-protocol-v1-vectors.json). An
implementation MUST reproduce the derived values, accept the positive envelope,
and reject every listed mutation before interoperability testing.
