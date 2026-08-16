---
status: accepted
date: 2026-05-25
updated: 2026-08-16
---
# ADR-0008: `#topic` and `@agent` as Separately Colored Clickable Filters

## Context and Problem Statement

Messages in history come from multiple `(topic, agent)` combinations running in parallel. Users
need to browse and filter this history by topic, by model, or both. The filter state should be
visible and directly tied to the input context.

## Considered Options

1. A separate filter sidebar with dropdowns for topic and agent
2. Clickable tags in message bubbles, with topic and agent as separate spans
3. A single combined `#topic@agent` tag per bubble, click-to-filter whole combination

## Decision Outcome

**Option 2.** In every message bubble, `#topic` and `@agent` are rendered as separately colored,
independently clickable spans:

- `#topic` in one color (e.g. blue) — clicking filters history to `topic = X`
- `@agent` in a second color (e.g. teal) — clicking adds `agent = Y` to the active filter

**Filter behavior is hierarchical:** clicking `#work` shows all agentes in that topic; clicking
`@claude-opus` within that view narrows to `topic = "work" AND agent = "claude-opus"`.

**Filter scope is editable.** The active topic and agent lane render as separate badge
segments. Either segment can be removed independently, so removing `#work` leaves an
agent-only filter across topics. Clicking a segment restores the full filter command in
the composer for editing; an existing draft can be recovered with Up.

**Slash command surface.** The same filter can be applied via commands typed into the input:

| Command | Behavior |
|---|---|
| `/f`, `/filter` | Apply the current input's full lane as the history filter |
| `/f #work` | Filter the whole topic |
| `/f #work*` | Filter a topic and its dot-separated subtopics |
| `/f @claude-opus!` | Filter an agent lane across all topics |
| `/f @claude-opus*` | Filter both modes for an agent across all topics |
| `/f #work@claude-opus!` | Filter one topic and agent lane |
| `/f reset` | Clear the active filter and reload full history |

The command-argument form is canonical because topic input is promoted to the full sticky
chip during normal typing. The legacy prefix form remains supported. The `!` adhoc flag is
part of the agent segment; removing that segment removes both the agent and lane type.

| Input | History filter |
|---|---|
| `/f #work` | `topic=work` — all agents, all turn types |
| `/f #work*` | `topic=work&topic_subtree=true` — `work` plus every `work.*` descendant |
| `/f #work@claude-opus` | `topic=work&agent=claude-opus&adhoc=false` — session turns only |
| `/f @claude-opus!` | `agent=claude-opus&adhoc=true` — across all topics |
| `/f @claude-opus*` | `agent=claude-opus` — session and adhoc across all topics |
| `/f reset` | no params — full history |

### Topic subtree wildcard (`*`)

A trailing `*` on a topic in a filter scope selects the **whole subtree** — the topic
itself plus every dot-separated descendant — using a segment boundary, not string-prefix
matching:

| Pattern | Matches | Does not match |
|---|---|---|
| `#work` | `work` (exact) | `work.cat1`, `workaaa` |
| `#work*` | `work`, `work.cat1`, `work.cat1.sub1` | `workaaa`, `work10` |
| `#work.cat1*` | `work.cat1`, `work.cat1.sub1` | `work.cat1x`, `work.cat10` |

The predicate is `topic == work OR topic LIKE 'work.%'` (equivalently
`topic === 'work' || topic.startsWith('work.')` client-side). Because `*` binds at the
segment boundary, `workaaa` — a *sibling* topic whose name merely shares the string prefix —
never matches; it is not part of the `work` collection. The wildcard matches any depth
(`work.cat1.sub1` included) with no `**`-style recursion. `*` is safe to reserve for this
meaning because topic slugs are `[a-z0-9_]+(?:\.[a-z0-9_]+)*` and can never contain `*`.

The `*` suffix is distinct from the existing `@agent*` adhoc-mode suffix: it sits on the
topic segment (`#work*@claude-opus` = subtree + one agent lane; `#work@claude-opus*` = exact
topic + both agent modes).

For sent messages the server resolves a null agent from the topic's sticky agent or the
default agent, and the chip updates post-response. But `/filter` is processed before any
server round-trip, so `#topic`-only remains topic-only.

**Contract tests**: `tests/e2e/filter.spec.js`

## History display: responses only

The history panel shows **only assistant responses** — user prompts are not rendered as
separate bubbles. `GET /history` (backed by `get_messages_flat`) filters to
`role = 'assistant'` before returning rows. Each assistant bubble includes a 55-character
prompt snippet in its header (from the `reply_to` join), giving enough context without
a separate user turn row.

**Rationale:** History is a cross-topic, cross-agent feed — not a single conversation
thread. User prompts interleaved from different sessions have no coherent order and would
add noise. The prompt snippet in the response header is sufficient to anchor each response
to its question. Showing full pairs only makes sense in a single-thread chat view.

**Live turns are unaffected.** Real-time messages (the current send) render both the user
bubble and the streaming assistant response as they arrive, since they come through the SSE
path, not `GET /history`. Only the scroll-back history feed is responses-only.

## Consequences

- Good: no additional filter UI to build; chip already exists
- Good: intuitive drill-down — topic first, then model within topic
- Good: clicking a response naturally "enters" that lane for the next message
- Good: keyboard-driven filtering via `/filter` for users who prefer not to click
- Good: history panel is a clean response feed — prompt snippets give enough context
- Bad: clicking `@agent` without a `#topic` context active needs a defined behavior
  (resolved as: clicking `@agent` alone sets `topic = <message's topic> AND agent = Y`)
