---
status: proposed
date: 2026-11-15
updated: 2026-11-15
---
# ADR-0056: Transcript scroll intent and completion footers

## Context

[ADR-0011](0011-response-surfaces-at-bottom-on-done.md) places completed responses
at the latest edge and reveals tall responses from their beginning. It does not
fully define following intent, footer visibility, or ownership of scroll changes
across asynchronous updates. These need one policy across the current direct-DOM
rendering paths.

The [ongoing scroll-drift investigation](../postmortems/2026-11-15-transcript-scroll-drift.md)
contains the incident reports, implementation analysis, reproductions, and
coverage gaps. This ADR specifies proposed behavior; it does not claim that the
runtime fully implements it or that the incident has been resolved. Targeted
implementation progress is recorded below; the ADR remains proposed.

## Proposed decision

### Preserve intent across mutations

There are three distinct positioning intents:

- **Follow latest:** keep the latest edge visible, including its footer.
- **Read an anchor:** keep the same message and viewport offset while surrounding
  content changes. If it disappears, preserve the nearest valid offset.
- **Reveal a completed response:** an explicit transition from following to the
  beginning of a response that cannot fit in the viewport.

Unsolicited navigation to a response is allowed only when a genuinely new
response completes and the reader was following the latest edge. An unchanged
snapshot, heartbeat, replay, or metadata update is not a new response. Adjusting
`scrollTop` to keep the same text stationary after a layout change is anchor
compensation, not navigation, and remains necessary while reading scrollback.

An explicit send, jump-to-latest button, or pull-up refresh can request following.
A deliberate reader scroll away from the edge selects a reading anchor. Jumping
to a historic message also selects a reading anchor. Background delivery,
reconnect, snapshot reconciliation, and layout changes must preserve intent.

A captured `atBottom: true` must remain following across ordinary mutations,
unless a response-reveal policy deliberately changes it or reader/navigation
input has occurred since capture. A delayed operation must not restore old
following state after the reader has scrolled away.

### Completion includes its footer

A completed presentation consists of the response plus its timestamp or stats
footer and associated tool blocks. Position it after inserting the synchronous
parts of that presentation.

- If the completed presentation fits in the viewport, reveal through its footer
  and retain following. Later footer growth must not hide the timestamp.
- If it does not fit, reveal the response beginning once, preserving ADR-0011's
  top-down reading behavior. The footer may then be below the viewport and the
  bottom button may appear immediately; that is expected.
- Do not first show a short response's footer and later move to a different
  target without a new reader action or an actual change that makes the
  completed presentation too tall. Do not repeatedly reveal a tall response's
  beginning when duplicate terminal events or snapshots arrive.
- Later asynchronous layout changes preserve the resulting intent: follow the
  edge in following mode, preserve the reading anchor otherwise.

The fit calculation must use the complete presentation and available scroller
space, including relevant gaps and padding, rather than bubble height alone.

### One policy for all paths

SSE completion, status fallback, WebSocket completion, recovered pending turns,
Flow responses, and snapshot reconciliation must use the same positioning rules.
History pagination and background connection messages must preserve reading
position. Initial bootstrap may follow until the reader interacts; subsequent
boot/recovery callbacks cannot override that interaction.

Keep geometry checks for measuring the viewport and rounding tolerance. Do not
use a layout-induced gap or programmatic scroll event alone to infer a new reader
intent. Use one documented tolerance for bottom-following decisions; gesture
recognition may retain a separate, larger edge threshold.

Ordering and scrolling are separate concerns. Preserve completed order by
`(completed_at, msg_id)` independently of any scroll adjustment. Scroll policy
must not change the authoritative completion order.

Distinguish **new completion placement** from **existing-row recovery**. A quota,
stats, footer, annotation, or stored-content refresh is not a new completion.
Replace an existing presentation at its original sibling boundary, preserving
its position relative to newer live prompt/WIP groups and client-only banners.
Do not remove it and feed it through the new-completion sorter. Restore position
only after its replacement footer/tool rows and date dividers are settled.
The same invariant applies to normal, filtered, and prompt-only history.

## Alternatives considered

- **Infer intent from the current bottom gap at every update.** Simple, but a
  layout change can create a gap without reader input and silently stop following.
- **Always follow streaming output and completion.** Keeps the latest content
  visible, but interrupts readers who deliberately moved into scrollback.
- **Preserve explicit following or reading intent across mutations (proposed).**
  Supports both uses, at the cost of tracking reader interaction and rejecting
  stale positioning work.

## Acceptance criteria

All delivery and recovery paths must preserve a chosen reading anchor, keep
short-completion footers visible while following, and reveal tall responses only
once. Layout growth, unchanged snapshots, and duplicate events must not select
new navigation targets. Validation must cover late layout changes and reader
input during pending work; detailed regression cases are tracked in the linked
investigation.

## Consequences

Following state must be distinct from instantaneous viewport geometry, and
positioning work must respect reader input that occurred after it was scheduled.
Completion measurement includes footer and ancillary rows. This requires shared
policy and browser coverage across the existing DOM paths.

The proposal retains ADR-0011's top-down reading behavior for tall responses.
It does not require reviving the abandoned reconciler in
[ADR-0041](0041-normalized-client-transcript-store-and-reconciler.md).
Acceptance and full runtime implementation remain outstanding.

## Implementation progress (2026-11-15)

The [stabilization plan](../plans/chat-scroll-stability-and-ws-migration.md)
and [postmortem](../postmortems/2026-11-15-transcript-scroll-drift.md) track
implemented paths and unresolved gates. Current code preserves captured
bottom-following, restores after synchronous history decorations, verifies
rendered rows through bounded local WS queries, and replaces existing rows in
place. Bookmark/bad and prompt-only recovery are covered. Automatic HTTP
history rebuilds have been removed. Connection probes use local WS
`health.check` with heartbeat-based liveness; startup/settings/provider health
metadata and other HTTP/SSE application traffic remain pending ADR-0040 migration.
Connection feedback preserves transcript position and does not rebuild history.
Prepared WS page/around query handlers have not replaced browser HTTP history.

The latest browser reproduction changed only an older response's quota metadata
after actual submission. Recovery incorrectly moved that response below the new
prompt/WIP. The correction preserves the original sibling boundary. Tests cover
both fresh local submission and a recovered live group, repeated metadata
updates, prompt/WIP identity, visibility, footer counts and bottom gap. Latest
recorded frontend validation passed 18 Node UI tests and 79 browser checks,
including later WS health/heartbeat checks, plus JS syntax checking. The separate
backend query review passed 121 tests; backend transport coverage does not
establish the remaining visual/reader-intent gates.

These targeted fixes do not establish complete footer-inclusive fit measurement,
reader intent tracking across every asynchronous layout path, WIP control
reachability, or incident closure. The precise production callback/metadata
sequence remains untraced. Retain those acceptance gates until demonstrated.
