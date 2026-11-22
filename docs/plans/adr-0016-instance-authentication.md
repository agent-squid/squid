# ADR-0016 implementation plan — Squid instance authentication

Implement the [instance authentication amendment](../decisions/0016-security-model.md#amendment-2026-11-22--authenticate-access-to-the-squid-instance). A browser needs a paired session to use the instance. Existing agent and operator tools keep their scoped credentials.

## 1. Inventory entry points and define authorization

- Inventory HTTP routes, delegated routers, UI assets, and realtime operations in `agent/server.py` and the route modules it mounts. Include chat, commands, files, quota and credential setup, terminal and auth-session I/O, and Shore entry points.
- Declare the allowed caller and required scope for each route and realtime operation. Share the authorization decision across HTTP and WebSocket handlers for the same operation. Fail closed for unclassified API routes, including routes added after this rollout.
- Keep the public surface limited to the pairing shell, the assets it needs, and the pairing exchange. Give host lifecycle checks a minimal health response or a scoped operator credential if they need more detail.
- Keep the server bound to loopback. Authenticate forwarded requests even if their peer address is loopback. Document secure access for Tailscale and SSH forwarding before retiring the raw HTTP remote URL from the pairing UI. Preserve Shore's separate pairing and transport checks under [ADR-0039](../decisions/0039-remote-access-via-shore-relay.md).

## 2. Implement host bootstrap and browser pairing

1. Add a host CLI command to generate the first browser pairing link using a host-only operator credential. Keep host credentials outside the browser and out of URLs. Confirm that possession of the instance URL or access to its port cannot bootstrap a browser.
2. Have a paired owner browser's `/remote` action issue a fresh pairing credential and show its QR code. Use the HTTPS remote URL for remote devices and support a local pairing URL for host setup.
3. Store a hash of the pairing credential with its expiration and authorized session capabilities. Redeem it once in a transaction that also creates the device session. All devices represent the same owner. Reject expired, used, and revoked credentials.
4. Put the credential in the URL fragment. The pairing page clears the fragment before sending the credential in a same-origin POST. Set an opaque `HttpOnly`, `SameSite` session cookie and use `Secure` on HTTPS. Keep credentials out of logs, Web Storage, and rendered history. Require HTTPS for remote pairing and sessions.
5. Handle expired sessions by returning to pairing. Preserve navigation to the requested page only for valid same-origin destinations. Give users a clear host recovery path if their last browser session is revoked.

The pairing lifetime and session lifetime can be tuned during implementation. Both need explicit expiry and tests with a controllable clock.

## 3. Protect browser operations and preserve local tools

- Authenticate browser requests and WebSocket handshakes with the session cookie. Validate the browser origin for state changes and upgrade requests. Enforce capabilities again when accepting privileged frames, including terminal input, auth-session input, and restart commands.
- Require a paired owner browser and an explicit action for Keychain unlock. Keep password input inside the existing auth-session flow. Require the remote-unlock opt-in for forwarded and ambiguous TCP requests, including ones whose peer appears to be loopback. Skip the opt-in only with positive host-local proof independent of peer address and browser headers. If the browser transport offers no such proof, apply the opt-in to all browser unlock requests.
- Keep `agentsquid watch`, `search`, and `continue` working with their existing turn or host-control credentials. Check their operation and target scopes where those credentials are consumed. Keep turn credentials out of browser responses and browser credentials out of subprocess environments.
- Store the initiating browser session ID on browser-started turns where available. Link subsequent tool activity to that turn for attribution. The recorded ID confers no authorization.
- Keep developer API keys out of this implementation. Exercise machine callers with the existing scoped credentials or ephemeral credentials in integration tests.

## 4. Implement device management and revocation

- Give operators a device list with a label, creation time, last activity, and revocation. Store the session identifier separately from the credential so the list and audit events can omit secrets.
- Invalidate the session on revocation, close its realtime connections, and terminate its browser-owned interactive terminal and auth processes. Bind these processes to the creating browser session. Check ownership and session validity on every attach, replay, and input operation. Serialize connection and process registration with revocation so a concurrent request cannot leave a usable socket or orphan process.
- Add an operator action to revoke all browser sessions and outstanding pairing credentials. Keep host bootstrap available for recovery. Ordinary agent turns retain their existing lifecycle and end with the turn or earlier scope revocation.
- Audit issuance, redemption, revocation, and privileged actions using session IDs and turn IDs where available.

## 5. Verify and roll out

| Area | Acceptance check |
|---|---|
| Bootstrap | A fresh host can pair its first browser. An unauthenticated client cannot mint a QR code or reach a protected API. |
| Pairing | A valid code establishes the intended session. Concurrent redemption succeeds once. Expired and revoked codes fail. |
| Transport | Direct local access, Tailscale HTTPS, and the documented SSH setup enforce the same application boundary. Raw remote HTTP cannot establish a browser session. |
| HTTP and realtime | Protected routes and sockets reject missing or expired credentials. Cross-origin state changes and upgrades fail. A paired client can use its granted operations. |
| Scope | Turn credentials work for their granted operation and target. They cannot call browser or operator endpoints. Existing host lifecycle and scoped CLI operations work. |
| Revocation | Revoking one device closes its sockets, stops its interactive terminal and auth processes, and preserves other device sessions. Reattach and replay are denied after revoke. Revoke-all also invalidates outstanding pairing codes. |
| Keychain | Unlock requires a paired owner session and an explicit action. Forwarded or ambiguous requests obey the opt-in even when the TCP peer appears local. |
| Shore | Existing Shore pairing and relay authorization still work with their declared caller scopes. |

Build and verify the bootstrap path before enforcing the boundary on existing
installations. Exercise it on a fresh instance and an upgrade. Confirm that a
host operator can recover from a revoked browser session without weakening
endpoint authorization. Once the boundary is enabled, recovery uses host
bootstrap and redeployment of a known-good authenticated build.
