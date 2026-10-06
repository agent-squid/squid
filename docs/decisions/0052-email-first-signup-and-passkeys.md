---
status: accepted
date: 2026-10-06
---
# ADR-0052: Email-First Signup and Passkeys as the Default Second Factor

## Context and Problem Statement

ADR-0039 signup asked for a handle and an email address together. Shore
reserved both before the address was proven, for the 10-minute life of the
emailed code. That caused two problems:

- **A typo stranded the handle.** Someone who mistyped their email never got
  the code, and the handle they wanted stayed reserved to the mistyped
  address. Signing up again with the right address was refused for up to 10
  minutes.
- **Handles could be squatted.** Anyone could hold any handle for 10 minutes
  with an address they did not own, and repeat that as each hold lapsed. The
  only limits were per-IP signup and edge rate limits.

The second factor was a TOTP authenticator app: install an app, scan a QR code,
type six digits at every sign-in and sensitive action. TOTP codes can also be
phished: a look-alike page can collect one and replay it within its 30-second
window. A Shore session can register a host and approve browsers for remote
access to the user's agent machines, so phishing resistance matters. ADR-0039
already allowed "TOTP or passkey" but only TOTP was implemented.

## Decision Drivers

- Nothing scarce (a handle) is held for an unproven address.
- A mistyped email costs nothing: correct it and continue.
- The default second factor resists phishing and needs nothing typed.
- Accounts keep exactly one email-plus-second-factor model: no new way in that
  bypasses the email code, and no factor enrollable from email alone once one
  exists.
- `agentsquid login` keeps working for every account, including one whose only
  factor is a passkey a terminal cannot use.
- Existing TOTP accounts keep working unchanged.

## Decision Outcome

### Signup: email, code, handle, second factor

1. `POST /signup/start {email}`. Shore emails a code to a new or still-pending
   signup. For an address that already has an account, it instead emails that
   owner a reminder of their handle. The response is the same `202 {sent:true}`
   either way, so the endpoint does not reveal which addresses are registered.
   Rate limits on emailed codes and reminders, and email delivery failures, also
   return `202`. They are logged (`shore_signup_delivery_failed`) instead of
   returned, because an error only one branch can produce would reveal
   registration. The page offers "Email me a new code".
   Only the address is held (`signup-account:<id>`, `signup-email:<email>` in
   the identity index), for the code's 10 minutes. Starting again with the same
   address reuses the pending account.
2. `POST /signup/verify {email, token}` consumes the code into an email-only
   (`account_authenticated`) session and extends the hold to the session's 15
   minutes. An unknown, registered, or lapsed address fails exactly like a wrong
   code.
3. `POST /signup/handle {email, handle}` (session cookie plus CSRF). The
   verified session chooses its handle; choosing again releases the previous
   choice. Only now is the handle held (`signup:<handle>`), until the session
   lapses.
4. The second factor is enrolled through the ordinary `/@handle/auth/*` routes,
   which already resolve a chosen-but-unactivated handle to its pending
   account. The first confirmed factor activates the account and rotates to a
   `remote_authenticated` session with a fresh step-up.

A signup that never activates is erased by the index and account alarms (email,
handle choice, pending TOTP secret, any passkey); audit records are kept. The
retired handle-first `POST /@handle/auth/signup` answers `410 signup_moved`.

### Passkeys (WebAuthn) as a second factor

The browser offers "Create a passkey" first and "Use an authenticator app
instead" second. Browsers without WebAuthn go straight to TOTP. Passkeys are a
second factor after the email code, exactly like TOTP: the session state
machine does not change.

| Route (`/@handle/auth/…`) | Session | Purpose |
|---|---|---|
| `passkey/register-options` | email-only with no factor, or fresh step-up | creation options |
| `passkey/register` | same | stores the credential; as the first factor, also completes step-up |
| `passkey/options` | any | request options for the account's passkeys |
| `passkey/verify` | any | verifies an assertion; completes step-up |
| `passkeys/<id>/revoke` | fresh step-up | removes one passkey, never the last factor |

- Relying party ID is the host of `ALLOWED_BROWSER_ORIGIN`; origin must equal it
  exactly; cross-origin assertions are rejected.
- User verification (biometric or device PIN) and user presence are required on
  every ceremony.
- Accepted algorithms: EdDSA/Ed25519 (-8), ES256 (-7), RS256 (-257, Windows
  Hello), with RSA keys of at least 2048 bits.
- Attestation is `none`. Shore does not trust or check the authenticator's make
  or model.
- Each session has one outstanding challenge, valid for 5 minutes and consumed
  by any attempt, successful or not.
- A signature counter that stops increasing (when either side is non-zero) is
  rejected as a possible cloned authenticator. Synced passkeys report zero.
- Failed assertions count toward the same per-session and per-account
  five-failure lockout as TOTP codes.
- At most 10 passkeys per account. Adding or removing one creates a security
  notification and email.

**Factor rules.** An email-only session may enroll a factor only while the
account has none. Afterwards, adding a passkey needs a step-up within the last
5 minutes, and TOTP is never added this way. A pending (unconfirmed) TOTP secret
cannot be confirmed once a passkey exists, and registering the first passkey
deletes it. The operator `totp-reset` operation now resets every second factor
(TOTP and passkeys). The operation name is unchanged for console and audit
compatibility.

The email-code response now lists the account's confirmed factors (`factors:
["passkey", "totp"]`), so sign-in clients know which step comes next.

### Terminal sign-in approved by a passkey

A terminal cannot run WebAuthn. For a passkey account, `agentsquid login`
signs in by email and then requests an approval:

1. `POST auth/approval/start` (email-only session) returns an 8-character code
   (31-symbol alphabet, about 40 bits) and `/@handle/approve`. The approval is
   bound to that session and expires within 10 minutes or with the session.
2. On a device that has the passkey, the person opens `/@handle/approve`, enters
   the code, and confirms with the passkey. `POST auth/approval/approve`
   requires a step-up within the last 5 minutes.
3. The terminal polls `POST auth/approval/poll` (every 3 s, in-memory rate
   limit). When the approval is approved, its own session is rotated to
   `remote_authenticated` with a fresh step-up, as if it had entered an
   authenticator code.

Accounts with both factors may type a TOTP code or press Enter to use the
approval instead.

Like any device-authorization flow, this can be phished: an attacker who
controls the victim's email starts a terminal sign-in and persuades the victim
to approve its code. The approve page says to approve only a sign-in you just
started yourself and never a code someone sent you. Approving requires a fresh
passkey check on the real origin. An approval works only for the session that
requested it and can be used once.

## Considered Options

### Signup

- **Cancel token plus a countdown (rejected).** The browser that started a
  signup could cancel it and fix the address, and the handle check could show
  when a hold expires. That fixes typos but still lets unverified addresses hold
  handles.
- **Email first (chosen).** It fixes both problems and adds one screen.

### Second factor

- **TOTP only (status quo).** Phishable, and needs an app plus typing.
- **Passkey by default with TOTP fallback (chosen).**
- **Passwordless passkey sign-in, no email code (deferred).** A passkey with
  user verification is stronger than email plus TOTP, but dropping the email
  code changes ADR-0039's authentication model. Revisit separately.
- **OAuth ("Continue with GitHub") (deferred).** It would verify the address in
  one click, but adds a provider dependency and account-linking cases.

### Terminal sign-in for passkey-only accounts

- **Require TOTP for the CLI (rejected).** Every passkey account would need a
  second factor whose only purpose is the CLI, and the first step after web
  signup is `agentsquid login`.
- **Browser approval with a short code (chosen).**

## Consequences

- Good: a mistyped address costs nothing. Handles are held only for verified
  addresses, for at most one 15-minute email-code session.
- Good: the default second factor is phishing-resistant and needs nothing typed.
- Good: signup no longer reveals whether an address is registered; the owner is
  told by email instead.
- Mixed: one more signup screen (handle after email).
- Bad: Shore now carries its own WebAuthn verifier (CBOR, COSE, DER) instead of
  a library. It is small and covered by software-authenticator tests for all
  three algorithms and by real Chromium virtual-authenticator output.
- Bad: older `agentsquid` builds can no longer create accounts (`410
  signup_moved`). Sign-in still works with TOTP.

## Verification

- Shore Worker tests cover email-first signup, indistinguishable delivery
  failures, factor enrollment and removal rules, lockout, all three accepted
  WebAuthn algorithms, and terminal approval.
- Pairing-app tests cover passkey option conversion, authentication-flow state,
  and approval-code handling.
- The Playwright TOTP and passkey journeys pass against a local Worker while
  using Resend for real email delivery. The passkey journey creates a Chromium
  virtual authenticator, signs in again from a fresh browser context, and
  approves an email-only terminal session.
- The CLI E2E completes email-first signup, TOTP verification, and signed host
  registration against the local Worker over HTTPS. Its later browser-pairing
  phase requires a signed AgentSquid Client release in the `RELEASES` bucket;
  an empty local bucket correctly reports that remote access is unavailable.
  Run that complete pairing phase against preproduction, or provision a signed
  local release explicitly.

## Related decisions

- ADR-0039: Remote access via Shore relay. This ADR amends its "User
  registration" section and implements its "TOTP or passkey" second factor.
- ADR-0050: Independent Shore web-client distribution (the Shore-served pages
  that run these ceremonies).
