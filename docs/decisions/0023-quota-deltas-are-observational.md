---
status: accepted
date: 2026-06-19
---
# ADR-0023: Quota Deltas Are Observational, Not Per-Prompt Attribution

## Context

For backends that expose an account quota percentage, Squid samples the value
before a prompt starts and again shortly after it completes. The difference is
stored on the message and session and displayed as a quota signal.

The provider value is account- or backend-wide. It is not scoped to a Squid
message, process, topic, or session. Provider APIs can also report usage after a
delay, and Squid shares quota-fetch state across concurrent requests to the same
backend.

When prompts overlap, their sampling windows overlap. For example:

1. Prompt A starts at 10%.
2. Prompt B starts at 10% while A is still running.
3. B completes at 12% and records +2%.
4. A completes at 15% and records +5%.

The account consumed 5 percentage points, but the message deltas sum to 7
because B's usage is also inside A's observation window. Depending on completion
order and provider reporting delay, a prompt can include another prompt's usage,
miss its own late-reported usage, or receive a cached snapshot while another
quota fetch is in flight.

Session quota snapshots are updated by `session_id`; a later turn in the same
session replaces the stored before/after pair. They are not an accumulated
per-session ledger.

## Decision

- Treat `quota_before`, `quota_after`, and `quota_delta` as observations of the
  backend-wide quota meter during a prompt's wall-clock interval.
- Do not describe a quota delta as exact per-prompt consumption.
- Token counts and backend-reported monetary cost remain the authoritative
  per-prompt usage fields when available.
- A quota delta is reasonably attributable only when no other work using the
  same backend overlaps the measurement window and the provider has updated its
  meter before the final sample.
- Aggregating overlapping quota deltas can double-count usage. Analytics and
  downstream API consumers must treat summed quota deltas as an estimate.

## Consequences

- The UI may continue showing quota deltas as a useful signal, but documentation
  and labels must not imply exact attribution.
- Parallel prompts remain supported without serializing work merely to improve
  quota accounting.
- Exact attribution would require provider-supplied per-request usage or a
  different accounting source; client-side global before/after sampling cannot
  derive it reliably.

