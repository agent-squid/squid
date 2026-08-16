---
status: proposed
date: 2026-08-15
---
# ADR-0044: Community Token-Efficiency Leaderboard — Opt-In, Aggregate-Only, a Squid-Owned Tab Beside the Community Page

## Context

The Community view today is a single iframe of the homepage's static page
(`ui/index.html` → `https://agentsquid.ai/community.html?embed=1`). It is
read-only marketing content authored in the homepage repo; it cannot see any
of a user's local stats, and squid ships nothing community-shaped of its own.

Separately, squid computes rich per-turn analytics locally (`/stats`,
`CHART_METRICS`, ADR-0026) — turns, tokens in/out/cache, cost, cache-hit rate,
duration — but by design (ADR-0016, local-first) none of it ever leaves the
box. The product tagline is **"More Done, Less Tokens."** There is a natural,
fun, universal thing to rank on that we already almost measure: *efficiency,
not volume*. oMLX ships a benchmark leaderboard in its admin UI; the ask is a
squid analogue, except ranking **real work**, not a synthetic benchmark.

Two facts make this a genuine architecture decision rather than a content
edit:

1. **A leaderboard is inherently multi-party.** Users submit, a server
   aggregates and ranks, everyone reads the ranked result. That is a write +
   aggregate path. It directly contradicts ADR-0031's chosen shape for
   central comms (`insights.json`: static file, pull-only, *no backend, no
   Worker, no database*). insights.json works precisely because it is
   one-directional; a leaderboard cannot be.
2. **We do not yet track the headline metric.** The requested "lines changed
   per input token" needs a per-turn net-diff number that is nowhere in
   `CHART_METRICS` today. It *is* derivable — per-turn worktree isolation
   (ADR-0025) plus topic code roots (ADR-0020) already establish a diff
   boundary per turn — but it is new data squid would have to compute and
   store.

## The metric

Ranking on raw volume (most turns, most tokens, most lines) just crowns the
heaviest user and leaks how much someone works. The interesting, on-brand,
*fair* axis is a **ratio**, so a light careful user can beat a heavy one — and
so nothing about absolute activity is exposed.

Headline board — **Code Yield**: net lines changed per 1,000 total tokens
(equivalently, tokens per net line). This is the user's "lines changed based
on input token," normalized into a rate. It is language-agnostic and
backend-agnostic (works for Claude, Codex, and local providers alike, whose
token semantics differ — ADR-0017 — but whose *net diff* does not).

Raw line-counting is trivially gamed and noisy, so the definition is
constrained up front:

- **Net diff, not churn** — added minus deleted on the turn's worktree diff,
  not total touched lines. Reformatting nets ~zero.
- **Exclude generated/vendored/lockfiles** — a denylist (lockfiles,
  `node_modules`, `dist`, minified, vendored, obvious generated headers) so a
  10k-line generated file doesn't win.
- **Minimum sample threshold** — a handle needs N qualifying turns in the
  window before it ranks, so a single lucky turn can't top the board.
- **Outlier capping / windowed** — per-turn yield is capped and the board is a
  rolling window (e.g. weekly), not all-time, so early flukes decay.

Even so, this is a **fun board, not a benchmark of record** — we accept mild
gameability rather than building an anti-cheat system. To keep it playful
rather than a single gameable target, ship a small basket of objective boards,
not one number:

- **Code Yield** (net lines / 1k tokens) — headline.
- **Cache Hit %** (already computed, ADR-0026) — rewards good session hygiene.
- **Streak** (already local) — rewards showing up.

We deliberately **do not** rank on cost or absolute token spend: it is a
privacy leak and pay-to-win. Board metric definitions are versioned
(`board_schema` field) so they can evolve without silently changing rankings.

## Decision

**1. A squid-owned Leaderboard sub-tab inside the existing Community view.**
Parent element is `#view-community` in `ui/index.html`, which currently
contains only `#community-iframe`. Add a sub-nav (themed segmented control,
not a browser/native control) with two panes: **Community** (the existing
external iframe, unchanged) and **Leaderboard** (a new squid-rendered pane
using the common theme components — never a system modal or external iframe
for squid-owned data). The leaderboard pane reads a static
`leaderboard.json` from the CDN and renders it locally, so the read path stays
as cheap and offline-tolerant as insights.json.

**2. Submission is opt-in, aggregate-only, and pseudonymous.** Off by default.
An explicit settings opt-in that shows *exactly* what will be sent before it is
enabled. What leaves the box is only: a user-chosen pseudonymous handle and the
derived numeric board metrics for the window. **Never** raw stats rows, topic
or agent names, file paths, filenames, prompts, or any code. Revocable —
opting out (or a "delete my data" action) removes the handle from the board.
Squid reuses its existing `/stats` aggregation to compute the board numbers
locally, then sends only those derived values on a signed periodic snapshot.

**3. A minimal dynamic ingest service — the one place we accept a backend.**
insights.json's "no Worker, no database" (ADR-0031) is the right call for
pull-only content and is *not* reversed here for that content. A leaderboard
genuinely needs a write + aggregate path, so this single surface gets a minimal
Cloudflare Worker + KV/D1: it accepts signed opt-in submissions, aggregates and
ranks them, and periodically regenerates a static `leaderboard.json` served
over the same CDN. The dynamic part is confined to *ingest*; every client read
stays static-file-over-CDN, exactly like insights.json. This is the explicit,
scoped exception to ADR-0031, justified by the multi-party nature of ranking.

## Consequences

- **First squid dynamic service.** A Worker + KV/D1 to maintain (deploy step,
  abuse/rate limiting, a signing scheme for submissions). This is the scoped
  reversal of ADR-0031's "no backend" — for this surface only, and only for
  the write path.
- **New per-turn data.** Net-diff-per-turn (with the generated-file denylist)
  must be computed and stored — it does not exist in `CHART_METRICS` today.
  That is a real code change in the stats pipeline, gated behind the same
  worktree/code-root machinery that already exists (ADR-0025, ADR-0020).
- **Privacy surface.** The first path by which any local number leaves the
  box. Must stay opt-in, aggregate-only, revocable, and fully disclosed at the
  opt-in moment, or it breaks the local-first promise (ADR-0016).
- **Cold-start / small-N.** A leaderboard with three participants is not fun.
  Given current distribution (see growth baseline: single-digit stars, zero
  distribution as of mid-2026), this may be premature until there is an
  audience, or needs a seeding/threshold plan ("board unlocks at N handles").
  Worth deciding before building, not after.
- **Gaming is possible and accepted.** Framed as fun, defended only by the net-
  diff/denylist/threshold/window constraints above — not by anti-cheat.
- **PWA version bump required at implementation time.** The UI change touches
  `ui/index.html` (and likely `ui/app.js`/`ui/style.css`), so the version
  string must be bumped in all five spots or clients serve stale cache. (Noted
  for the implementing change; this ADR is documentation only.)

## Considered and rejected

- **Put it in the external `community.html`.** Can't — that static page has no
  access to a user's local `/stats`; the whole point is ranking local work.
- **Extend insights.json to carry the board.** insights.json is pull-only; it
  has no ingest path, so it can accept submissions from no one.
- **Rank on cost or raw volume.** Privacy leak and pay-to-win; contradicts
  "More Done, Less Tokens."
- **GitHub gist / PR-based submission.** Too much friction for a periodic
  automatic snapshot, and still exposes raw data in a public repo.

## Related

- [ADR-0031](./0031-central-comms-insights-json.md): pull-only static central
  comms — the philosophy this ADR carves a scoped exception out of.
- [ADR-0016](./0016-security-model.md): local-first security model the opt-in
  must honor.
- [ADR-0017](./0017-token-counting-semantics.md): per-backend token semantics
  the Code Yield denominator must normalize across.
- [ADR-0025](./0025-per-turn-worktree-isolation.md),
  [ADR-0020](./0020-topic-code-roots-for-cross-agent-diffs.md): the per-turn
  diff boundary that makes net-lines-changed derivable.
- [ADR-0026](./0026-stats-breakdown-filter-semantics.md): the local stats
  measures the board metrics are computed from.
