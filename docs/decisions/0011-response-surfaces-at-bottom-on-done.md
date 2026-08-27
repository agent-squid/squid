---
status: accepted
date: 2026-05-26
updated: 2026-08-23
---
# ADR-0011: Completed Response Bubbles Surface at the Bottom of the Chat

## Core Behavior

**Responses complete at the bottom, not at their submission position.** User prompts stay anchored where they were submitted. When a response finishes (on `done`), it appears at the bottom of the chat as the latest item, regardless of when the prompt was sent. This surfaces async/parallel completions where the user is looking — no hunting required.

During streaming, a live plain-text preview scrolls inside the thinking bubble, so progress is visible. Markdown rendering happens once, at completion, and the full response bubble is inserted at the bottom in one atomic handoff.

Prompts, thinking bubbles, and responses placement:

| Element | During streaming | At completion |
|---------|------------------|----------------|
| **User prompt** | Appears at submission position | Stays anchored (never moves) |
| **Thinking bubble** | Live plain-text preview at prompt site | Frozen (if has narrative) or removed |
| **Response** | Withheld from DOM | Inserted at bottom in completion order |

## Live vs. History Turns

“Live” and “history” describe how this browser page first observed a turn. They
do not describe the backend endpoint that most recently supplied fields for the
turn, whether the turn is currently pending, or which device submitted it.

A **client session** is the lifetime of one loaded page. An actual reload starts
a new client session. Backgrounding, sleep, temporary network loss, WebSocket
reconnect, and returning to the same retained page do not start a new client
session by themselves.

A **live turn** is first observed after that page loaded, through local composer
submission or realtime delivery. This includes a turn submitted from another
device and delivered to this page while it remains open. Its standalone user
prompt is a session-local breadcrumb: it appears at submission/discovery
position and remains visible after the response completes. Later status or
history data may enrich and reconcile the turn, but must not downgrade it to a
history turn during that client session.

A **history turn** is first observed from persisted transcript loading. Its
prompt appears only in the response header, not as a standalone user bubble. A
completed history turn renders as the self-contained unit
`[header with prompt][response][stats][tools]`.

Pending rows discovered by the initial history page are currently treated as
**recovered live turns**: the renderer creates a standalone prompt and resumes
watching them. This is a deliberate exception to the simple “anything returned
by `/history` is history” rule because the page is observing the remainder of
their lifecycle. If the desired product rule is instead “only turns first seen
after bootstrap may have standalone prompts,” this recovery behavior must be
changed in the renderer; documentation alone does not make that semantic true.

### When history is pulled

The client fetches persisted transcript pages in these situations:

- initial page bootstrap and scroll-up pagination;
- changing or clearing a history filter;
- leaving a bounded search/jump window and resetting to the latest page;
- opening and paging a bounded history window around a message.

Merely returning from background, sleep, or a network interruption does not
reload transcript history. The retained page resumes realtime transport,
process polling, and pending-turn recovery. Realtime replay, snapshots, and
per-message status recovery fill gaps without resetting the client-session
boundary.

Consequently, in a multi-device example:

- Mobile opening the thread in a newly loaded page sees persisted completed
  turns as history. Turns it observes afterward are live on mobile.
- A desktop page that remained loaded can observe mobile's later turns through
  realtime delivery; those turns are live on that desktop page too, even though
  desktop did not submit them.
- If desktop's page is actually reloaded or discarded and recreated, turns
  already persisted at that new bootstrap are history. Hibernate alone does
  not guarantee this; it depends on whether the browser retained the page.

## Decision Rationale

- **Good:** Async responses surface without scrolling or scanning; parallel completions appear in completion order at bottom, naturally readable.
- **Good:** Live preview (thinking bubble) shows progress for long-running queries without a displaced response bubble.
- **Good:** Single markdown render per turn, at completion, avoids flickering.
- **Tradeoff:** No traditional prompt-response visual pairing in real time; user must glance at thinking bubble to see the pending prompt. History view recovers this by embedding the prompt in the response header.

## Scrolling Behavior

When a completion surfaces at the bottom:
- If the user is at the bottom (within ~150px), the view scrolls to reveal the new response's top (not past it), so the user reads downward through it.
- If the user is scrolled up in history, the view anchor is preserved — no forced jump.
- Tall responses (taller than viewport) land on their top, not their tail, for natural top-down reading.

## Tests

See `tests/e2e/chat.spec.js` for validation:
- **`does not appear in DOM before done`** — Response withheld until `done` frame, thinking bubble shows live preview.
- **`appears at bottom of #messages on done`** — Response inserted at bottom, after stats.
- **`response taller than the viewport scrolls to reveal its top`** — Tall responses scroll to top (not tail) for natural reading.
- **`filter round-trip keeps a completed response and its live prompt in order`** — Prompts submitted in the current live session remain anchored through store-driven history reconciliation.
- **`filter round-trip keeps the newest live prompt at the bottom when history includes it as pending`** — Reconciliation preserves all current-session prompt breadcrumbs and keeps the pending turn newest.
