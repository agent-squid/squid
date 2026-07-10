---
status: accepted
date: 2026-07-10
---
# ADR-0026: Stats Filter and Breakdown Semantics

## Context

The Stats view has independent filters for topic, agent, and session type. It
also has breakdown projections such as `Agent`, `Agent x Session Type`, and
future compound projections such as `Topic x Agent x Session Type`.

The selectors are intentionally coarse. This is a UX simplification: users
filter by stable dimensions instead of exact rendered series. The agent selector
chooses base agents, not individual session lanes. For example, selecting
`codex` means the base agent `codex`; it does not directly choose only `codex`
or only `codex!` as chart columns.

The session type filter chooses which session lanes are included for the
selected base agents. This same rule must hold regardless of which breakdown is
currently projecting the data.

Without a clear rule, breakdowns can look inconsistent: the Total view may
include adhoc data, but a compound breakdown may hide the corresponding
`agent!` lane if only exact returned series are shown.

## Decision

Stats filtering is resolved by this principle:

- Filters constrain dimension values.
- Breakdowns choose how the constrained data is projected into rows/columns or
  chart series.
- A breakdown must not reinterpret a coarse filter as an exact-series picker.

Resolution order:

1. Apply topic filter.
2. Resolve base agent selection.
3. Apply session type filter.
4. Project the selected rows into the active breakdown.

The session type filter controls lane expansion for each selected base agent:

| Session type filter | Rendered lanes per selected base agent |
|---|---|
| `Sess + Adhoc` | `agent`, `agent!` |
| `Session` | `agent` |
| `Adhoc` | `agent!` |

This applies to every breakdown that includes the session type dimension:

- `Agent x Session Type`
- `Topic x Agent x Session Type`
- Any future compound breakdown containing `Session Type`

When no agents are explicitly selected, the UI chooses the default top base
agents for the breakdown. The same expansion rule then applies. With the current
top-three default:

- `Sess + Adhoc`: three base agents render as six session-type lanes.
- `Session`: three base agents render as three session lanes.
- `Adhoc`: three base agents render as three adhoc lanes.

For breakdowns that do not include the session type dimension, the same session
type filter still constrains the data, but it does not create separate session
type columns:

| Breakdown | `Sess + Adhoc` | `Session` | `Adhoc` |
|---|---|---|---|
| `Agent` | one column per base agent, aggregating both modes | one column per base agent, session data only | one column per base agent, adhoc data only |
| `Topic x Agent` | one series per topic/agent pair, aggregating both modes | one series per topic/agent pair, session data only | one series per topic/agent pair, adhoc data only |

For breakdowns that do include session type, lane expansion is explicit:

| Breakdown | `Sess + Adhoc` | `Session` | `Adhoc` |
|---|---|---|---|
| `Agent x Session Type` | `agent`, `agent!` | `agent` | `agent!` |
| `Topic x Agent x Session Type` | `topic / agent`, `topic / agent!` | `topic / agent` | `topic / agent!` |

For example, if the default base agents are `codex`, `claude`, and `opencode`:

| Session type filter | Expected lanes |
|---|---|
| `Sess + Adhoc` | `codex`, `codex!`, `claude`, `claude!`, `opencode`, `opencode!` |
| `Session` | `codex`, `claude`, `opencode` |
| `Adhoc` | `codex!`, `claude!`, `opencode!` |

For a topic-qualified compound breakdown, the same rule applies after topic
filtering. If the selected topics are `squid` and `ops`, and the selected base
agents are `codex` and `claude`, then `Topic x Agent x Session Type` yields:

| Session type filter | Expected lanes |
|---|---|
| `Sess + Adhoc` | `squid / codex`, `squid / codex!`, `squid / claude`, `squid / claude!`, `ops / codex`, `ops / codex!`, `ops / claude`, `ops / claude!` |
| `Session` | `squid / codex`, `squid / claude`, `ops / codex`, `ops / claude` |
| `Adhoc` | `squid / codex!`, `squid / claude!`, `ops / codex!`, `ops / claude!` |

Missing counterpart lanes are still valid columns in session-type breakdowns.
For example, if `opencode` has adhoc turns but no session turns in the selected
range, `Sess + Adhoc` still shows both `opencode` and `opencode!`; the session
lane has zero values.

## Non-Goals

- No exact micro-selection UI is introduced here. Users cannot select only
  `codex!` and `claude` while excluding `codex` and `claude!` through the base
  agent selector.
- No per-breakdown custom selector is introduced. The same topic, base agent,
  and session type filters drive all stats projections.

## Consequences

- The breakdown table and chart remain predictable: filters narrow the data,
  and breakdowns only change how that data is projected.
- Breakdowns containing session type can show more columns than selected base
  agents, because each selected base agent may expand into two lanes.
- Session-only and adhoc-only filters must be honored even under
  session-type breakdowns; they reduce the lane count from two per base agent
  to one per base agent.
- Exact mixed-lane comparison requires a future advanced exact-series selector
  or sort/filter control, not overloading the existing base agent selector.
