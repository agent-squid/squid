---
status: accepted
date: 2026-05-30
updated: 2026-06-10
---
# ADR-0015: Topic Journals as Weekly Markdown Snapshots

## 2026-06-10 Update

Automatic journal generation is disabled for now because it can make real CLI
calls and consume significant tokens. The manual `/journal` command and journal
read/list API remain available so the feature can be revisited without removing
the implementation.

## Context and Problem Statement

Conversations in Squid accumulate over time across multiple agents and topics.
When starting a new session — especially with a different agent, or after a
week away — there is no way to quickly orient the agent on what was done
previously without replaying the full message history as context, which is
expensive and often irrelevant.

A mechanism was needed to create durable, human-readable summaries of
what happened in a topic, injectable as context pins at the start of a new
session.

## Considered Options

**Trigger point**
1. **On every prompt (background task)** — always up to date; at-most-once-per-day
   guard prevents redundant generation
2. **Explicit command only** — user must ask; easy to forget
3. **On server shutdown** — misses abrupt exits; awkward for a long-running daemon

**Granularity**
1. **Per-topic aggregate** — summarises all agents in a topic; loses per-agent detail
2. **Per-agent only** — precise but misses cross-agent context
3. **Both: aggregate + per-agent** — covers both handoff and overview use cases

**Storage**
1. **DB table** — queryable but opaque; not directly injectable as a file pin
2. **Markdown files in `context/`** — appear in `/tmp/squid` via existing
   context sync; injectable as pins without any new infrastructure

**Generation agent**
1. **Configured in YAML** — static; requires restart to change
2. **Resolved from DB at runtime** — uses `get_default_agent()`, consistent
   with how the rest of the system picks agents; dynamically updated

**Minimum turn threshold**
1. **Configurable minimum (e.g. 5 turns)** — avoids trivially short journals;
   adds a tunable that must live somewhere
2. **Zero-turn guard only** — generate for any non-empty week; content scales
   naturally with conversation length

## Decision Outcome

**Trigger:** background `asyncio.create_task` on every non-adhoc `/chat`
request. A `.state.json` file tracks the last generation timestamp per scope;
generation is skipped if already run today for the current ISO week.

**Granularity:** both grains are generated:
- `context/journals/{topic}/YYYY-Www.md` — all agents for that topic
- `context/journals/{topic}@{agent}/YYYY-Www.md` — single agent's turns only
- `context/journals/_all/YYYY-Www.md` — cross-topic synthesis (generated after
  topic journals, from their markdown output)

The per-agent journal is the primary artifact for session resumption: injecting
`work@claude/2026-W22.md` as a pin gives the next session an exact record of
what that specific agent did, without noise from other agents.

**Storage:** Markdown files under `context/journals/`. They appear in
`/tmp/squid/journals/` automatically via the existing `context_sync` rsync,
making them available as file pins with no additional infrastructure.

**Generation agent:** resolved via `get_default_agent()` at generation time.
Not configurable in YAML — agent config already lives in the DB.

**Threshold:** guard is `turn_count == 0` only. A one-turn week produces a
brief journal. Content scales with conversation length; an arbitrary minimum
was not added.

### API surface

| Endpoint | Description |
|---|---|
| `POST /cmd {"command":"journal","topic":"…","agent":"…"}` | Force-generate; awaited, returns file path |
| `GET /journals/{topic}` | List all journals for topic (aggregate + per-agent) |
| `GET /journals/{topic}/{week}` | Aggregate markdown |
| `GET /journals/{topic}/{week}?agent=…` | Per-agent markdown |

### Scope naming

Scopes follow `{topic}` for aggregate and `{topic}@{agent}` for per-agent,
matching the existing `#topic@agent` notation used throughout the UI and ADR
history.

## Consequences

- Good: journals appear in `/tmp/squid` automatically — no new sync code
- Good: per-agent grain enables precise context injection on agent handoff or
  session restart
- Good: fire-and-forget trigger never delays a `/chat` response
- Good: `_all` cross-topic synthesis gives a weekly overview across all work
- Bad: journal generation makes a real CLI call (billed tokens) once per day
  per scope; on a busy instance with many topics and agents this could add up
- Bad: generation runs outside `TopicDispatcher`, so it is not FIFO-ordered
  and does not appear in queue depth or active-process counts — intentional, but
  means journal tasks are invisible to `/processes`
- Note: `.state.json` is a simple JSON file with no locking; concurrent journal
  triggers for the same scope (unlikely given the once-per-day guard) could
  race on the state write — mitigated by atomic `os.replace` on the temp file
