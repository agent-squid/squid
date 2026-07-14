---
status: accepted
date: 2026-07-13
---
# ADR-0031: Central Comms via `insights.json` on the Homepage Domain

## Context and Problem Statement

Squid has several surfaces that could benefit from dynamic, remotely-updatable
content without requiring a code release:

- **Boot banner**: the talking-squid variant can show a contextual message
  (streak milestones, session counts, time-of-day flavor).
- **Stats tab** (future): tips, milestone callouts, feature discovery.
- **Announcements** (future): version highlights, community news, maintenance
  notices.

All of these share the same shape: a small piece of content, with optional
conditions controlling when it appears, authored by the maintainer and consumed
by the app. They don't need real-time delivery — boot-time fetch is sufficient.

The question: where does this content live, how does squid fetch it, and what
format does it use?

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

- **Template conditions.** Each message has an optional `when` block:
  `streak` (exact day count), `sessions.milestone` (total session milestones),
  `hour` (time-of-day range), `dow` (day of week), `random` (probability).
  All conditions in a `when` block are AND-ed; templates are evaluated
  top-to-bottom; first match wins. If nothing matches, a per-section `default`
  is used. Placeholders like `{streak}`, `{hit}` are substituted with live data.

- **Source of truth is the homepage repo** (`~/Work/agentsquid.ai/`), not the
  squid repo. Content authors can update messages without touching squid code
  or cutting a release. The squid code defines the format and condition engine;
  the homepage repo fills in the content.

- **Fetch at each boot, last result stored for offline.** No TTL — every boot
  pulls the latest. The JSON is a few KB and served from Cloudflare's edge CDN.
  If the fetch fails (offline), the last successful result from localStorage is
  used. If nothing is stored, squid falls back to the hardcoded default
  (`"More Done, Less Tokens."`).

- **Stats from `/health`.** `total_sessions` and `first_seen` were added to
  `/health` (queried from the local sqlite DB) to power milestone conditions.
  Streak is tracked client-side via `localStorage` date set.

- **Not a backend service.** No Worker, no API, no database. The file is plain
  JSON served by the existing GitHub Pages → Cloudflare CDN pipeline already
  set up for `agentsquid.ai`. This is consistent with ADR-0030's philosophy of
  "checked via a static file, not an API."

### Format

```json
{
  "version": 1,
  "boot": {
    "default": "More Done, Less Tokens.",
    "templates": [
      { "text": "Day {streak} streak. Absolute legend.",
        "when": { "streak": 365 } },
      { "text": "Session #{hit}! Here comes the next big thing.",
        "when": { "sessions": { "milestone": [50, 100, 250] } } },
      { "text": "Burning the midnight oil? 🌙",
        "when": { "hour": [0, 5] } },
      { "text": "More tentacles, fewer tokens.",
        "when": { "random": 0.06 } }
    ]
  },
  "stats": {
    "tips": [
      { "text": "Tip: Click any chart point for per-turn details.",
        "when": { "random": 0.2 } }
    ]
  }
}
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
- Good: no new infrastructure. Piggybacks on the existing Cloudflare-proxied
  GitHub Pages domain. If that domain's analytics are ever enabled, every fetch
  becomes a DAU heartbeat as a side effect — but that's not the mechanism's
  purpose.
- Good: works offline. Last fetch → hardcoded fallback. Squid always boots.
- Bad: no push delivery. Squid only checks at boot. If a user leaves squid
  open for days, they won't see new messages until the next restart.
- Bad: stats conditions (`total_sessions`, `first_seen`) require the `/health`
  endpoint to be reachable. These fields were added in this same change; if
  `/health` fails, the condition engine falls back to streak-only (localStorage)
  and eventually to the hardcoded default.

## Related

- [ADR-0030](./0030-update-mechanism-notify-only.md): update mechanism using
  `pyproject.toml` from `raw.githubusercontent.com`. Same static-file-over-CDN
  philosophy, different file and domain.
