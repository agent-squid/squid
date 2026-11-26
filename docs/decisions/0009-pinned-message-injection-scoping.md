---
status: accepted
date: 2026-05-26
updated: 2026-11-26
---
# ADR-0009: Pinned Message Injection Scoping by Mode

## Original Design (Superseded 2026-05-29)

The original design used a server-side `get_pending_injections` / `inject_history` mechanism
to push pinned messages into sessions automatically. Exclusion was performed server-side using
`exclude_session_id=resume_session_id` against `chat_messages.session_id`. This was removed
along with the `session_context_log` table (see [[0007-cross-session-injection-tracking]]).

## Current Design (Updated 2026-06-12)

Injection is **explicitly client-driven**. The client selects which pins to send and the
server injects them.

### Scoping by mode

| Mode | How pinned content enters | Same-session exclusion |
|---|---|---|
| Session turn (`--resume`) | Prepended as `<referenced_context>` block in `effective_message` | Client skips pins whose `session_id` matches `_sessionIds[topic@agent]` |
| Adhoc turn (`!`) | Prepended to `context_history` for `_build_prompt`, deduped against lookback window | Fresh context. Pins are eligible again once the prior send settles. |

### Client-side exclusion at send time

The client stores pins and their selected topic trees in localStorage. Selection applies
across sibling topics in a tree. For a resumed session, the client excludes pins whose
origin session matches the destination session, pins previously injected into that
session, and pins pending for the route. These exclusions preserve the saved selection.
A fresh turn can use a selected pin again after the prior send settles.

### Server-side injection

The server receives `pinned_ids: [id, ...]` in `POST /chat` and:

1. Fetches rows via `get_messages_by_ids(filtered)` — assistant messages, `status='done'`.
   Each returned pair also carries the origin `topic`/`agent` (needed to tell pins apart
   when several land in one turn, e.g. a Squid Flow join — see [[0032-route-chains-with-cwd-profile-agents]])
2. Deduplicates against the adhoc lookback window to avoid double-injecting context
   already in `context_history`
3. For **session turns**: builds `effective_message` with a `<referenced_context>` prefix;
   each pinned pair is labeled `From #topic@agent:` so multiple pins are distinguishable
4. For **adhoc turns**: prepends fetched pairs to `context_history` (unlabeled — the model
   just sees them as prior conversation turns)

### DB record at send time

`chat_messages.context` on the user message row stores:

```json
{"pins": [47, 52], "mem": true}
```

The user row records the context prepared for the turn, including pin IDs and whether
topic memory was included. The session lookup uses records associated with its session
ID to seed cross-device tracking. The request record alone is not a delivery
acknowledgement.

### Re-injection prevention

`injectedInto` in localStorage is keyed by **`session_id`** (not `topic@agent`).

- **On session load**: `GET /topics/{topic}/session` returns `injected_ids` — the union of
  pin IDs recorded on user prompts whose assistant row completed successfully with that session ID. The client seeds
  `injectedInto[session_id]` from this response, giving any device resuming the session
  immediate knowledge of what was already injected.
- **After a successful turn**: the client records the server-reported pin IDs in
  `injectedInto[session_id]` for eligible turns with a known session ID.
- **On session delete**: `injectedInto[session_id]` is cleared.

Keying by `session_id` rather than `topic@agent` ensures stale tracking from a prior session
never suppresses injection in a new one.

### Delivery confirmation and context cart

A submitted item can remain **Sending** after response streaming begins. The client
confirms delivery when it observes successful turn completion through the SSE `done`
event or a stored turn with status `done` through realtime or status recovery. The
first streamed text, request acceptance, and reported context IDs do not confirm
delivery. Failed or cancelled turns release pending state for retry.

| Cart state | Checkbox | Status |
|---|---|---|
| Eligible and selected | Checked, editable | |
| Manually deselected | Unchecked, editable | |
| Pin selected only in another topic tree | Unchecked, editable | Other topic |
| Response originated in or was injected into the current session | Checked, disabled | In session |
| Included in an in-flight send | Checked, disabled | Sending |
| File sent successfully | Unchecked, editable | Used |
| File missing | Unchecked, disabled | No file |

Files are staged within the current topic tree. A successful send marks a file **Used**
until the user selects it again. A failed send leaves the file selected. Missing files
remain in the cart and recover their prior selection when they become available again.

The cart button shows one badge for the active route. Ready items take priority,
followed by a pulsing sending count, then a grey selectable count. Switching topics,
agents, or between resumed and fresh mode recomputes the count. Returning to a route
with context still in flight restores its pulse. A staged file can stay locked in
sibling topics while contributing to the sending badge only on its submitting route.

Cancellation leaves context eligible for retry unless successful completion won the
race. A cancelled turn can retain a session ID for turn history. The session lookup
only counts successful turns as confirmed context delivery.

**Contract tests**: `tests/ui/context-cart.test.cjs`, `tests/e2e/pin.spec.js`

## Consequences

- Good: no server-side pending queue or cleanup logic
- Good: same-session exclusion is session-id–precise — a prior session under the same
  `topic@agent` is correctly treated as cross-session and injected
- Good: cross-device correctness for `injectedInto` state — DB is ground truth, seeded
  on session load
- Good: DB record (`chat_messages.context`) provides an audit trail of what was injected
- Neutral: pins themselves (the basket) remain device-local in localStorage — cross-device
  pin basket sync is not implemented
- Neutral: if `_sessionIds` has no entry for the current `topic@agent` (first turn of a new
  browser session before the session API responds), same-`topic@agent` pins are not excluded
  — they inject, which may be redundant but is not harmful
