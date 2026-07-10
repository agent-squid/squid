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

## Implementation Contract

The supported breakdown keys are:

| Key | Grain |
|---|---|
| empty / omitted | total over time |
| `agent` | time x agent |
| `agent_session` | time x agent x session type |
| `topic_agent` | time x topic x agent |
| `topic_agent_session` | time x topic x agent x session type |

All breakdowns should use the same conceptual response shape: one row per time
bucket and breakdown-grain tuple, with the same measure fields used by the
`Total` view. Dimension fields are present when they participate in the active
breakdown.

Required measure fields:

- `period`
- `sessions`
- `total_turns`
- `input_tokens`
- `output_tokens`
- `cost_usd`
- `quota_delta`

Required dimension fields by breakdown:

| Key | Dimension fields |
|---|---|
| `agent` | `agent_key`, `agent` |
| `agent_session` | `agent_key`, `agent`, `session_type` |
| `topic_agent` | `topic`, `agent_key`, `agent` |
| `topic_agent_session` | `topic`, `agent_key`, `agent`, `session_type` |

`session_type` is the normalized dimension value, either `session` or `adhoc`.
Rendered labels may still use the existing `agent` / `agent!` convention, but
the API shape should expose the normalized dimension so future renderers do not
need to parse punctuation.

The UI should pivot rows by:

1. time grain from `period`
2. active breakdown grain tuple
3. selected measure

Adding a new breakdown should not require a new chart/table rendering model if
it follows that row shape.

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

Default source dimension values are ranked by `total_turns`, not by the active
display measure and not by the sparsity of the rendered child combinations. This
makes defaults stable when the user changes the chart measure or breakdown. For
example, if `codex` is one of the top agents by turns, `Agent x Session Type`
with `Sess + Adhoc` may render both `codex` and `codex!` even when one lane has
no data in the selected range. The high-turn source agent anchors the default;
the breakdown then shows the complete implied combination.

For compound breakdowns, ranking should happen at the source dimension grain
before Cartesian expansion. `Topic x Agent x Session Type` should choose
topic/agent source values with the most turns under the active independent
filters, then expand those choices into session lanes. It should not chase the
top four individual topic/agent/session cells, because that would make the
default set too dynamic and would reintroduce exact-series behavior through the
back door.

Default dimension counts are allocated under the four-series budget as complete
combinations:

| Breakdown | Default source values with `Sess + Adhoc` |
|---|---|
| `agent` | top four agents |
| `agent_session` | top two agents x two session types |
| `topic_agent` | top two topics x top two agents |
| `topic_agent_session` | top one topic x top two agents x two session types |

When the session type filter is `Session` or `Adhoc`, the session type
cardinality is one and the freed budget may be used by the remaining dimensions.
For example, `topic_agent_session` with `Session` may select two topics x two
agents x one session type.

When a breakdown contains more dimensions than fit in the budget, reduce the
most explosive or dynamic dimension first. Today, topic is treated as broader
than agent for defaults because topic populations change more with day range and
workflow, so `topic_agent_session` uses one topic before reducing agents or
session types. A future session-id dimension should be treated as broader than
topic and should usually default to one session id in compound breakdowns.

For a breakdown with session type and `Sess + Adhoc`, the session type dimension
has cardinality two. Therefore default rendered series should usually be even:

- `Agent x Session Type`: two agents x two session types = four series.
- `Topic x Agent x Session Type`: one topic x two agents x two session types =
  four series.

If the session type filter is `Session` or `Adhoc`, the session type cardinality
is one:

- `Agent x Session Type`: up to four agents x one session type = four series.
- `Topic x Agent x Session Type`: follow the default allocation table; the
  freed session-type budget may expand the next dimensions while keeping the
  rendered series count at or below four.

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

## Saved Filter Presets

Stats must support named saved filter combinations. A saved preset captures the
current stats view state as coarse selectors, not expanded rendered series. This
keeps saved views aligned with the filter semantics above and allows future
dimensions, such as per-session views, without changing the base persistence
model.

Saved presets are backend-owned durable state, not browser-local storage. They
belong in the Squid DB because stats history and filter options are already
backend-owned and the same presets should survive browser changes.

The DB should store one row per named preset:

```sql
CREATE TABLE IF NOT EXISTS stats_filter_presets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    state_json   TEXT NOT NULL,
    is_default   INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_filter_presets_one_default
    ON stats_filter_presets(is_default)
    WHERE is_default = 1;
```

`state_json` is a versioned JSON document. It should store all filter dimensions
and selected values using dimension keys, rather than one SQL column per
dimension:

```json
{
  "version": 1,
  "time": {
    "period": "daily",
    "days": 30
  },
  "dimensions": {
    "topic": {
      "mode": "selected",
      "values": ["squid"]
    },
    "agent": {
      "mode": "selected",
      "values": ["codex", "clive"]
    },
    "session_type": {
      "mode": "all",
      "values": []
    }
  },
  "breakdown": {
    "key": "agent_session"
  },
  "measure": {
    "primary": "turns",
    "secondary": null,
    "visible": ["turns"]
  }
}
```

Dimension `values` must store source selector values, not rendered labels. For
example, save `agent.values = ["codex", "clive"]` with
`session_type.mode = "all"`, not `["codex", "codex!", "clive", "clive!"]`.
When the preset is applied, the normal breakdown resolver expands those values
into the rendered lanes.

Dimension `mode` distinguishes an explicit empty selection from dynamic default
selection:

| Mode | Meaning |
|---|---|
| `selected` | Use exactly the listed source values. |
| `all` | Include all values allowed by that dimension/filter. |
| `auto_top` | Recompute the top source values using the default ranking rules. |

The UI should save what the user means:

- If the user explicitly selected values, save `mode = "selected"` and those
  values.
- If the user is using the default top selection, save `mode = "auto_top"` so
  the preset continues to track the most-used source values by turns.
- If a dimension has an explicit all/both control, such as session type
  `Sess + Adhoc`, save that as `mode = "all"`.

Future dimensions should be added as new keys under `dimensions`, for example
`"session"` or `"model"`. Implementations should preserve unknown dimension
keys when round-tripping presets so older clients do not destroy newer saved
state. Unsupported dimensions may be ignored when applying a preset, but they
must not be silently deleted.

Recommended API shape:

| Endpoint | Behavior |
|---|---|
| `GET /stats/filter-presets` | List presets with `id`, `name`, `state`, `created_at`, and `updated_at`. |
| `POST /stats/filter-presets` | Create a preset from `{ "name": "...", "state": { ... } }`. Reject duplicate names. |
| `PUT /stats/filter-presets/{id}` | Rename and/or replace the saved state. |
| `DELETE /stats/filter-presets/{id}` | Delete the preset. |

Applying a preset is a UI operation: load the saved state into the existing
stats controls, then request stats through the normal stats endpoints. Presets
must not introduce a separate stats query path.

Preset UI behavior:

- Each preset is a named view and may be rendered as a selectable tab, pill, or
  menu item. One saved row maps to one named UI entry.
- Selecting a preset applies its state, records that preset as the active preset,
  and clears the dirty state.
- Changing any stats control after applying a preset creates a temporary dirty
  state. The UI should keep the active preset visible but mark it as modified
  until the user saves, overwrites, switches away, or resets.
- Save New creates a new preset from the current temporary state.
- Overwrite updates the active preset's `state_json` with the current temporary
  state and clears dirty state.
- Rename changes only `name`.
- Delete removes the preset. If the deleted preset was active, the current
  controls may remain as an unsaved temporary state.
- Mark Default sets `is_default = 1` on exactly one preset. On first Stats tab
  load, apply the default preset if no explicit URL/query state is present.

The structured `state_json` is the canonical saved form. A URL query string is a
projection of the same state for sharing and browser navigation. The app may
derive a URL from `state_json`, and may hydrate controls from URL parameters,
but should not store only the URL as the preset source of truth.

Reasons to keep structured state instead of URL-only persistence:

- It preserves selection modes such as `auto_top`, `selected`, and `all`.
- It can round-trip future dimensions without inventing new query syntax first.
- It is easier to validate and migrate by `version`.
- It avoids treating presentation labels or expanded series as saved state.

URL/query state takes precedence over the default preset because opening a
shared link is an explicit user action. Applying a named preset should update
the controls and may update the URL to the equivalent query representation.

## Non-Goals

- No exact micro-selection UI is introduced here. Users cannot select only
  `codex!` and `claude` while excluding `codex` and `claude!` through the base
  agent selector.
- No per-breakdown custom selector is introduced. The same topic, base agent,
  session type, and measure controls drive all stats projections.
- No multi-measure breakdown table is introduced here. Breakdown views compare
  one selected measure at a time.
- No exact default-selection optimizer is specified beyond the rendered-series
  budget, complete-combination requirement, allocation table, and top-by-turns
  ranking. Tie-breaking among candidate topics or agents can use existing
  ranking rules.
- No separate preset table per dimension is introduced. Extensibility comes
  from the versioned `state_json` document, not from schema changes for every
  new stats dimension.

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
- Saved presets can reproduce explicit views while still allowing dynamic
  top-by-turns defaults through `auto_top`.
