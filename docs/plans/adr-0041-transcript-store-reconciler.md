# ADR-0041 implementation record

ADR-0041 was completed on 2026-08-22.

## Final ownership

- `ui/transcript-store.js` is the normalized, transport-independent transcript
  state keyed by message identity.
- `ui/reconciler.js` serially reconciles dirty assistant turn IDs.
- The history registry in `ui/app.js` owns completed HTTP history, recovered
  pending turns, WebSocket snapshot discovery, WebSocket lifecycle discovery,
  and registered SSE pending-to-terminal transitions under `#messages`.
- Failed snapshot or authoritative lifecycle reconciliation leaves the turn
  dirty, withholds its cursor ACK, and reconnects for a bounded snapshot. It
  does not create a direct-DOM fallback owner.
- Transport watchers retain mutation of their already-adopted live preview
  nodes (streamed text, narrative, tools, queue state, and cancellation). The
  registry owns stable identity, placement, and terminal replacement.
- Prompt-only history, Flow attachment, and CLI-auth UI are distinct
  projections outside ADR-0041's four transcript producers.

## Completed producer migration

| Producer | Normalized input | Registry rendering | Legacy fallback retired |
|---|---:|---:|---:|
| HTTP history | yes | yes | yes |
| WebSocket snapshot | yes | yes | yes |
| WebSocket lifecycle | yes | yes | yes |
| SSE chat/reconnect | yes | registered terminal handoff | yes for registered turns |

Sparse realtime rows are enriched from the authoritative `/chat/{id}/status`
row before rendering. Terminal discovery installs its content and completion
timestamp as normalized message facts; raw-only enrichment never overwrites
live accumulated content.

## Final verification

Run on 2026-08-22 with one Playwright worker per file:

- `transcript-store.spec.js`: 54 passed.
- `reconciler.spec.js`: 13 passed.
- `history-registry.spec.js`: 28 passed.
- `history-store-renderer.spec.js`: 9 passed.
- `chat.spec.js`: 101 passed on the full run; its four failures exposed the
  stale-frame success/no-op regression, and all four passed together after the
  fix.

The PWA cache version for the final cut is `v20260822-027`.

## Follow-up boundary

Moving Flow attachment, CLI-auth, or live-preview mutation into normalized
rendering requires a separate decision. Those changes are not unfinished
ADR-0041 work.
