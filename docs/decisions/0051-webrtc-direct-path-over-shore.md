---
status: proposed
date: 2026-09-25
---
# ADR-0051: WebRTC Direct Path with Shore as Signaling and Fallback

## Context and Problem Statement

ADR-0039 routes every remote byte through the Shore relay: the browser and the
host each hold a WebSocket to the account Durable Object, and the host
processes relay frames serially on one socket. Today only read-only state push
(`dashboard.read.v1`) crosses it; commands, files, diffs, uploads, and other
HTTP-shaped operations are future work.

Carrying the full feature set through the relay is viable (Cloudflare does not
meter Worker/Durable Object bandwidth, so request and duration charges dominate,
not bytes), but it has structural limits:

- One host socket multiplexes every device and operation, so a large response
  head-of-line blocks live events for all paired devices.
- Cloudflare caps WebSocket message size, so large bodies need chunking,
  per-stream flow control, and prioritization inside the envelope protocol.
- Throughput and latency are bounded by one Durable Object per account and an
  extra network hop.
- All application traffic depends on Shore's availability and capacity.

Tailscale avoids the equivalent problem by using its servers only as a
coordination plane (keys, peer lists) and sending data peer-to-peer over
NAT-traversed WireGuard, with DERP relays as a fallback. Browsers cannot run
WireGuard or raw UDP, but WebRTC data channels provide the same shape natively:
ICE/STUN hole punching, DTLS encryption, and TURN fallback.

## Non-Goals

- Not a general-purpose network or VPN. The direct path connects the Squid web
  client to the Squid process only; no other host ports or services are
  reachable through it.
- Not a replacement for the Shore relay. The relay remains the baseline
  transport that must always work.
- Not a change to the host's `127.0.0.1` bind or its outbound-only network
  posture.
- Not in scope before ADR-0039 Milestone 6 closes and the full feature set works
  over the relay.

## Decision Drivers

- Remove relay head-of-line blocking and message-size constraints for files,
  diffs, and parallel requests.
- Lower latency, and keep traffic on the LAN when the phone and host share a
  network.
- Reduce the share of application traffic and trust surface that passes
  through Shore.
- Keep one application protocol: the direct path must carry the same sealed
  ADR-0039/ADR-0040 envelopes and capability checks, not a second API.
- No client install: must work from the existing browser/PWA client.

## Decision Outcome

Proposed: add an opportunistic WebRTC data-channel path between the browser
client and the Squid host, negotiated over Shore, with the Shore relay as
transparent fallback.

### Connection flow

1. The browser attaches over Shore exactly as today and starts working
   immediately over the relay.
2. Browser and host exchange SDP offer/answer and ICE candidates as sealed
   application envelopes over the existing relay channel. Shore sees only
   opaque ciphertext.
3. ICE gathers host (LAN), server-reflexive (STUN), and optionally relay (TURN)
   candidates and performs connectivity checks. The host-side peer runs inside
   the Squid process and uses outbound UDP only; no listening port is opened
   and the HTTP server's `127.0.0.1` bind is unchanged.
4. Once the data channel is up and verified, the client migrates request and
   event traffic to it. On failure, ICE disconnect, or network change, traffic
   falls back to the relay without user-visible interruption, and the client
   may attempt an ICE restart.

### Security requirements

- **Fingerprint binding.** The DTLS certificate fingerprints in the SDP must be
  signed by the paired device key and host key and verified inside the sealed
  envelope. An unsigned or mismatched fingerprint aborts the direct path, so a
  compromised Shore cannot man-in-the-middle it.
- **Same authorization.** Every message on the data channel is a sealed
  envelope subject to the same device trust, replay protection, capability
  authorization (`agent/shore_capabilities.py`), and audit recording as relay
  frames. The data channel is never a raw tunnel to the local HTTP server.
- **Audit continuity.** Direct-path traffic does not carry Shore relay
  receipts. The audit design must define how host-local audit events for
  direct-path operations reconcile with the ADR-0039 receipt chain before this
  ships.
- **Client integrity.** The direct path only protects data if the client code is
  trusted. It depends on ADR-0050 signed client releases and bootstrap
  verification; it must not weaken them.
- **TURN.** If a TURN service is used, it relays DTLS ciphertext only.
  Credentials are short-lived and issued per session by Shore.

### Transport behavior

- Separate data channels (or stream IDs) for control/events and bulk
  transfers, so file traffic cannot block live state.
- Large bodies are chunked with per-stream flow control on the direct path too;
  SCTP message-size limits still apply.
- Host implementation choice (e.g. Python `aiortc` vs. a Go `pion` sidecar) is
  deferred to a prototype that measures throughput, CPU, and packaging cost.

## Considered Options

### Option A: Relay-only (status quo, extended)

Carry all features through Shore with chunking, stream multiplexing, and
priority lanes. Simplest trust model and works on every network, but keeps
head-of-line, message-size, and single-Durable-Object throughput limits, and
routes all data through Shore.

### Option B: Encrypted object-storage side channel for bulk data

Browser encrypts large payloads client-side and uploads via a presigned URL
(e.g. R2); the host fetches and decrypts. Solves bulk transfer cheaply and
works everywhere, but adds storage lifecycle/retention concerns and does not
improve latency for interactive traffic. Remains a valid complement if the
direct path's success rate on mobile networks is poor.

### Option C: WebRTC direct path with relay fallback (proposed)

Tailscale-like data plane for browsers. Best latency and throughput, and keeps
data off Shore when direct connectivity succeeds, at the cost of NAT-traversal
complexity, a new host dependency, and an additional authenticated ingress
surface.

### Option D: Require a mesh VPN (Tailscale/WireGuard) on every device

Strongest connectivity and always-on background behavior, but requires an app
install on every device and exposes more than Squid. Conflicts with the
clientless browser model of ADR-0039.

## Expected Behavior and Limits

- **Same LAN.** Phone and host on the same Wi-Fi connect via host candidates;
  traffic stays local.
- **Cellular/CGNAT.** Hole punching fails more often; those sessions use TURN
  or the Shore relay.
- **Mobile background.** Browsers suspend background tabs, so the direct path
  drops when the app is backgrounded. Notifications continue to rely on Shore
  and Web Push. Reopening reconnects over the relay immediately, and the direct
  path is re-established within roughly 1–3 seconds.
- **Network roaming.** A Wi-Fi/cellular switch requires ICE restart rather than
  WireGuard-style seamless roaming.

## Future Work (explicitly deferred)

- Prototype to measure direct-path success rate (home, office, cellular),
  connection setup time, and host throughput for `aiortc` vs. `pion`.
- Decide whether TURN is worth operating, or whether the Shore relay is a
  sufficient fallback.
- Specify audit reconciliation for direct-path operations.
- Reassess Option B for bulk data if mobile direct success is low.

## Consequences

- Good: removes the relay's head-of-line and message-size constraints for most
  sessions; faster files, diffs, and parallel requests.
- Good: LAN sessions never leave the local network; less data and trust
  surface on Shore.
- Good: clientless. Works from the existing browser/PWA with no VPN app.
- Good: one application protocol. Envelopes, capabilities, and handlers are
  shared with the relay path.
- Bad: meaningful complexity in ICE, fallback/migration logic, and signed
  fingerprint binding, plus a new host-side WebRTC dependency.
- Bad: does not materially reduce Shore cost. Bandwidth is already unmetered,
  and Shore remains required for pairing, signaling, fallback, and push.
- Bad: a new authenticated ingress path to the host that must be held to the
  same security review bar as the relay.
- Bad: audit continuity needs new design, since direct traffic bypasses relay
  receipts.

## Related decisions

- ADR-0039: Remote access via Shore relay (baseline transport, pairing, audit).
- ADR-0040: Versioned realtime protocol over WebSocket (envelope and message
  types carried on both paths).
- ADR-0050: Independent Shore web-client distribution (client integrity the
  direct path depends on).
