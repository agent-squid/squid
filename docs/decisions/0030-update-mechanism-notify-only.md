---
status: accepted
date: 2026-07-13
---
# ADR-0030: Update Mechanism — Tag-Based Tarball Releases, Notify-Only

## Context and Problem Statement

Squid has no update mechanism today. Install is `curl <tag>.tar.gz | tar xz`
(`README.md`), which happens to extract into a version-named sibling
directory (`squid-X.Y/`, GitHub's archive convention) but nothing formalizes
moving between versions, checking for new ones, or telling the user one
exists. There is also no migration system: DB tables are created via
`CREATE TABLE IF NOT EXISTS` only (`agent/stats_db.py`), and config is
bootstrapped into `~/.squid/squid.yaml` only if missing (ADR-0014); neither
reconciles a shape change into an existing install. `PID_FILE` was also
directory-scoped (`bin/start.sh`, `bin/stop.sh`) even though the port/config
it depends on is shared via `~/.squid/squid.yaml` — moved to `~/.squid/` as
part of this work so cross-version restarts detect the right process.

Squid has no installed user base yet (pre-1.0, no released state to stay
backward compatible with), so the immediate need is narrower than "handle
arbitrary version jumps safely" — it's to formalize the release/update loop
that already exists informally, and let users know when they're behind,
without building a migration system ahead of any real need for one.

## Considered Options

1. **Do nothing beyond documentation** — tell users to re-run the install
   curl command with a new tag. No version visibility, no in-app notice.
2. **Notify-only**: single version source of truth, `/health` reports it,
   frontend checks the latest released version and surfaces it in the
   existing UI (this ADR).
3. **Self-applying updater**: the server detects a new release, downloads it,
   and restarts itself into the new code in place.
4. **Full migration system now**: version-gated `~/.squid/version` marker +
   ordered migration chain + old-version-refuses-to-boot, ahead of any real
   need for it.

## Decision

**Option 2.**

- **Single version source.** `pyproject.toml`'s `version` field becomes the
  only place the version is declared; `agent/server.py`'s hardcoded
  `FastAPI(title="Squid", version="0.1.0")` reads it instead of duplicating it.
- **`/health` reports version.** Added alongside the existing `boot_time`,
  `harnesses`, `providers` fields.
- **`bin/update.sh`.** Downloads the target tag's tarball into a sibling
  `squid-X.Y/` directory (the layout `README.md` already documents), runs its
  `install.sh`, then its `start.sh --restart` — reusing the existing
  port-based old-process kill and active-prompt confirmation already in
  `bin/start.sh` rather than writing new kill/confirm logic.
- **In-app notice, checked via a static file, not the GitHub API.** The
  frontend fetches `raw.githubusercontent.com/<org>/squid/main/pyproject.toml`
  directly (cached in `localStorage`, ~24h TTL) and regexes out the `version`
  line, rather than calling `api.github.com/repos/.../releases/latest`. No new
  manifest file to maintain — it reads the same `pyproject.toml` field Phase 1
  already made canonical. `raw.githubusercontent.com` is CDN-backed (Fastly),
  not subject to `api.github.com`'s unauthenticated rate limit, and needs no
  JSON/release-schema parsing — just a plain-text fetch and one regex. This
  mirrors how existing auto-updaters actually work (see "Prior art"): a small
  static version file, not a platform API call.
- **Surfaced as a badge, not a boot banner.** The boot banner
  (`showBootBanner()`, `ui/app.js`) is reserved for blocking conditions — e.g.
  no coding agent installed, squid unusable until fixed. An available update
  is optional, not blocking, so repeating a full banner in the chat timeline
  on every page load would be noisy for something already seen. Instead: a
  small dot/badge on the existing hamburger button (`#hamburger-btn`,
  `initSettings()`), persistent until dismissed or updated, with the actual
  current-vs-latest version and the copy-paste update command shown inside
  the existing `Settings` view — no new UI surface, reuses the menu/view
  structure that's already there.
- **Release process.** Cutting a release is: bump `pyproject.toml` + `git tag
  vX.Y` + push. No CI/build pipeline — it's a source tarball, not a compiled
  artifact.

Option 3 (self-applying) is rejected for now: the running server lives inside
one versioned directory's venv and can't relaunch itself as a different
directory's code without a wrapper process, and a bad auto-update on a
locally-run dev tool is a worse failure mode than a copy-pasted command the
user chooses to run. This is also why auto-update defaults to off rather than
on — a restart kills in-flight CLI sessions, so it must stay a deliberate,
confirmed action either way.

Option 4 (full migration system) is deferred, not rejected — see below.

## Deferred: version-gated migrations

Not built yet, since there is no installed base to protect:

- `~/.squid/version` marker recording the last version to touch `~/.squid/`.
- Ordered migration chain (`MIGRATIONS = [("0.2", fn), ("0.3", fn), ...]`),
  applied sequentially from the installed version up to the target — never
  pairwise deltas between every `(from, to)` pair.
- Old binaries refusing to boot if `~/.squid/version` is ahead of what they
  know, so an old install can't silently misinterpret state a newer version
  has reshaped.
- Backup of `squid.db`/`squid.yaml` before applying any migration step.

Revisit once there is a real prior release whose state needs to survive an
upgrade. Until then, DB and config changes must stay additive-only (no
renamed/dropped columns or keys) — the same discipline `agent/stats_db.py`
already follows — since that discipline is what keeps an old binary
accidentally safe to run against newer-shaped state in the absence of a
version gate.

## Prior art

- **npm/gh/docker-style CLIs**: check the latest release, print a "you're
  behind, run X to update" nag; never self-apply. Closest match to Option 2,
  and the dominant pattern for developer-facing CLI tools.
- **asdf/rbenv/nvm/Homebrew Cellar**: each version installed to its own
  directory, old ones left in place until explicitly removed — free
  rollback, no in-place mutation. Matches the tag-per-directory tarball
  layout already in use.
- **Squirrel.Windows (`RELEASES` manifest), Sparkle (`appcast.xml`), VS
  Code's `update.json`**: all check a small static version file/manifest
  fetched from a CDN, not a full platform/release API. This is the actual
  precedent for reading `pyproject.toml` straight off `raw.githubusercontent.com`
  instead of calling `api.github.com/.../releases/latest` — the static-file
  check is the standard-weight approach, not a cut corner.
- **Home Assistant**: shows a persistent "update available" indicator with a
  one-click apply — closest precedent for the badge treatment (persistent,
  low-severity, not a one-time banner), but HA has a separate supervisor
  process that can safely restart the main app into new code; squid has no
  such supervisor, which is why Option 3 isn't in scope.
- **VS Code/Slack/Docker Desktop (Electron auto-updaters)**: background
  download + apply-on-restart, backed by a signed-update framework
  (Squirrel/Sparkle). The manifest-check idea is worth borrowing; the
  apply-on-restart machinery is overkill for a locally-run, source-tree tool
  aimed at technical users.

## Consequences

- Good: version is visible in one place (`/health`) instead of
  duplicated/unused.
- Good: reuses existing `start.sh` port-kill/confirm logic and
  `showBootBanner()`'s banner pattern — no new UI component or
  process-management code.
- Good: release process is just a git tag; no build pipeline to maintain.
- Good: doesn't block adding the deferred migration system later —
  `/health`'s version field and the update banner keep working once that lands.
- Bad: update still requires a manual step (running the copy-pasted
  command) — acceptable for now, revisit if that becomes friction once there
  are real users.
- Bad: client-side static-file check only fires while the UI tab is open and
  the browser is online.
- Bad: a badge is easier to miss than a banner — acceptable trade for
  matching severity (optional, not blocking) to presentation; revisit if
  users report missing updates entirely.
- Bad: until the deferred migration system exists, any DB/config shape
  change must remain additive-only or an old running instance could
  misbehave silently.
