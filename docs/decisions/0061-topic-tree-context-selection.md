---
status: proposed
date: 2026-11-24
---
# ADR-0061: Topic Tree Context Selection

## Context

A global context selection can carry work pins or files into an unrelated
topic on the next send. The [context injection plan](../plans/context-injection-global-local-scope.md)
proposed remembering selections per delivery route. This proposal uses the
topic tree as the scope so related topics can share selections.

## Decision

Use the root topic's identity to scope saved selections. Topics under the
same root share their checked context items. When switching trees, load the
destination tree's saved selections and clear staged composer attachments.
A tree without saved selections starts with nothing checked. Attachments
already sent stay with their original turns.

A context item can remain available for explicit selection in another tree.
Its presence in the picker never selects it automatically. Existing global
selections start unchecked in each tree until the user selects them there.

Resolve the destination tree before assembling each prompt. Automatic
conversation history and summaries come from that destination's own
conversation context. Squid must also keep session resume scoped to the
destination so prior turns from the source tree cannot carry over through
a resumed provider session.

### Context picker

- **Checked** means selected for the next send. Existing session
  deduplication still applies.
- **Unchecked** means leave the item out of the next send.
- **Already sent** is a separate indicator for an item injected earlier
  into the current session. It can appear on an unchecked item because
  unchecking affects future injection and the session may retain earlier
  content.

Keep injection tracking per session as described in
[ADR-0009](0009-pinned-message-injection-scoping.md).

### Provider eligibility

[ADR-0060](0060-topic-provider-preferences.md) continues to govern which
providers can run each topic. This proposal governs automatic carryover
between topic trees. Provider groups and cross-group sharing policy remain
separate decisions. An explicit context selection remains subject to any
applicable provider policy.

## Verification

- Check a pin in one topic and confirm a sibling under the same root shares
  the selection.
- Switch to a different tree and confirm its saved selections load and staged
  attachments clear. For a tree without saved selections, verify the next
  prompt excludes the source tree's pins, staged files, history, and summaries.
- Return to the original tree and confirm its saved selections are restored.
- Uncheck an item already sent to the current session and confirm the
  indicator remains visible while the item is omitted from fresh injection.
