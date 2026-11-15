---
status: planned
date: 2026-11-15
---
# Chat scroll stability, then WS-only migration

## Handoff and authorization

The user wants delayed automatic scroll movement fixed and every Chat-tab
application operation moved to WS. The recommended sequence is essential fixes
first, full transport migration second. This turn records that plan only;
implementation and publication have not been performed by this plan.

Read the current session's workspace instructions before editing. Squid isolated
worktree paths change each turn: use the code roots provided by the new session,
not paths from old messages. Do not edit backing checkouts during an edit turn.
Publication requires a separate review and the applicable Squid publication/Git
workflow; this plan is not publication authorization.

## Read first

- [ADR-0040: WS protocol and Chat scope](../decisions/0040-versioned-realtime-protocol-over-websocket.md).
  Its accepted 2026-11-15 revision requires WS-only application traffic. Older
  implementation/migration sections describe HTTP/SSE compatibility historically;
  do not treat them as target exceptions.
- [ADR-0039: Shore](../decisions/0039-remote-access-via-shore-relay.md).
  Direct and Shore Chat semantics must agree. Transport expansion must not widen
  capability grants, including currently read-only remote scopes.
- [Proposed ADR-0056: scroll policy](../decisions/0056-transcript-scroll-intent-and-completion-footers.md).
- [Ongoing postmortem and reproductions](../postmortems/2026-11-15-transcript-scroll-drift.md).
- [ADR-0011: response placement](../decisions/0011-response-surfaces-at-bottom-on-done.md).
- [ADR-0041](../decisions/0041-normalized-client-transcript-store-and-reconciler.md)
  is superseded: do not revive the abandoned reconciler as an assumed requirement.

## Current findings and limits

The final response initially appears with its footer visible, then seconds later
moves without reader input and displays the bottom button. WIP output can also
lose following, putting its bottom-right expansion arrow outside the viewport.

Confirmed investigations, with numerical results in the postmortem:

- Normal `sendMessage()` through mocked SSE completion followed by ordinary
  `syncConnectionHistory()` changed a bottom gap from 0 to 53 pixels and removed
  the live user prompt. This exercises the actual background-refresh path.
- Anchor restoration after synthetic footer growth changed a 0-pixel gap to 48
  pixels and cleared following intent.
- Repeated unchanged history resets with real timestamps moved the reading
  response by 40 pixels each time: date dividers were rebuilt after restoration.
- Review found a separate ordering blocker: per-item insertion around live turns
  by message ID can reverse completed order when completion times differ from
  submission order. Example: live 100; completed 110 at 10:00 and 90 at 12:00
  render as 90, live 100, 110 instead of preserving completed order 110, 90.

The untouched recording strongly matches delayed history replacement, but no
production callback trace has conclusively attributed its exact transition.
The earlier passing tests do not establish that these defects are fixed.

`syncConnectionHistory()` and its unconditional 15-second timer were introduced
in commit `2f382b8` on 2026-11-15. WS already owns live lifecycle delivery,
cursor replay, and snapshot recovery. HTTP history polling duplicates that role.

## Phase 1: essential stabilization

Keep this change reviewable separately from a protocol-wide migration.

1. Read current code before making changes; check for intervening fixes.
   Relevant areas in `ui/app.js` are `syncConnectionHistory`,
   `recoverForegroundState`, online/offline and startup hooks,
   `captureTranscriptPosition`, `restoreTranscriptPosition`, history loading,
   `refreshDateDividers`, WIP preview updates, and completion/reveal helpers.
2. Remove automatic HTTP history reconciliation from the interval, startup,
   foreground/pageshow, and network recovery. WS events, replay, and snapshots
   own automatic live recovery. Separate health/connectivity feedback from
   transcript replacement so status indicators still work. Do not silently
   replace polling with an automatic HTTP rebuild on another event.
3. Preserve following versus reading intent through necessary DOM mutations.
   Finish synchronous footer, tool, banner, and date-divider changes before
   restoring position. Apply this to reset and older/newer pagination paths.
   Do not convert layout-induced gaps into reader intent. Delayed callbacks
   must respect reader navigation that occurred after they were scheduled.
4. Preserve tall-response reading: reveal its beginning once when a new response
   completes while following; snapshots/duplicates must not repeat that jump.
   A short complete presentation includes its timestamp/stats and ancillary rows.
5. Verify WIP output keeps following while intended, and that its expansion
   control remains reachable. Read `.thinking-height-btn` and its overflow
   detection before choosing a minimal control/layout change.
6. Resolve the reviewed completion-order inversion before declaring the pending
   changes ready. Completed order is `(completed_at, msg_id)`; do not let pending
   placement reorder completed rows.

Phase 1 may retain existing bounded HTTP history/search and explicit navigation
requests temporarily. Record them as migration debt, not WS-only completion.
Do not claim that removing one timer completes the accepted transport decision.
Before UI edits, state the exact parent element (normally `#messages` for these
changes). Follow current cache-bump instructions for `ui/app.js`, `style.css`,
and `index.html`; inspect `sw.js` and `index.html` for all matching versions.

### Phase 1 validation gates

Add regressions that fail against the unfixed behavior:

- Complete a real submitted turn, then advance beyond the former polling period:
  no automatic history request/rebuild, footer loss, or unexpected bottom button.
- WS reconnect, foreground resume, and network recovery perform no automatic
  HTTP history reconciliation and preserve the selected reading position.
- Repeated history reconstruction with `timestamp` and `completed_at` preserves
  the same reading row, including same-day and date-boundary pagination.
- Followed WIP growth, footer growth, and viewport/composer changes retain the
  edge; deliberate scrolling away remains respected during subsequent output.
- Short completion includes its footer; tall completion is revealed once;
  duplicate snapshots and delayed metadata do not move the reader.
- Concurrent completion-time order disagrees with IDs and still renders correctly.

Existing starting points are `tests/e2e/thinking-header-spacing.spec.js`,
`connection-history.spec.js`, `history-pagination.spec.js`, and WS tests in
`chat.spec.js`; unit tests live in `tests/ui/*.test.cjs`. Some connection-history
expectations encode the polling design and need updating to the intended WS
behavior, rather than preserving the regression. Use deterministic protocol and
layout events, not just sleeps or assertions immediately after completion.
Run appropriate unit and browser checks; record exact results and limitations.

## Phase 2: audit and migrate every Chat operation to WS

1. Inventory all requests initiated by Chat, including panels/modals it opens,
   shared helpers, initial data, history/search/jumps, prompt/context controls,
   stats, attachments, tool/diff actions, and explicit refresh. Identify each
   request's direct handler, Shore capability, and existing WS equivalent.
   Include `fetch`, EventSource/SSE, polling helpers, form submissions, and
   application file/download URLs. Record the inventory and remaining work.
2. Reuse existing WS request correlation, typed errors, authorization, replay,
   and idempotency facilities. Add bounded query/result commands where missing.
   History-page cursors remain distinct from the durable event cursor.
3. Specify cancellation, timeout, stale-result suppression, and reconnect
   behavior. Query results from an old scope must not overwrite current Chat.
   Retries must not duplicate mutations or reinterpret grants.
4. Add chunking, backpressure, integrity/resume semantics, and scheduling for
   large transfers so they cannot starve live events or control messages.
5. Implement direct and encrypted Shore parity under the existing capability
   registry. Unauthorized operations fail explicitly; there is no HTTP fallback.
6. Remove migrated HTTP/SSE Chat callers and compatibility paths. Retire server
   routes only after auditing other clients and explicitly accounting for them.
   Document any remaining non-Chat application migration required by ADR-0040's
   broader target; do not silently expand a stabilization patch to cover it.

Static documents/assets and the opening handshake remain transport bootstrap.
Remote sockets remain secure (`wss://`); WS-only does not mean disabling TLS.

### Phase 2 validation gates

- A request audit/test fails if Chat makes any HTTP application-data request,
  including initial load, query, explicit refresh, file access, or recovery.
- Initial history, older/newer paging, search, jumps, commands, cancellation,
  attachment/tool actions, and refresh work through WS on direct and Shore paths.
- Reconnect/replay/snapshot rollover, expired credentials, capability rejection,
  query cancellation, stale responses, and transfer interruption are covered.
- All Phase 1 reading-position and completion-order regressions continue to pass.

## Documentation and review

Update the postmortem with implemented fixes, evidence, remaining unknowns, and
verification results. Update ADR implementation status accurately; ADR-0056
remains proposed unless explicitly accepted. Keep the two phases and their
remaining debt visible in review. Do not report either migration or incident
closure merely because the targeted tests pass, and do not publish without the
separate review requested by the user.
