---
status: accepted
date: 2026-07-13
---
# ADR-0029: Retain Stats When Topics Are Deleted

## Context

Squid records usage in `session_stats` for cost, token, duration, cache, and
quota analytics. Topics and agent lanes are workflow organization surfaces: they
drive chat history, active sessions, autocomplete, memory, and navigation.

Deleting a topic removes the chat messages that power turn drilldown and message
previews. However, deleting the corresponding `session_stats` rows also changes
historical usage totals. That makes the Stats page unstable: cleanup of old
topics can make previous consumption appear to disappear.

Squid usage is not the provider account source of truth. Other provider
surfaces, tools, machines, and concurrent prompts can consume tokens or quota
outside Squid. Still, Squid-attributed consumption should remain internally
stable unless a future explicit purge operation says otherwise.

## Decision

- Topic and topic-agent deletion must not delete `session_stats`.
- Deletion may remove active workflow state such as topic rows, topic sessions,
  chat messages, run-event drilldown reachability, and worktree state.
- Aggregate Stats should continue counting retained `session_stats` rows for
  deleted topics and agents.
- By Turn can only show turns whose `chat_messages` rows still exist. Deleted
  topics may therefore retain aggregate usage without message previews or
  clickable turn details.
- Squid will not offer a stats purge in this decision. A future explicit purge
  action can be designed separately if needed.

## Consequences

- Stats totals stay stable after topic cleanup.
- Deleted topic and agent names may still appear in Stats filters if retained
  stats exist. The UI can label those values as deleted/archived in a later
  change.
- Aggregate usage and turn drilldown are intentionally allowed to differ after
  deletion: aggregate rows are usage history, while turn drilldown depends on
  retained chat history.
