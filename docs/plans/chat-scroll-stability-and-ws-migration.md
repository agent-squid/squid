---
status: in-progress
date: 2026-11-15
updated: 2026-11-15
---
# Chat scroll stability, then WS-only migration

## Handoff and authorization

The user wants delayed automatic scroll movement fixed and every Chat-tab
application operation moved to WS. The recommended sequence is essential fixes
first, full transport migration second. Targeted stabilization has been
implemented and tested as recorded below. Full WS migration remains pending;
this document does not authorize publication.

Read the current session's workspace instructions before editing. Squid isolated
worktree paths change each turn: use the code roots provided by the new session,
not paths from old messages. Do not edit backing checkouts during an edit turn.
Publication requires a separate review and the applicable Squid publication/Git
workflow; this plan is not publication authorization.

## Current implementation and next steps (2026-11-15)

Phase 1 is partially implemented; do not restart from the original investigation
below or treat all validation gates as complete. The latest ordering fix is in
`ui/app.js`; current PWA asset versions are `20261115-050`.

Implemented:

- Automatic startup, foreground, view-switch, network, and 15-second checks no
  longer fetch/rebuild HTTP history. Connection probes now use local WS
  `health.check`; the browser's 15-second HTTP probe timer is removed. WS
  subscription/close and heartbeat expiry drive liveness and reconnect;
  heartbeat delivery separately triggers bounded history verification. Failed
  prompt-history bootstrap is retried after a successful WS health reply.
  Startup/settings/provider metadata still have HTTP `/health` callers.
- Local `history.check` verifies exact rendered IDs, including bookmark/bad
  annotation membership and prompt-only rows. It uses correlated command
  replies, validates inputs, splits oversized batches, rejects stale-scope
  results, and captures reading position after each reply. A failed check or
  single oversized row offers explicit refresh. Snapshot absence alone is not
  deletion evidence. SSE turn streaming also enables WS lifecycle discovery.
- Captured bottom-following survives restoration. History reset and older-page
  compensation run after date dividers; WIP updates preserve following or
  scrollback. Explicit filter/reload loads its first page even when retained
  live rows leave the sentinel offscreen.
- Existing-row recovery replaces a response and its ancillary rows at their
  original sibling boundary. It must not pass metadata-only updates through
  new-completion sorting: that reproduced `[old response, new prompt, new WIP]`
  becoming `[new prompt, new WIP, old response]`, hiding the submitted prompt
  when bottom-following resumed. Prompt-only replacements retain their renderer.
- Local `history.page` and `history.around` handlers now reuse history logic with
  strict input validation, identity checks, bounded request IDs and full-envelope
  response sizing. Two reads per socket may run outside the receive loop; five-
  second timeout/error replies do not close the socket. Timed-out reads retain
  admission until the read finishes and discard late results. Receive and auth
  output readers persist across query-completion wakeups to prevent message loss.
  Browser history still uses HTTP; these handlers are preparation, not rollout.
- Newly completed/discovered rows still use completion-time ordering. History
  insertion around live turns preserves completed order when IDs disagree.
- A frontend/backend mismatch was confirmed separately: the old running backend
  rejected `history.check` as `unsupported_type` while `/health` succeeded.
  Restarting it resolved that probe. Host reachability is not proof of protocol
  compatibility or successful history recovery.

Recorded frontend validation: 18 Node UI tests, JavaScript syntax checking, and
79 browser checks passed across connection recovery, thinking/scroll, pagination,
and WS health/heartbeat behavior. These browser suites use mocked service
responses and socket fixtures against the UI; they are not live backend/Shore
integration tests. The browser checks include local submission followed by two quota-metadata updates
to the older response, a recovered live group, prompt/WIP identity and visibility,
footer counts, bottom gap, filtered recovery, and prompt-only reading position.
The latest backend query review passed 121 realtime, Shore capability and parity
tests, including completion-boundary races, invalid IDs, timeout/error handling,
frame limits and admission limits. These are separate test runs, not one full
end-to-end migration audit. Backend validation and earlier reproductions are
recorded in the
[postmortem](../postmortems/2026-11-15-transcript-scroll-drift.md).

Next work:

1. Complete the remaining Phase 1 gates: whole-presentation fit (bubble plus
   footer/tools/padding), late asynchronous layout, once-only tall-response
   reveal, reader intent beyond instantaneous geometry, date-boundary/newer
   pagination, and WIP expansion-control reachability. Existing coverage is
   evidence for specific paths, not universal completion of this policy.
2. Keep the older-response-after-new-submission regression as a release gate.
   The screenshot matches the reproduced ordering defect, but its exact
   production metadata-trigger field was not captured. The original delayed
   scroll recording also lacks a conclusive production callback trace.
3. Execute Phase 2's request inventory and migration. HTTP health metadata/bootstrap,
   history/search/navigation, status lookups and other application callers,
   plus SSE compatibility, remain migration debt. `history.check` is local;
   Shore recognizes but denies it under the existing `dashboard.read.v1` grant.
   Remote parity requires explicit capability design without widening grants.
   The same restriction applies to `health.check`, `history.page` and
   `history.around`. Use the [request inventory](ws-request-inventory.md) as a
   starting point; dynamic URLs and form/download ownership still need audit.
4. Review the final changes before any separately authorized publication.
   ADR-0056 remains proposed; incident closure and WS-only completion are not
   claimed by these fixes.

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

## Original investigation findings and limits

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
The tests that predated stabilization did not establish that these defects
were fixed. See the implementation and validation record above for subsequent
fixes; the original production transition remains untraced.

`syncConnectionHistory()` and its unconditional 15-second timer were introduced
in commit `2f382b8` on 2026-11-15. WS already owns live lifecycle delivery,
cursor replay, and snapshot recovery. HTTP history polling duplicates that role.

## Phase 1: essential stabilization

Keep this change reviewable separately from a protocol-wide migration. The
numbered items below are the original Phase 1 scope; use the status above to
distinguish implemented work from remaining verification.

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

### History query preparation

Implemented local correlated `history.page` and `history.around` handlers,
reusing the current history handlers with strict parameter validation and
bounded result sizes. Identity is required; invalid/oversized queries fail
explicitly. Shore recognizes the names without widening existing grants.
Browser history callers have not switched yet: transfer support and remote
capability parity remain rollout prerequisites. This is preparation for Phase 2,
not completion of the WS migration.

The [static browser call-site inventory](ws-request-inventory.md) records direct
fetch/EventSource calls, including non-Chat operations. Dynamic URLs and
helper/form/download ownership still require manual audit.

Initial query-preparation checkpoint: 114 realtime, Shore capability, and Shore
parity tests passed,
including identity rejection, HTTP result parity, invalid query types/bounds,
oversized results, and explicit Shore denials. Python compilation passed.

### Query handling publication review follow-up

Fixed a completion-boundary race: history-query completion must not cancel a
pending socket receive or authentication queue reader, since either can consume
a message after the wait snapshot. Both readers now persist until consumed (or,
for authentication output, the session changes). Request-ID validation also
rejects lone Unicode surrogates before constructing a UTF-8 reply.

Validation: 121 realtime, Shore capability and parity tests passed, including
deterministic arrival-at-completion tests for socket frames and authentication
output. Python compilation passed. This review covers the bounded history-query
preparation; browser HTTP removal, transfer support and remote parity remain
outstanding. No publication or backend restart was performed.

### Reproducing recorded validation

Run from the current isolated Squid code root, not an old turn's absolute path:

```sh
node --check ui/app.js
node --test tests/ui/*.test.cjs
uv run --no-project --with pytest --with pytest-asyncio --with-editable . python -m pytest tests/test_realtime.py tests/test_shore_capabilities.py tests/test_shore_realtime_parity.py -q
```

Run the focused browser suite from `tests/e2e`. With a fresh checkout, install
its locked dependencies and Chromium first:

```sh
npm ci
npx playwright install chromium
```

Ensure port 3456 is free before running: `playwright.config.js` serves this
worktree's UI there, but outside CI it reuses an existing server. An unrelated
server on that port can silently serve another checkout. The browser tests use
mocked HTTP/WS services; no running Squid backend is required.

Then run:

```sh
npx playwright test connection-history.spec.js thinking-header-spacing.spec.js history-pagination.spec.js --workers=3
```

The recorded counts above refer to earlier execution against the corresponding
implementation checkpoints. This documentation review checked source consistency
and document links; it did not rerun runtime tests. The focused suites do not
cover all application HTTP requests, remote query parity, transfer interruption,
or every Phase 1 scroll acceptance gate.
