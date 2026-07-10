---
status: accepted
date: 2026-07-10
---
# ADR-0026: Stats Breakdown Filter Semantics

## Context

The Stats view has independent filters for topic, agent, and session type. It
also has breakdown projections such as `Agent` and `Agent x Session Type`.

The selectors are intentionally coarse. The agent selector chooses base agents,
not exact rendered series. For example, selecting `codex` means the base agent
`codex`; it does not directly choose only `codex` or only `codex!` as chart
columns. The session type filter decides which session lanes are projected for
the selected base agents.

This matters most for `Agent x Session Type`. Without a clear rule, the UI can
look inconsistent: the Total view may include adhoc data, but the breakdown can
hide the corresponding `agent!` lane if only exact returned series are shown.

## Decision

Stats filtering is resolved in this order:

1. Apply topic filter.
2. Resolve base agent selection.
3. Apply session type filter.
4. Project the selected rows into the active breakdown.

For `Agent x Session Type`, the agent selector always means base agents. The
session type filter controls how many lanes each base agent expands into:

| Session type filter | Rendered lanes per selected base agent |
|---|---|
| `Sess + Adhoc` | `agent`, `agent!` |
| `Session` | `agent` |
| `Adhoc` | `agent!` |

When no agents are explicitly selected, the UI chooses the default top three
base agents for the breakdown. The same expansion rule then applies:

- `Sess + Adhoc`: three base agents render as six lanes.
- `Session`: three base agents render as three session lanes.
- `Adhoc`: three base agents render as three adhoc lanes.

For example, if the default base agents are `codex`, `clive`, and `opencode`:

| Session type filter | Expected lanes |
|---|---|
| `Sess + Adhoc` | `codex`, `codex!`, `clive`, `clive!`, `opencode`, `opencode!` |
| `Session` | `codex`, `clive`, `opencode` |
| `Adhoc` | `codex!`, `clive!`, `opencode!` |

Missing counterpart lanes are still valid columns in `Agent x Session Type`.
For example, if `opencode` has adhoc turns but no session turns in the selected
range, `Sess + Adhoc` still shows both `opencode` and `opencode!`; the session
lane has zero values.

## Non-Goals

- No exact micro-selection UI is introduced here. Users cannot select only
  `codex!` and `clive` while excluding `codex` and `clive!` through the base
  agent selector.
- No per-breakdown custom selector is introduced. The same topic, base agent,
  and session type filters drive all stats projections.

## Consequences

- The breakdown table and chart remain predictable: filters narrow the data,
  and breakdowns only change how that data is projected.
- `Agent x Session Type` can show more columns than selected base agents,
  because each selected base agent may expand into two lanes.
- Session-only and adhoc-only filters must be honored even under
  `Agent x Session Type`; they reduce the lane count from two per base agent to
  one per base agent.
- Exact mixed-lane comparison requires a future advanced exact-series selector
  or sort/filter control, not overloading the existing base agent selector.
