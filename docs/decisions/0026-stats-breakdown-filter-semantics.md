---
status: accepted
date: 2026-07-10
---
# ADR-0026: Stats Filter and Breakdown Semantics

## Context

The Stats view has independent filters for topic, agent, and session type.
In the `Total` view, these filters work independently and directly. A user can
pinpoint a specific grain such as `#squid@codex!` and inspect multiple metrics
for that filtered slice.

Stats also supports breakdown dimensions. A breakdown changes the analytical
grain: instead of viewing one filtered total over time, the user views that same
filtered data split by a dimension or dimension combination, such as:

- `Agent`
- `Agent x Session Type`
- `Topic x Agent`
- `Topic x Agent x Session Type`

When a breakdown is selected, the table and chart compare breakdown series for
one measure at a time. The multi-measure selector is disabled because it belongs
to the `Total` table, where multiple metrics can be shown side by side. The
chart measure selector remains active and chooses the single measure used for
both the breakdown chart and breakdown table.

Put differently:

- Filters decide which data is included.
- Breakdown decides which grain to compare.
- Measure selector decides which metric is displayed for that grain.

The selectors are intentionally coarse. This simplifies the filter UX and makes
compound breakdowns extensible. The agent selector chooses base agents, not
individual rendered series. The session type selector chooses which session
lanes are included for those base agents.

This means users cannot express every exact lane combination with the standard
filters. For example, they cannot compare only `codex` and `claude!`. With the
base agent and session type selectors, selecting `codex` and `claude` with
`Sess + Adhoc` yields `codex`, `codex!`, `claude`, and `claude!`.

That loss of micro-selection is intentional. The tradeoff is a simpler and more
predictable filter model that can support complex combinations of dimensions
without adding a custom selector for every breakdown.

The difficult part is default selection. A default should show complete
dimension combinations, not a partial set that only makes sense if exact
micro-series selection exists. For example, `Agent x Session Type` with
`Sess + Adhoc` expands each selected agent into two lanes, so selecting three
default agents would produce six rendered lanes. If the UI wants a compact
default of four rendered lanes, it should select two base agents and both
session types, not three base agents with an incomplete session-type expansion.

## Decision

Stats filtering follows this principle:

- Filters constrain dimension values.
- Breakdowns project the constrained data into a comparison grain.
- A breakdown must not reinterpret a coarse filter as an exact-series picker.
- Every independent filter must continue to work under every breakdown.
- Default breakdown selections should show complete dimension combinations
  within a small rendered-series budget.

Resolution order:

1. Apply topic filter.
2. Resolve base agent selection.
3. Apply session type filter.
4. Select the measure.
5. Project the selected data into the active breakdown grain.

## Total View

The `Total` view has no breakdown dimension. It shows the filtered aggregate
over time and can show multiple measures at once.

Examples:

| Filters | Result |
|---|---|
| topic = `squid`, agent = `codex`, session type = `Adhoc` | Total metrics for `#squid@codex!` |
| topic = `squid`, agent = `codex`, session type = `Session` | Total metrics for `#squid@codex` |
| topic = `squid`, agent = `codex`, session type = `Sess + Adhoc` | Total metrics for both `#squid@codex` and `#squid@codex!` |

## Breakdown Views

When a breakdown is selected, the same filters still apply. The difference is
only the projection grain.

For example, with breakdown = `Agent`:

- Topic filter still limits the data to selected topics.
- Agent filter still limits the base agents.
- Session type filter still limits session, adhoc, or both.
- The result is split by base agent.

With breakdown = `Agent x Session Type`:

- Topic filter still limits the data to selected topics.
- Agent filter still limits the base agents.
- Session type filter still limits session, adhoc, or both.
- The result is split by base agent and session type.

With breakdown = `Topic x Agent x Session Type`:

- Topic filter limits the topic dimension.
- Agent filter limits the base agent dimension.
- Session type filter limits the session type dimension.
- The result is split by topic, base agent, and session type.

## Session Type Expansion

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

## Default Breakdown Selection

Default selection is part of the semantics because the filters are coarse. The
default must choose source dimension values, then render the full Cartesian
combination implied by the active breakdown and filters.

The default rendered-series budget is four. This keeps the initial chart/table
readable while preserving complete combinations. The UI may show fewer than four
series if there are not enough available dimension values.

For a breakdown with session type and `Sess + Adhoc`, the session type dimension
has cardinality two. Therefore default rendered series should usually be even:

- `Agent x Session Type`: two agents x two session types = four series.
- `Topic x Agent x Session Type`: two topics x one agent x two session types =
  four series, or one topic x two agents x two session types = four series.

If the session type filter is `Session` or `Adhoc`, the session type cardinality
is one:

- `Agent x Session Type`: up to four agents x one session type = four series.
- `Topic x Agent x Session Type`: choose a complete topic/agent combination
  whose rendered series count stays at or below four.

Explicit user selections are not capped by the default budget. If the user adds
another base agent under `Agent x Session Type` with `Sess + Adhoc`, the rendered
series count doubles by two lanes. For example, two agents render four lanes;
three agents render six lanes; four agents render eight lanes.

The key rule is: default selection must never drop one side of an implied
dimension pair merely to hit a target count. It should reduce source dimension
values instead. For `Agent x Session Type`, prefer two complete agents with both
session lanes over three agents with incomplete lanes.

A future option may allow hiding empty dimension combinations, such as a
checkbox for "hide lanes without data." That would be a display option layered
on top of the coarse filter model, not a change to filter semantics.

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

## Examples

If the default base agents are `codex` and `claude`, then `Agent x Session Type`
yields:

| Session type filter | Expected lanes |
|---|---|
| `Sess + Adhoc` | `codex`, `codex!`, `claude`, `claude!` |
| `Session` | `codex`, `claude` |
| `Adhoc` | `codex!`, `claude!` |

If the user explicitly adds `opencode`, then `Sess + Adhoc` expands predictably
to `codex`, `codex!`, `claude`, `claude!`, `opencode`, and `opencode!`.

If the selected topics are `squid` and `ops`, and the selected base agents are
`codex` and `claude`, then `Topic x Agent x Session Type` yields:

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
  session type, and measure controls drive all stats projections.
- No multi-measure breakdown table is introduced here. Breakdown views compare
  one selected measure at a time.
- No default-selection optimizer is specified beyond the rendered-series budget
  and complete-combination requirement. Tie-breaking among candidate topics or
  agents can use existing ranking rules.

## Consequences

- The breakdown table and chart remain predictable: filters narrow the data,
  and breakdowns only change how that data is projected.
- Breakdowns containing session type can show more columns than selected base
  agents, because each selected base agent may expand into two lanes.
- Session-only and adhoc-only filters must be honored even under
  session-type breakdowns; they reduce the lane count from two per base agent
  to one per base agent.
- The filter model is simple and extensible for future compound dimensions.
- Default breakdowns remain readable without implying exact micro-selection.
- Adding another selected dimension value can multiply rendered series; this is
  expected because the UI renders complete coarse-filter combinations.
- Exact mixed-lane comparison requires a future advanced exact-series selector
  or sort/filter control, not overloading the existing base agent selector.
