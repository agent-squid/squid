---
status: accepted
date: 2026-05-26
updated: 2026-08-14
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

## Update 2026-08-14: land on the new bubble's top, not past it, when it's taller than the viewport

Surfacing a bubble at the bottom (step 3 above) still leaves a problem for a response taller
than the viewport: `scrollToBottom()` (step 4) jumps straight to `messages.scrollHeight`, i.e.
past the whole bubble, landing on its tail rather than its head. The user has to scroll back up
to read it from the start — unnatural, since reading the rest of the transcript already scrolls
downward. The original (pre-ADR-0040-migration) behavior avoided this by accident: a
non-`force`d `scrollToBottom()` only moves `scrollTop` if already within 150px of the bottom, and
a tall bubble's insertion pushes that gap well past 150px, so historically the call was silently
a no-op — combined with the thinking bubble's own live-preview auto-scroll (`updateThinkingPreview`
→ `scrollToBottom()`) already having tracked the viewport to the thinking bubble's bottom edge
(exactly where the frozen thinking bubble hands off to the response bubble), the net effect was
"land on the new bubble's head." Commit `b4026ce` ("Harden realtime replay and stabilize chat
scrolling") changed the WebSocket-delivered-item path (`insertCompletedHistoryItem`,
`insertPendingHistoryItem`) from that conditional call to `scrollToBottom(true)` — an
unconditional jump to `messages.scrollHeight` — to fix a different, real bug (the conditional
left the view "stuck" inconsistently in some cases), but it traded away the head-first reading
behavior as a side effect for any tall response delivered through that path.

Two follow-up attempts:

1. **Clamp long responses to a ~10-line CSS preview with a "Show more" expand.** Rejected — this
   was a misreading of the request. The ask was to keep scrolling to the response's head so the
   user reads down through it like the rest of the transcript, not to collapse/truncate it behind
   a click.
2. **Compute a scroll offset to land on the bubble's top** (`scrollToRevealBubble`, added to
   `insertCompletedHistoryItem`/`insertPendingHistoryItem` only): `messages.scrollTop +=
   bubble.getBoundingClientRect().top - messages.getBoundingClientRect().top` when the bubble is
   taller than `messages.clientHeight`, else the old literal-bottom behavior (short bubbles land
   the same place either way). This is the shipped fix, but applied only to the WebSocket
   realtime-item path initially, it didn't cover the primary SSE send-flow completion paths
   (`showStoredResponse`, the inline SSE `done` handler) — both still called plain
   `scrollToBottom()`.

**Root cause of the "worked, then broke" report was a second, unrelated bug**, found while
writing the regression test: `sendMessage()` schedules two one-shot
`requestAnimationFrame(() => messages.scrollTop = messages.scrollHeight)` calls — one when the
thinking bubble first appears, one when the first content chunk arrives (`revealResponseBubble`)
— meant to follow the *thinking* bubble into view. If the whole turn (including `done`) completes
before the browser's next paint (fast/cached backends, or a mocked instant response in tests),
these stale callbacks fire *after* `scrollToRevealBubble` has already positioned the view on the
new bubble's top, and silently stomp it back to the literal bottom. Fixed by guarding both with
`if (!thinkingFrozen)` — `thinkingFrozen` is set by the time `done` finishes processing, so a
callback that fires late becomes a no-op instead of overriding the more-informed positioning.
This race is timing-dependent per browser event-loop/paint scheduling, which is consistent with
the original report reading as "worked on Chrome, broke on Safari, then broke again after
restart" — it was never a single stable behavior to begin with.

`scrollToRevealBubble` is now applied at every site that surfaces a *newly completed* response
bubble while the user was at the bottom: `showStoredResponse`, the inline SSE `done` handler,
`insertCompletedHistoryItem`, and `insertPendingHistoryItem`. It is not applied to
`appendHistoryItem`'s plain history-pagination path (scrolling up into old history should not
itself trigger a re-jump) or to the disconnected-tab polling fallback in `pollMessageStatus`
(that bubble is already visible and growing in place, not newly surfacing).

Consequences:
- Good: matches the original, currently-expected reading UX — new content is read top-down,
  same direction as the rest of the transcript
- Good: the stale-rAF fix removes a real, reproducible race independent of this scroll-target
  question — it could have caused other inconsistent-scroll symptoms too
- Neutral: `scrollToRevealBubble` still reads `getBoundingClientRect()`/`offsetHeight`
  immediately after DOM insertion, the pattern suspected (but not confirmed) as the original
  Safari-specific failure; only the stale-rAF race was confirmed and fixed here
- Bad: this repo's Playwright e2e suite (`tests/e2e/playwright.config.js`) has no WebKit
  project, and running it ad hoc via `--browser=webkit` fails on unrelated pre-existing
  route-mocking issues (confirmed against an untouched existing test, not this change) — so
  Safari itself still cannot be re-verified end-to-end from this environment
