# Plan: Context injection — global shelf vs local (per-route) arming

Design-settling doc. The design questions are resolved below; distill the
result into an ADR (`docs/decisions/`) before implementation. This plan then
drives implementation.
All state involved is client-side (`localStorage` + in-memory) — the server
only receives the final `pinned_ids` / `attached_paths` / `include_topic_memory`
in the chat payload, so no backend or protocol change is expected.

## Problem

1. **Accidental injection on route switch.** Pinned responses and attached
   files live in one global cart (`pinnedItems`, `attachedFiles` in
   localStorage) and are never removed except explicitly. The delivery guard
   (`injectedInto`: session_id → injected ids) is per *session*, so switching
   to a route whose session has never seen the cart makes every cart item
   "not yet injected" → all of it auto-injects on the next send. Users fire
   off stale context without noticing.
2. **Adhoc over-injection.** For adhoc targets, `_injectablePinnedIds`
   (`ui/app.js:15590`) returns every cart item whenever `lookback === 0`
   (there is no session to be guarded by), and `_attachedFilesState`
   (`ui/app.js:15607`) selects all attached files on every adhoc turn.
3. **Window deselect is per-turn.** In a `!N` adhoc window, deselecting an
   in-window item adds it to `_lookbackUnselected` (`ui/app.js:3873`), but
   that set is cleared on send and whenever the window's candidate set
   changes. Next turn the window re-selects the last N wholesale, so keeping
   an item out requires re-deselecting it every turn.

## Terminology (settled — use in code, UI copy, and the ADR)

Drop "sticky". Two scopes:

- **Global** — the shelf: `pinnedItems` + `attachedFiles`. Membership is
  route-independent and persists until explicit removal. Presence on the
  shelf never causes injection by itself.
- **Local** — armed per delivery head. Only locally-armed shelf items are
  candidates for injection. Pinning/attaching arms the item for the applicable
  current head(s); arming it on any other head is an explicit ctx-panel action.

Delivery guard stays **per session** (`injectedInto`, unchanged).

## Model (settled)

Local identity is the normalized delivery head `#topic@agent` plus mode — the
session route and the adhoc route of the same `#topic@agent` are **separate**
local scopes (`!N` depth is a window parameter, not part of the key). Rationale:
adhoc is a different context need — quick one-shot questions — and conflating
it with the session route would leak session arming decisions into one-off turns.
Route expressions, chains, and broadcasts are not scope keys; they resolve to
one or more delivery heads. Agent resolution must finish before the key is
built so `topic@_` cannot become a second scope for the same eventual target.

- **Session route:** armed items inject on the first turn that sends them,
  the per-session delivery guard suppresses them afterward. No redundant
  in-session injection — same guarantee as today.
- **Adhoc route:** every turn is a fresh session in the same route, so armed
  items inject on every adhoc turn. This is the desired behavior ("locally
  persistent for adhoc") and falls out of arm-per-route + deliver-per-session
  with no adhoc special case.
- **Route switch:** the new route's armed set is whatever was armed there
  before (initially empty). Carried shelf items sit inert. This kills the
  accidental send without a global on/off toggle to forget.
- **Pin/attach action:** adds to the global shelf *and* arms for the current
  route, so the pin-then-immediately-send flow keeps working in one click.
- **Cross-route carry (e.g. pin claude's output → send to codex):** explicit
  "arm here" action on the shelf item in the ctx panel. Never implicit.
- **Topic memory:** unchanged. It is *scoped* context (belongs to the topic)
  and revision-gated, so it keeps its auto-inject default — the rule is
  "scoped context defaults on, unscoped context (pins/files) defaults off
  per route", not a memory-specific exception.
- **Attached files:** same model as pins. Global file shelf, armed per route,
  delivered per session; adhoc injects armed files each turn.

## `!N` window (adhoc)

The window stays a pure dynamic query: membership recomputed per send, items
rotate out silently. Two local overrides, both scoped to the adhoc route:

- **Add:** manually selecting an out-of-window item arms it locally — exactly
  the same state as selecting a response on a session route. It then behaves
  per the model above (injects each adhoc turn until disarmed/removed).
- **Deselect:** persist the exclusion locally for the adhoc route. It applies
  while the item is in the window and becomes inert once the item rotates out (window
  only moves forward). Toggling the item back on clears the exclusion. Delete
  an exclusion after it demonstrably rotates out of a successfully computed,
  non-empty window; do not prune merely because history is loading or empty.

## Settled edge cases

1. **Window deselect persistence.** Persist it per adhoc head as described
   above. Clear the head's exclusions on `/clear`; retain its armed shelf
   items so the next fresh session receives deliberately armed context.
2. **Broadcast / multi-head routes.** Armed sets remain independent per head.
   The panel groups state by head rather than presenting an ambiguous "armed
   somewhere" toggle. The badge remains the existing boolean/clamped summary.
   Pinning a response arms it for the response's own head. Attaching a file
   while composing a broadcast arms it for every current broadcast head,
   because the pre-send attach action explicitly targets that broadcast. The
   panel supports all-head and individual-head file/pin toggles.
3. **Migration of existing behavior.** Today every shelf item auto-injects
   into fresh sessions. After the change, the first send on each route
   injects nothing unless armed. Use a versioned clean break: preserve the
   shelf, create no armed entries, and write a migration marker. Auto-arming
   old shelf contents would recreate the bug. The shelf remains visibly
   populated so users discover the arm action.
4. **Native shell and flow additions.** Native-shell sends must use the same
   local arming resolver; their current all-pins bypass would preserve the
   leakage bug. Explicit flow-provided `extraPinnedIds` remain an intentional
   bypass because the flow, rather than shelf state, selected them.
5. **Removal vs disarming.** Removing an item from the shelf removes it from
   every armed scope and relevant pending map. Disarming affects only the
   current scope and leaves the global shelf item intact.

## UI implications

- **Single active-count badge.** In its active state, the ctx badge counts only
  what will actually leave with the next send: locally-armed pins + armed files + window-selected
  items + memory when selected. Unarmed shelf items contribute nothing.
  (This changes `updatePinCount`, which today falls back to the shelf count
  when nothing is selected.)
- **Shelf visibility without a second badge:** when the shelf is non-empty but
  nothing is armed for this route, show the pin button's count dimmed — one
  color change, not a second number. Inactive items live in the panel, dimmed,
  with an arm toggle.
- **Ctx panel:** for a single head, two groups — "This route" (armed) and "Shelf"
  (global, dimmed until armed). Window rows marked as auto (window) vs manual
  (locally armed); persisted deselect shows as Off and stays Off across turns.
  Armed items already delivered to a session remain in "This route" with
  `in session · skip`; they do not contribute to the active badge. Broadcast
  routes group these controls by delivery head.
- No system modals; panel uses the existing themed popover.

## Implementation sketch

All in `ui/app.js` + `ui/style.css` (PWA version bump in the 5 spots):

1. Add one versioned persisted object, with validated arrays at its boundary:
   `contextScopesV1 = { armedPins, armedFiles, excludes }`. Each map is keyed
   by the normalized `topic@agent:session|adhoc` helper. Pin ids and file paths
   stay in separate maps rather than sharing an untyped `routeArmed` array.
2. Add one `resolveContextForTarget({ topic, agent, adhoc, lookback })` path
   that returns selected pins, files, window items, memory, pending state, and
   delivered state. Both send payload construction and the badge/panel consume
   this result so UI and delivery filtering cannot drift apart.
3. `_injectablePinnedIds` / `_attachedFilesState`: delegate to the resolver,
   which filters shelf membership by armed state before applying the unchanged
   per-session delivery guards.
4. Pin/attach actions: shelf add + arm for the applicable head(s), following
   the broadcast rules above.
5. `_activeLookbackItems`: apply persisted exclusions instead of the per-turn
   `_lookbackUnselected`.
6. Panel regrouping + dimmed-badge state; arm/disarm toggles.
7. `updatePinCount`: resolver-derived active count + dimmed shelf indicator.
8. Global removal prunes every scope; `/clear` prunes that adhoc head's
   exclusions but retains its arming.

## Verification

- e2e (Playwright, alongside the existing lookback specs in
  `tests/e2e/chat.spec.js`): route switch injects nothing unarmed; pin → send
  on same route still injects once; adhoc re-sends armed items each turn;
  `!2` window deselect persists across turns and self-clears when the item
  rotates out (but not while history is empty/loading); memory still
  auto-injects on a fresh route; `/clear` retains arming but clears exclusions;
  shelf removal prunes all scopes; native shell respects arming; explicit flow
  pins still bypass it; and cold-cache agent resolution does not create an
  alias scope.
- Broadcast e2e: response pin arms only its origin head; a pre-send file
  attachment arms all current heads; individual and all-head panel toggles
  produce the payload shown by the badge/panel.
- Visual check of panel groups, dimmed badge, and armed/unarmed row states on
  desktop and mobile widths.
