---
status: accepted
date: 2026-05-26
---
# ADR-0011: Completed Response Bubbles Surface at the Bottom of the Chat

## Context and Problem Statement

When a response is sent to the server, a response bubble is created and inserted immediately
after the corresponding user bubble in the message list. For fast responses this is fine —
the user is watching and the response appears in context.

With parallel adhoc (`!`) execution (ADR-0010) the user can fire multiple queries and
continue chatting while they run. A response that takes minutes to complete ends up deep
in scroll history by the time it finishes. The user has no natural way to notice it without
manually scrolling up.

## Considered Options

**A. Keep response in place; no notification**
Works for synchronous use. Fails for async: the user must manually hunt for the finished
response.

**B. Keep response in place; show a "ready" chip at the bottom**
A dismissible notification (`↑ Response from #topic@alias ready`) scrolls to the response.
Preserves positional pairing of prompt and response.

**C. Move the completed response bubble to the bottom on `done`**
Streaming still happens in the original position (progress is visible if the user happens to
be watching). When the server signals `done`, the bubble (and its stats row) are re-appended
to `#messages`, surfacing it as the newest item. The response header already contains the
topic tag and a truncated prompt, making it self-contained.

## Decision Outcome

**Option C** — move on `done`.

The response bubble header (`#topic@alias  prompt…`) identifies its origin without needing
positional proximity to the user bubble. The frozen thinking bubble (status text) remains
at the original position as a breadcrumb showing where the request was initiated.

At `done`, `messages.appendChild(bubble)` and `messages.appendChild(statsEl)` (if present)
relocate both elements. `scrollToBottom()` scrolls the view only if the user is already
near the bottom, avoiding interrupting active reading.

The `finally` block's `addTimestamp(bubble, doneTime)` naturally appends after `bubble` in
its new position.

## Consequences

- Good: async responses always surface where the user is looking
- Good: no new UI component needed (notification chip, badge, etc.)
- Good: self-contained response header makes positional decoupling legible
- Neutral: during streaming the bubble is in its original (possibly off-screen) position;
  only the completed result moves — streaming progress is not visible for long-running queries
- Neutral: the frozen thinking bubble remains as a positional breadcrumb, which may look
  like an orphaned element without its paired response
- Bad: breaks the traditional prompt-response visual pairing for synchronous use; the response
  always moves to the bottom even when the user was watching it stream in place
