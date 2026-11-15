---
status: investigating
date: 2026-11-15
---
# Transcript scroll drift: ongoing investigation

## Status and impact

Readers report movement while reading completed responses and streaming WIP,
loss of timestamp visibility, an unexpected scroll-to-bottom button, and a WIP
expansion control falling outside the viewport. Two mechanisms have been
reproduced in Chromium. The exact event sequence behind the production reports
remains unconfirmed. This is an ongoing investigation, not a completed incident
root-cause analysis; the scope of affected browsers and sessions is unknown.

The proposed behavior is recorded separately in
[ADR-0056](../decisions/0056-transcript-scroll-intent-and-completion-footers.md).
No runtime remediation is included in this documentation change.

## Investigation evidence

### Reported completion behavior

A final response appears with its timestamp visible below the bubble. Shortly
afterward, the timestamp disappears below the viewport and the scroll-to-bottom
button appears, without the reader scrolling.

The exact production event sequence has not yet been captured. A footer leaving
the viewport does not necessarily mean `scrollTop` decreased: content below the
reading anchor may have grown while `scrollTop` stayed constant.

## Current implementation and evidence

The relevant code is in [ui/app.js](../../ui/app.js):

| Path | Current behavior | Risk |
| --- | --- | --- |
| `isAtBottom()` | Bottom gap at most one pixel | Geometry is treated as a proxy for reader intent. |
| Scroll listener / `messagesPinnedToBottom` | Every scroll event copies `isAtBottom()` into the flag | Programmatic scrolling and reader scrolling are not distinguished. |
| Layout `ResizeObserver` | Follows the bottom when that flag is true | Observes viewport/layout elements; it does not settle every descendant's later growth. |
| `scrollToRevealBubble()` | Compares bubble height alone with viewport height | Timestamp, stats, tool blocks, gaps, and bottom padding are outside that measurement. |
| SSE `done` / status fallback | Captures position, removes WIP, renders response and footer, then positions | Correct first placement is not a guarantee about subsequent refresh/layout work. |
| `replacePendingWithStoredItem()` | Captures before replacing WIP, then reveals or restores | Another transport path must obey the same intent policy. |
| `captureTranscriptPosition()` | Captures `atBottom`, a visible anchor, and its offset | Captures both following and reading state. |
| `restoreTranscriptPosition()` | Restores the anchor or numeric offset, then recomputes pinned state | Does not use the captured `atBottom` flag. Footer growth can turn following into scrollback. |
| `syncConnectionHistory()` | Background resets use `followLatest: false` | Even a previously bottom-following reader is sent through anchor restoration. |
| `setAssistantMarkdown()` / Mermaid | Diagram rendering can run after insertion | Layout can change after completion placement. Mermaid uses its own two-pixel bottom tolerance. |
| `updateScrollButtonVisibility()` | Shows when not geometrically at bottom, or in a history window | A visible button alone does not prove a reader opted out of following. |

`#messages` disables native scroll anchoring with `overflow-anchor: none` in
[ui/style.css](../../ui/style.css). Application code is responsible for preserving
the viewport across these mutations.

### Confirmed helper-level reproduction

A headless Chromium investigation on 2026-11-15 used the current application,
waited for history/bootstrap/banner settlement, inserted an overflowing assistant
row and timestamp, and then:

1. Called `scrollToBottom(true)` and `captureTranscriptPosition()`.
2. Increased the timestamp row's height by 48 pixels, without reader input.
3. Called `restoreTranscriptPosition()` and `updateScrollButtonVisibility()`.

The result was:

```json
{"capturedAtBottom":true,"before":0,"after":48,"pinned":false,"bottomButton":true}
```

`before` and `after` are bottom gaps in pixels. This confirms that anchor
restoration can discard bottom-following intent after footer growth. It is a
synthetic layout reproduction, not proof that this exact footer mutation or
callback caused the reported production incident. No runtime fix was included
with the investigation.

### Confirmed upward drift during date-divider reconstruction

A second headless Chromium investigation on 2026-11-15 installed three long,
completed history messages with both `timestamp` and `completed_at` on the same
day. After scrolling to the middle response, it called
`resetHistoryToLatest({ data, followLatest: false })` three times with identical
history. No response was added and no reader input occurred between resets.

| Sample | `scrollTop` | Reading response top | At bottom |
| --- | ---: | ---: | --- |
| Before reset | 2213 | 68.375 | false |
| First reset | 2173 | 108.375 | false |
| Second reset | 2133 | 148.375 | false |
| Third reset | 2093 | 188.375 | false |

Each reset moved the reading response down by 40 pixels, equivalent to scrolling
up by 40 pixels. The code restores the anchor before calling
`refreshDateDividers()`. Old date dividers are removed with the history rows;
restoration measures the replacement message without its divider, and rebuilding
the divider afterward adds height above the restored anchor. Native anchoring is
disabled, so nothing compensates for that later insertion.

`loadHistory()` and the older-page path in `loadHistoryWindow()` likewise apply
scroll-height compensation before rebuilding date dividers. Their exposure
includes pagination while reading. Existing rebuild tests use fixtures without
`timestamp`, whereas `refreshDateDividers()` selects `.history-item[data-ts]`;
those tests therefore omit this source of layout movement.

This proves a reconstruction defect, not that an idle timer repeatedly resets
scrolled-up history: `syncConnectionHistory()` normally defers changed-history
rebuilds when the reader is away from the bottom. It can perform the defective
reset when the reader is at the bottom, and the pagination paths can run while
reading history. Identifying the trigger for the user's exact incident still
requires a trace.


### Untouched recording and actual background-sync reproduction

The 16-second recording `Screen_Recording_20261115_141834_Chrome.mp4` shows
completion around 9–10 seconds, with its footer visible, followed around
11–12 seconds by a second viewport change and the bottom button appearing.
The separate live user-prompt bubble disappears during the second transition,
consistent with replacement by responses-only fetched history. No touch is
visible during that transition. The recording establishes the delayed movement,
but cannot identify a JavaScript callback by itself.

A mobile-sized Chromium check then submitted a real UI `sendMessage()` through
a mocked SSE meta/content/done response and invoked the normal
`syncConnectionHistory()` with the completed turn and earlier history. It used
no synthetic footer growth and did not force a latest jump. The result was:

```json
{"before":{"top":1059,"gap":0,"responseTop":502.59375,"users":1},"after":{"top":1018,"gap":53,"responseTop":447.78125,"users":0}}
```

The refresh removed the live prompt and left a 53-pixel gap after a previously
bottom-pinned completion. The direction and displacement of individual rows in
this fixture do not exactly match the recording; they depend on the replaced
history geometry. This confirms the normal refresh path can produce a second
movement and lost bottom-following after completion.

The relevant chain is `syncConnectionHistory()` detecting a changed snapshot,
`resetHistoryToLatest({ followLatest: false })` replacing the live presentation,
and anchor restoration instead of retaining the captured following state. Date
dividers are also reconstructed after restoration. The periodic sync runs every
15 seconds independently of completion, so it can land seconds after a response
appears. The recording is consistent with this chain; a runtime trace is still
needed to attribute its exact transition conclusively.

## Why earlier tests missed these cases

The new submission-order cases use IDs that increase with completion time, so
passing them did not rule out completion-order inversions. The rebuild-position
fixtures omit the `timestamp` field that creates date dividers, so they do not
exercise the layout change reproduced above. Initial completion placement and
later asynchronous layout settlement are separate assertions; the former alone
cannot establish stability while reading.

## Follow-up and verification

- Capture the production event sequence, identifying each scroll writer and
  layout mutation before attributing the report to a specific reproduced defect.
- Restore position after date-divider construction and other synchronous layout
  mutations; cover repeated resets and pagination across date boundaries.
- Preserve following intent across footer growth while respecting subsequent
  reader input; test equivalent transport and recovery paths.
- Address the separately reviewed ID-versus-completion-order inversion.
- Verify WIP expansion-control reachability while output grows.

These are outstanding actions, not completed fixes.

## Regression coverage to add

Existing tests cover WIP growth/removal, submission from scrollback, viewport
shrink, history-anchor restoration, and prompt placement. They do not establish
the full policy above.

Add browser coverage that observes both initial placement and later mutations:

- Short completion with timestamp visible, followed by a changed HTTP snapshot,
  footer growth, and a connection-status update: bottom gap remains within the
  agreed tolerance and the bottom button stays hidden.
- A response whose bubble fits but whose presentation including footer does not:
  choose a single, deterministic initial target.
- Tall completion: reveal the beginning once; a later duplicate event, viewport
  change, or snapshot must not jump to the tail or re-reveal the beginning.
- Reader scrolls away between completion and delayed work: preserve that new
  anchor and never restore an earlier following intent.
- Late stats, tool blocks, images, and diagrams; mobile keyboard opening/closing;
  pagination above the viewport; and foreground recovery.
- SSE, WebSocket, and polling/status fallback with equivalent final results.
- Repeated unchanged history reconstruction with real `timestamp` fields and
  date dividers: the same reading response stays at the same viewport offset.
  Include same-day pages and pages crossing a date boundary.
- Concurrent completions whose IDs do not match completion-time order.

Assertions should include bottom gap, footer bounds, anchor displacement, and
button visibility after the relevant callbacks/layout changes have completed.
An assertion immediately after `done`, or a fixed sleep alone, is insufficient.
For diagnosis, record each scroll writer's reason, current intent, bottom gap,
anchor, and reader-interaction generation before and after mutation. This is
proposed diagnostic instrumentation, not something currently implemented.


## Closure criteria

Record the implemented fixes and test results, trace the reported production
sequence or explicitly retain it as unresolved, and verify that late callbacks
do not move a reader away from their chosen position. Do not infer incident
closure solely from synthetic reproductions or the ADR's acceptance.

## Stabilization implemented (2026-11-15)

Automatic startup, foreground, view-switch, network, and periodic connectivity
checks now probe health without fetching or rebuilding history. Realtime replay
and snapshots remain responsible for automatic transcript recovery; explicit
refresh and bounded history navigation still use HTTP. This is not completion
of the WS-only migration.

Position restoration now honors the captured bottom-following state. History
resets restore position after date dividers and other synchronous decorations;
older-page compensation likewise runs after divider construction. Live-edge
insertion preserves completed-history order even when message IDs disagree
with completion times. PWA asset versions were bumped together.

Browser regressions cover footer growth, three consecutive timestamped history
rebuilds, automatic recovery across the former history polling interval, and
completion-order inversion around a pending turn. Existing WIP growth,
scrollback, viewport resize, completion, and pagination coverage also exercises
these changes. The original production callback sequence remains untraced;
late asynchronous layout and all completion-presentation edge cases are not
claimed resolved by these targeted fixes.

Validation: all 18 Node UI tests passed; all 25 thinking/scroll browser tests
passed, and all 14 history-pagination browser tests passed on rerun after
replacing an immediate DOM read with an assertion waiting for loaded rows.
`node --check ui/app.js` also passed.

## Recovery review follow-up (2026-11-15)

The first stabilization patch removed HTTP reconciliation before replacing its
existing-row recovery role. The UI now uses the correlated local WS
`history.check` read for exact rendered completed IDs, in batches of at most
100, after subscription and health recovery. It removes deleted responses and
updates changed ones without replacing unchanged rows, pending streams, or live
prompts. Position is captured after each reply to respect intervening reader
navigation. Large batches split; a single oversized row or failed check offers
an explicit refresh. Bounded lifecycle snapshots alone are never interpreted
as proof of deletion. SSE turn streaming now also enables lifecycle discovery;
Shore's existing capability grant is unchanged and denies the new local query.

Explicit refresh now detects removed IDs even before a previous sync baseline
exists. Successful initial history loading records its sync time, while health
success alone no longer suppresses a slow initial-history warning. Failed
prompt-history bootstrap is retried on successful health probes. Filter/reload
navigation requests its first history page immediately even when retained live
rows leave its pagination sentinel above the viewport.

Connection tests now distinguish host reachability from explicit history sync.
A WS browser regression covers SSE-mode discovery, same-ID changes, deletion,
unchanged row identity/reading position, and explicit recovery for oversized
rows. This remains a stabilization change, not full Chat WS migration.

Follow-up validation: 247 Python tests passed (realtime, Shore capability/parity,
stats DB and history tools), as did all 18 Node UI tests. The final focused
browser checks passed across reruns: 31 connection-history, 25 thinking/scroll,
14 pagination, and four selected Chat cases. The initial broader browser run
passed 176/179; its attachment test reload, offscreen filter sentinel, and stale
instruction-link selector failures were corrected and passed targeted reruns.
The SSE fixture now counts active connections rather than connections closed
by startup recovery. The delayed WS reply regression confirms that scrolling
away while a check is pending is respected when its result arrives. No
publication or original-production callback trace was performed.


## Filtered recovery correction (2026-11-15)

Existing-row reconciliation no longer uses the incoming-response visibility
policy, which suppresses all incoming responses in bookmarks-only and
bad-response-only views. The bounded WS check now applies those annotation
filters on the host. Matching changed rows are reinserted using the existing
topic/agent/flow filter; deleted rows and rows whose annotations were removed
are removed. Unchanged matching rows retain their DOM identity. Boolean query
filters are validated, and unfiltered by-ID callers retain their old behavior.
Browser regressions cover changed, unchanged, and no-longer-matching rows in
both filtered views; backend tests exercise real annotation additions/removals.

Validation for the filtered correction: 204 backend tests, all 18 Node UI tests,
and all 33 connection-history browser tests passed, including both new filtered
recovery cases. JavaScript syntax checking passed. PWA cache versions were
bumped together to `20261115-041`. No publication was performed.

## Prompt-only recovery correction (2026-11-15)

Bounded recovery now includes stored `.prompt-only-history-item` rows as well
as assistant rows. Pending prompt rows can update without attaching a streaming
response. Replacements reuse the prompt-only renderer and completion ordering,
including the open-response control and timestamp. Unchanged prompt rows retain
their identity; deleted or empty prompts are removed. Lifecycle discovery also
recognizes existing prompt rows and renders newly discovered turns as prompts
while this view is active. The browser regression checks pending prompt edits,
deletion, unchanged-row identity and reading offset, and pending-turn discovery
without leaking response bubbles into the prompt-only view.

Validation for the prompt-only correction: all 18 Node UI tests and 73 browser
checks passed (72 connection/scroll/pagination cases plus the new prompt-only
regression). JavaScript syntax checking passed. PWA cache versions were bumped
together to `20261115-042`. No publication was performed.

## Existing-row ordering correction (2026-11-15)

A mobile screenshot showed a newly submitted prompt and its WIP above an older
completed response. A browser reproduction using actual `sendMessage()` and
WS recovery changed only the older response's quota metadata. DOM order changed
from `[old response, new prompt, new WIP]` to `[new prompt, new WIP, old response]`.
This identifies a recovery ordering defect; the phone's exact metadata-trigger
field was not captured. Recovery removed the old row and passed it through the
new-completion insertion helper. That helper does not anchor against freshly
submitted live WIP rows, so the older response was appended after the live group.
Bottom restoration then followed the displaced old response and could hide the
new prompt above the viewport.

Existing-row recovery now renders a replacement fragment at the old group's
original sibling boundary, including its timestamp/stats/tool rows, instead of
sorting it as a newly completed turn. New-completion ordering remains separate.
Prompt-only replacements retain their row-specific presentation. Regressions
exercise two successive metadata updates after real local submission and with
a recovered live group, asserting exact row order, surviving prompt/WIP identity,
prompt visibility, footer counts, and bottom gap on a mobile-sized viewport.

Validation for existing-row ordering: all 18 Node UI tests and 75 browser checks
passed (connection recovery, thinking/scroll, and history pagination), including
both new metadata-after-submission regressions. JavaScript syntax checking
passed. PWA cache versions were bumped together to `20261115-043`. This change
is frontend-only; no backend restart or publication was performed.
