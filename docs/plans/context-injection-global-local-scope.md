# Plan: Context injection — global shelf vs local (per-route) arming

Design-settling doc. Once the open questions here are resolved, distill the
result into an ADR (`docs/decisions/`); this plan then drives implementation.
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
- **Local** — armed per route. Only locally-armed shelf items are candidates
  for injection. Pinning/attaching arms the item for the route you're on;
  arming it on any other route is an explicit action from the ctx panel.

Delivery guard stays **per session** (`injectedInto`, unchanged).

## Model (settled)

Route identity for local state: `#topic@agent` plus mode — the session route
and the adhoc route of the same `#topic@agent` are **separate** local scopes
(`!N` depth is a window parameter, not part of the key). Rationale: adhoc is
a different context need — quick one-shot questions — and conflating it with
the session route would leak session arming decisions into one-off turns.

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
- **Deselect (OPEN — see below):** today, deselect is per-turn. Proposal:
  persist the exclusion locally for the adhoc route; it applies while the
  item is in the window and becomes inert once the item rotates out (window
  only moves forward). Toggling the item back on clears the exclusion.

## Open questions (settle before ADR)

1. **Window deselect persistence.** Recommended: persist per adhoc route as
   above — re-deselecting every turn is the worse failure mode, and the
   exclusion self-retires when the item leaves the window, so no stale state
   accumulates. Alternative: keep per-turn deselect (window purity, but
   repetitive). Storage is trivial either way (ids only); prune exclusions on
   `/clear` of the route.
2. **Broadcast / multi-head routes.** Armed sets are per head
   (`#topic@agent` per target). The ctx badge already clamps to boolean
   presence per category for broadcast (`updatePinCount`, `ui/app.js:15620`);
   panel display for mixed armed states across heads needs a decision:
   per-head grouping vs. a single "armed somewhere" indicator.
3. **Migration of existing behavior.** Today every shelf item auto-injects
   into fresh sessions. After the change, the first send on each route
   injects nothing unless armed. Clean break (no auto-arming migration —
   that would recreate the bug), but the empty-badge-on-upgrade moment needs
   the shelf to be visibly populated so users discover the arm action.

## UI implications

- **Single active-count badge.** The ctx badge counts only what will actually
  leave with the next send: locally-armed pins + armed files + window-selected
  items + memory when selected. Unarmed shelf items contribute nothing.
  (This changes `updatePinCount`, which today falls back to the shelf count
  when nothing is selected.)
- **Shelf visibility without a second badge:** when the shelf is non-empty but
  nothing is armed for this route, show the pin button's count dimmed — one
  color change, not a second number. Inactive items live in the panel, dimmed,
  with an arm toggle.
- **Ctx panel:** two groups — "This route" (armed, active) and "Shelf"
  (global, dimmed until armed). Window rows marked as auto (window) vs manual
  (locally armed); persisted deselect shows as Off and stays Off across turns.
- No system modals; panel uses the existing themed popover.

## Implementation sketch (after questions settle)

All in `ui/app.js` + `ui/style.css` (PWA version bump in the 5 spots):

1. New persisted maps: `routeArmed` — route key → [ids/paths]; `routeExcludes`
   — adhoc route key → [ids]. Route key helper shared by all three plus the
   existing pending-delivery maps.
2. `_injectablePinnedIds` / `_attachedFilesState`: filter shelf by armed
   state for the current route instead of "everything not yet delivered".
3. Pin/attach actions: shelf add + arm for current route.
4. `_activeLookbackItems`: apply persisted `routeExcludes` instead of the
   per-turn `_lookbackUnselected` (subject to open question 1).
5. Panel regrouping + dimmed-badge state; arm/disarm toggles.
6. `updatePinCount`: active-only count + dimmed shelf indicator.

## Verification

- e2e (Playwright, alongside the existing lookback specs in
  `tests/e2e/chat.spec.js`): route switch injects nothing unarmed; pin → send
  on same route still injects once; adhoc re-sends armed items each turn;
  `!2` window deselect persists across turns (if Q1 accepted) and self-clears
  when the item rotates out; memory still auto-injects on a fresh route.
- Visual check of panel groups, dimmed badge, and armed/unarmed row states on
  desktop and mobile widths.
