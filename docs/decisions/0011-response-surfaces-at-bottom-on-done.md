---
status: accepted
date: 2026-05-26
updated: 2026-05-28
---
# ADR-0011: Completed Response Bubbles Surface at the Bottom of the Chat

## Context and Problem Statement

With parallel adhoc (`!`) execution (ADR-0010) the user can fire multiple queries and
continue chatting while they run. A response that takes minutes to complete ends up deep
in scroll history by the time it finishes. The user has no natural way to notice it without
manually scrolling up.

## Considered Options

**A. Keep response in place; no notification**
Works for synchronous use. Fails for async: the user must manually hunt for the finished
response.

**B. Keep response in place; show a "ready" chip at the bottom**
A dismissible notification (`↑ Response from #topic@agent ready`) scrolls to the response.
Preserves positional pairing of prompt and response.

**C. Move the completed response bubble to the bottom on `done`**
The response bubble is withheld from the DOM entirely during streaming. When the server
signals `done`, the bubble is appended to `#messages` with fully rendered markdown,
surfacing it as the newest item at the bottom. The response header (`#topic@agent  prompt…`)
makes it self-contained without positional proximity to the user bubble.

During streaming, content is shown as a live plain-text preview inside the thinking bubble
(a scrollable `max-height` area), giving the user progress visibility without a jumping
bubble. The thinking bubble collapses to a toggle on `done` if status/tool events were
present, or is removed entirely if not.

## Decision Outcome

**Option C** — bubble deferred to `done`.

The response bubble (`const bubble`) is created immediately on send but not inserted into
the DOM until `done`. All content chunks are accumulated in `raw` and shown as a live
preview in the thinking bubble via `updateThinkingPreview()`. At `done`:

1. `freezeThinking()` — collapses or removes the thinking bubble
2. `contentDiv.innerHTML = marked.parse(raw)` — renders final markdown
3. `messages.appendChild(bubble)` + `messages.appendChild(statsEl)` — surfaces at bottom
4. `scrollToBottom()` — scrolls only if user is already near the bottom

Error and reconnect paths (`showError`, `showStoredResponse`) explicitly append the bubble
to the DOM before populating content.

## Consequences

- Good: async responses always surface where the user is looking — no jumping
- Good: parallel responses complete in completion order at the bottom, never mid-stream
- Good: thinking bubble provides live progress for long-running queries without a displaced bubble
- Good: no new UI component needed
- Neutral: the frozen thinking bubble remains as a positional breadcrumb at the prompt site
- Bad: breaks traditional prompt-response visual pairing; response always moves to bottom
- Bad: full markdown rendering deferred to `done` — no incremental markdown during streaming
