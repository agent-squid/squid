# Plan: Shore Durable Object write budget

**Status:** Implemented and released. Phases 1–3 shipped in Shore `b89478c`
and AgentSquid `v0.1.6rc12`. Phase 4 item 11 and Phase 5 items 12–14 shipped
in Shore `b62082c` and AgentSquid `v0.1.6rc13`. Two items are deferred: 10
(optional, no DO saving) and 15 (a billing decision). Re-measurement is
pending: dev was at its 2026-09-25 quota when these shipped. Replace the
"Expected effect" estimates with a full day of observed data.

Companion to [ADR-0039](../decisions/0039-remote-access-via-shore-relay.md)
("Traffic accounting and capacity forecast", "Receipt-chain scope and
processing rules"). The ADR owns the decisions; this plan owns sequencing and
verification.

## Problem

On 2026-09-25 `dev.agentsquid.ai` exhausted the Workers Free Durable Objects
limit of 100,000 rows written per day. Every Durable Object call then failed
(`Exceeded allowed rows written in Durable Objects free tier`), including the
`IdentityIndex` lookup behind `/@user/auth/security`. The hosted client
reported this as "Could not reach your Squid host."

Cloudflare GraphQL analytics (`durableObjectsPeriodicGroups`,
`durableObjectsInvocationsAdaptiveGroups`) for that day:

- One `Account` object performed all 119,084 row writes. Daily writes had been
  about 250 on 2026-09-18..21, 22,375 on 09-24, and 119,084 on 09-25, after
  relay receipts were re-enabled in preproduction.
- Idle hours: about 260 inbound messages and 250 row writes per hour, with no
  user activity.
- Active hours: 7.5 row writes per inbound message (18:00 UTC: 5,796 messages,
  43,869 rows).

Every storage `put`/`delete` and alarm write is a billed row write. Measured
write sources, per unit:

| Source | Writes | Cause |
| --- | --- | --- |
| Receipted relay envelope | ~5 | receipt chain tip, idempotency record, `receipt-seq` index, audit event, audit chain tip |
| Audit export scheduling | ~1 per append | export alarm armed 1s after each audit append; export + compaction deletes |
| Traffic metric | ~1 per frame burst | `traffic:<minute>` flushed 100ms after frames |
| Host lease heartbeat (~15s) | ~2 | counted as traffic (flush) and `scheduleSocketAlarm` re-set the alarm |
| Browser `ack` (every 5s per open tab) | ~7.5 | re-sent even with an unchanged cursor; each is a receipted envelope |
| Host `ping` / browser `pong` (20s) | ~7.5 each | receipted envelopes |

Roughly 75–85% of active-hour messages were host→browser pushes.

## Target

- Idle host, no tab: near zero writes.
- Idle open tab: near zero writes.
- Active streaming: a few hundred writes per hour.
- A heavy dev day: under ~5,000 writes (5% of the free daily limit).

## Phase 1 — Shore storage amplification (done)

No protocol change.

1. Lease heartbeats return without traffic accounting and arm the alarm only
   when no earlier alarm exists (`armAlarm`), instead of re-setting it through
   `scheduleSocketAlarm` on every heartbeat.
2. Traffic minutes are accumulated in memory and persisted at most every 10s
   (`TRAFFIC_FLUSH_INTERVAL_MS`); `alarm()` and `/internal/state` flush
   immediately.
3. Audit export and compaction run 30s after the first pending append
   (`AUDIT_EXPORT_DELAY_MS`) instead of 1s, well inside the five-minute
   export-lag alert.

Verification: `npm test` in Shore. The traffic-metrics test asserts heartbeats
are not counted, and a heartbeat test asserts an earlier pending alarm is not
rewritten.

## Phase 2 — Browser acks (done)

4. `ShoreDashboardSession` sends `ack` only when the applied cursor advanced
   since the last ack on the connection, on a 30s interval (was 5s,
   unconditional). Liveness is unaffected: the host's device ping/pong carries
   it, not acks.

Verification: `browser/` vitest ("acks only when the applied cursor
advances"). Ships with the next Shore web-client release.

## Phase 3 — Receipt scope: browser→host only (ADR-0039 amendment, done)

The largest remaining cut. Needs the ADR-0039 amendment below, a
`shore-protocol-v1.md` change, and a security review note.

5. Receipt only browser→host ordinary envelopes. Those are the only envelopes
   that can drive host behavior, a browser cannot opt out, and the direction is
   already in the cleartext routing shell. No sender-declared "mutating" flag:
   Shore cannot verify it and it leaks the read/write split.
6. Host→browser envelopes carry no receipt and no per-frame Shore audit event.
   Without the second part the non-receipted branch still writes an audit event
   plus chain tip (`this.audit("relay_frame_received", …)`) for every frame.
   The host's audit records each receipted inbound request and its decision
   (outbound frames are consequences of those); E2E signatures prevent injection;
   the browser cursor detects loss. Per-minute traffic metrics still account for
   volume.
7. Remove `relay_receipt_ack`. Receipt sync covers the inbound chain only.
8. Gated on the host header `x-shore-receipt-scope: browser_to_host`
   (`Meta.inboundReceiptsOnly`), so a host that still expects outbound acks is
   not stalled during the version handoff. The new host still stages acks from
   an older Shore.
9. Receipted envelope writes cut from 7 rows to 4. The pre-forward
   `relay_frame_received` audit event stays: it is the relay-chain record the
   B2 export requires (`shore-security-operations.md`), and it is written in
   the same transaction as the receipt. Two other writes go instead:
   - No separate `relay-receipt-tip` row. The tip is the highest
     `relay-receipt-seq` index entry (`receiptTip`). The index is backfilled
     before the first allocation.
   - No `relay_frame_outcome` event (2 rows, plus a compaction delete) when a
     receipted frame is forwarded. Its acceptance is already in the pre-forward
     event and the host chain records the receipt. Failed forwards
     (`backpressure`, `send_failed`, `peer_offline`) are still audited.
   Remaining per receipted frame: idempotency record, seq index, audit event,
   audit chain tip.

Verification: Shore `npm test` ("relays host envelopes without receipt or audit
when the host scopes receipts to browser_to_host" asserts no ack and no new
`audit:`/`relay-receipt-*` keys; "wraps browser deliveries…" asserts a forwarded
receipted frame writes only `relay_frame_received`; allocation tests assert no
tip row); Squid `tests/test_shore_*.py`.

## Phase 4 — Squid host framing and ping interval

10. Deferred: coalescing outbound dashboard events per 100–250ms window into
    one envelope. After Phase 3 this saves bandwidth and CPU, not DO writes,
    and it would need a new batched frame type in `shore-protocol-v1.md`.
    Revisit only if relay bandwidth or frame-rate limits become the constraint.
11. Done: the host pings each subscribed device every 45s on the Shore path
    (`_SHORE_DEVICE_PING_SECONDS`), not the direct path's 20s, and evicts it
    after 90s of silence. The upper bound is Shore's 60s browser-socket
    heartbeat deadline, because an idle tab's pong is its only frame; 60s would
    need a Shore deadline change and Shore to deploy first. An idle open tab
    drops from 180 to 80 receipted pongs/hr (~720 to ~320 writes/hr). Documented
    in `shore-protocol-v1.md` "Per-device push liveness and backpressure".

## Phase 5 — Guardrails

12. Done: Shore test "Durable Object write budget" instruments storage
    (`put`/`delete`/alarm writes, including inside transactions) and asserts
    0 writes for host lease heartbeats, exactly 4 for a receipted
    browser→host frame, 0 for a scoped host→browser frame, and 0 while idle.
    It runs in CI with `npm test`.
13. Done: the Shore workflow `do-write-budget.yml` runs every 2 hours and on
    demand. It executes `scripts/check-do-writes.mjs`, which sums today's UTC
    `rowsWritten` and fails the run (GitHub notifies) at 50% of 100k. The query
    is account-wide, and dev and prod share one Cloudflare account, so this
    one job covers both. It uses the `shore-dev` `CLOUDFLARE_API_TOKEN`, which
    has Account Analytics:Read, and can also run locally with any token that
    has that scope.
14. Done: `loadAuthenticatedShoreRoute` reports a 5xx from `/auth/security` as
    `shore_service_unavailable`, and the hosted client says the relay is having
    a problem and the host is not at fault.
15. Deferred: Workers Paid on `shore-dev` as a safety net (billing decision).

## Expected effect

| | 2026-09-25 | After 1–2 | After 1–3 |
| --- | --- | --- | --- |
| Idle, no tab | ~250/hr | ~0–60/hr (socket alarm) | same |
| Idle tab open | ~6.7k/hr | ~1.3k/hr (20s ping/pong) | ~0.7k/hr; ~0.3k/hr with item 11 (4 writes per pong) |
| Active streaming | 7–44k/hr | roughly halved | a few hundred/hr |
| Day like 2026-09-25 | 119k | ~40–60k | ~2–5k |

Re-measure with the GraphQL queries after each phase deploys and replace these
estimates.
