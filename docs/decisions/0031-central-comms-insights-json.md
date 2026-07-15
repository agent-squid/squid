---
status: accepted
date: 2026-07-13
updated: 2026-07-15
---
# ADR-0031: Central Comms via `insights.json` on the Homepage Domain

## Context and Problem Statement

Squid has several surfaces that could benefit from dynamic, remotely-updatable
content without requiring a code release:

- **Boot banner**: the talking-squid variant can show a contextual message
  (streak milestones, recent activity stats, time-of-day flavor).
- **Stats tab** (future): tips, milestone callouts, feature discovery.
- **Announcements** (future): version highlights, community news, maintenance
  notices.

All of these share the same shape: a small piece of content, with optional
conditions controlling when it appears, authored by the maintainer and consumed
by the app. They don't need real-time delivery — boot-time fetch is sufficient.

Early versions used a hardcoded condition set (`streak`, `sessions.milestone`,
`hour`, `dow`, `random`) with a single DB-backed stat from `/health`. This was
too rigid — adding "cache hit rate this week" or "turns WoW" required backend
code changes. The format needed to reference the same stats measures the stats
page already computes, with period scoping and week-over-week comparisons.

The question: where does this content live, how does squid fetch it, and what
format gives us declarative access to the stats system without code changes per
message?

## Considered Options

1. **Embed in the squid repo.** Ship a `bubble-templates.json` (or similar)
   bundled with the code. Updates require a new release.
2. **Separate static file on the homepage domain.** Host `insights.json` on
   `agentsquid.ai` (the GitHub Pages + Cloudflare-proxied homepage). Squid
   fetches it at each boot, stores the last result for offline fallback.
3. **Cloudflare Worker acting as an API.** A Worker merges content from
   multiple sources, serves it, and provides analytics.
4. **Dedicated backend endpoint.** Squid's own `/health` or a new route serves
   content from the server's database.

## Decision

**Option 2 — static `insights.json` on `agentsquid.ai`.**

```
agentsquid.ai/insights.json   ← maintainer edits in homepage repo, pushes
         │
         ▼   (fetched at each boot, last result stored for offline)
    squid app
         │
         ├─ boot banner (variant 2: talking squid bubble)
         ├─ stats tab (future)
         └─ announcements (future)
```

- **Single file, namespaced.** One JSON file with top-level keys for each
  surface (`boot`, `stats`, `announcements`, etc.). Squid reads the keys it
  knows, ignores the rest. New surfaces can be added without a format change.

- **Source of truth is the homepage repo** (`agent-squid/agentsquid.ai`), not
  the squid repo. Content authors can update messages without touching squid
  code or cutting a release. The squid code defines the format and condition
  engine; the homepage repo fills in the content.

- **Fetch at each boot, last result stored for offline.** No TTL — every boot
  pulls the latest. The JSON is a few KB and served from Cloudflare's edge CDN.
  If the fetch fails (offline), the last successful result from localStorage is
  used. If nothing is stored, squid falls back to the hardcoded default
  (`"More Done, Less Tokens."`).

- **Not a backend service.** No Worker, no API, no database. The file is plain
  JSON served by the existing GitHub Pages → Cloudflare CDN pipeline already
  set up for `agentsquid.ai`. This is consistent with ADR-0030's philosophy of
  "checked via a static file, not an API."

### Declarative measure references

The `insights.json` declares what data it needs in a top-level `measures` block.
Each entry is a key–measure–period–aggregation tuple. Templates reference keys
with `{key}` placeholders. Conditions use the same keys.

The measure keys (`turns`, `cache_hit_rate`, `cost`, etc.) are the **same
measures the stats page already defines** in `CHART_METRICS` and
`STATS_METRIC_AGGS` (`ui/app.js`, near line 5441 as of 2026-07-15 — check
current line numbers rather than relying on this citation). This is squid's existing semantic
layer — each measure has a fixed set of valid aggregations determined by its
semantic type:

| Semantic type   | Measures                         | Valid aggs                | Default |
|-----------------|----------------------------------|---------------------------|---------|
| **count**       | turns, sessions                  | `sum` only                | sum     |
| **currency**    | cost                             | sum, avg, min, max, p50–p95 | sum  |
| **token count** | tokens_in, tokens_out, tokens_total, cache_read, cache_write, new_input | sum, avg, min, max, p50–p95 | sum |
| **ratio**       | cache_hit_rate                   | `avg` only                | avg     |
| **per-unit**    | avg_tokens_turn                  | `sum` only                | sum     |
| **delta**       | quota                            | sum, avg, min, max, p50–p95 | sum  |
| **duration**    | duration                         | avg, sum, min, max, p50–p95 | avg  |

When `agg` is omitted, the default from `STATS_METRIC_AGGS` is used.

### Stats resolution (client-side from `/stats`)

The frontend resolves measure values by calling the existing `/stats` endpoint,
which already computes period-bucketed aggregates for every measure. No new
backend endpoints are needed.

For a measure like `{ "key": "turns", "measure": "turns", "period": "7d" }`:
1. Call `GET /stats?period=weekly&days=7`
2. Sum `total_turns` across returned rows → `228`

For a WoW comparison like `{ "key": "turns_wow", "measure": "turns", "compare": "prev_period", "fmt": "delta" }`:
1. Call `GET /stats?period=weekly&days=14`
2. Current week row → 228, previous week row → 180
3. Delta = 228 − 180 = `+48`

Using `period=weekly` means a 14-day range returns only 2 rows (this week, last
week), making WoW comparisons trivial.

### Format

```json
{
  "measures": {
    "period": "7d",
    "values": [
      { "key": "streak",    "source": "local" },
      { "key": "hour",      "source": "clock" },
      { "key": "dow",       "source": "clock" },
      { "key": "turns",     "measure": "turns" },
      { "key": "turns_wow", "measure": "turns", "compare": "prev_period", "fmt": "delta" },
      { "key": "cache",     "measure": "cache_hit_rate" },
      { "key": "cache_wow", "measure": "cache_hit_rate", "compare": "prev_period", "fmt": "pp" }
    ]
  },
  "boot": {
    "default": "More Done, Less Tokens.",
    "templates": [
      {
        "text": "Day {streak} streak. Absolute legend.",
        "when": { "streak": 365 }
      },
      {
        "text": "{turns} turns this week! {turns_wow} from last week. 🦑",
        "when": { "turns": { "gte": 50 } }
      },
      {
        "text": "Cache hit {cache}% — {cache_wow} vs last week!",
        "when": { "cache": { "gte": 80 }, "cache_wow": { "gte": 1 } }
      },
      {
        "text": "Monday. Let's ink some code.",
        "when": { "dow": { "in": [1] } }
      },
      {
        "text": "Burning the midnight oil? 🌙",
        "when": { "hour": { "between": [0, 5] } }
      },
      {
        "text": "More tentacles, fewer tokens.",
        "when": { "random": 0.06 }
      }
    ]
  }
}
```

### Measure reference fields

| Field       | Required | Description                                                  |
|-------------|----------|--------------------------------------------------------------|
| `key`       | yes      | Variable name used in `{key}` placeholders and `when` blocks |
| `measure`   | yes*     | Stats-page measure name (e.g. `turns`, `cache_hit_rate`)     |
| `source`    | yes*     | `"db"` (default), `"local"` (streak), or `"clock"` (hour/dow) |
| `period`    | no       | `1d`, `3d`, `7d` (default), `14d`, `30d`, `90d`, `all`      |
| `agg`       | no       | Defaults from `STATS_METRIC_AGGS` per measure                 |
| `compare`   | no       | `"prev_period"` for WoW delta                                |
| `fmt`       | no       | `"value"` (default), `"delta"`, `"pct"`, `"pp"`, `"duration"` |
| `filter`    | no       | `{ "agent": "…", "topic": "…", "adhoc": "session" }`        |

*\* Exactly one of `measure` or `source` is required.*

### Condition operators

All `when` keys reference measure keys. Conditions in a template are AND-ed;
templates evaluated top-to-bottom, first match wins.

| Operator      | Meaning                    | Example                        |
|---------------|----------------------------|--------------------------------|
| `eq`          | equal                      | `"streak": {"eq": 365}`        |
| `gte`         | greater than or equal      | `"turns": {"gte": 50}`         |
| `lte`         | less than or equal         | `"cost": {"lte": 5}`           |
| `gt`          | greater than               | `"turns_wow": {"gt": 0}`       |
| `in`          | value in list              | `"dow": {"in": [1, 3, 5]}`     |
| `between`     | value in range [a, b)      | `"hour": {"between": [0, 5]}`  |
| *(bare value)*| exact match (shorthand)    | `"streak": 365`                |
| *(bare 0–1)*  | random probability          | `"random": 0.06`               |

### Format specifiers

`fmt` controls how the value is rendered into `{key}` placeholders:

| `fmt`       | Input   | Output      |
|-------------|---------|-------------|
| `"value"`   | 228     | `228`       |
| `"delta"`   | 48      | `+48`       |
| `"delta"`   | -12     | `-12`       |
| `"pct"`     | 26.7    | `+26.7%`    |
| `"pp"`      | 3.0     | `+3 pp`     |

### Fallback chain

```
Template matches         → render with resolved values
No template matches      → boot.default text
No insights.json loaded  → hardcoded "More Done, Less Tokens."
```

## Why not the other options

- **Option 1 (bundled):** content updates require a code release. Breaks the
  decoupling between "what the message says" and "what version of squid is
  installed." A streak milestone message for day 100 shouldn't need a git tag.

- **Option 3 (Cloudflare Worker):** adds infrastructure to maintain (Worker
  script, deploy step, TLS for a new subdomain) for a project whose release
  philosophy is "no build pipeline, no new UI component, no new
  process-management code" (ADR-0030). The static file on the existing
  Cloudflare-proxied domain gives the same CDN edge caching and the same
  analytics side effect without a Worker.

- **Option 4 (backend endpoint):** couples content delivery to the squid server
  being up and reachable. Boot messages should work even if the server is
  unreachable (the offline fallback path). Also adds server-side complexity for
  something that's fundamentally static content.

## Consequences

- Good: content updates are a push to the homepage repo — no code release, no
  server restart, no Worker deploy.
- Good: extensible format. Adding a new surface (`announcements`, `tips`) is a
  new top-level key — no format migration needed.
- Good: same measure names as the stats page. Users see "turns" in both places.
- Good: no new infrastructure. Piggybacks on the existing Cloudflare-proxied
  GitHub Pages domain.
- Good: works offline. Last fetch → hardcoded fallback. Squid always boots.
- Good: declarative measures. Adding a new stat-backed message is a one-file
  edit to `insights.json` — no code changes.
- Good: WoW comparisons via `compare: "prev_period"` and `period=weekly` keep
  message data lightweight (2 rows for a WoW comparison).
- Bad: no push delivery. Squid only checks at boot. If a user leaves squid
  open for days, they won't see new messages until the next restart.
- Bad: boot message now depends on `/stats` response time (typically <50ms for
  a 7d weekly query — negligible at boot).

## Related

- [ADR-0030](./0030-update-mechanism-notify-only.md): update mechanism using
  `pyproject.toml` from `raw.githubusercontent.com`. Same static-file-over-CDN
  philosophy, different file and domain.
