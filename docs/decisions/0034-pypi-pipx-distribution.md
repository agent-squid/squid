---
status: accepted
date: 2026-07-24
---
# ADR-0034: Distribution via PyPI, Install/Update via pipx

## Context and Problem Statement

ADR-0030 formalized a notify-only update mechanism on top of the install
path that existed at the time: `curl <tag>.tar.gz | tar xz` into a
version-named sibling directory (`squid-X.Y/`), run its `bin/install.sh`,
then `bin/start.sh`. That ADR planned `bin/update.sh` (never written) to
automate re-running that sequence against a new tag, and flagged a
dependency it hadn't landed yet: moving `PID_FILE` out of the per-checkout
directory into `~/.squid/` so a new version's `start.sh --restart` could
find and kill the old version's process across directories.

Two problems with that path surfaced while actually preparing a release:

- The package's own `pyproject.toml` had never been exercised as a real
  install target: `[project.scripts]` pointed `squid` at
  `agent.server:app` — a FastAPI ASGI instance, not a callable — which
  would have crashed on invocation. It went unnoticed because
  `bin/start.sh` always ran `python -m agent.server` directly, never the
  installed console script.
- The project name `squid` collides with the long-established Squid proxy
  cache server (owns `brew install squid`, dominates search results), an
  SEO/discovery problem independent of the packaging bug.

Separately, a `pip install`-based path is the standard expectation for a
Python CLI tool, and `pipx` (isolated per-app venvs, one command to
upgrade) is the standard tool for installing Python CLI applications
system-wide without polluting a project or system environment — closer to
what `brew install`/`npm install -g` users expect than a tarball-and-shell-
script checkout.

## Decision

**Publish to PyPI as `agentsquid`; install and update via `pipx`.**

- **Package renamed** `squid` → `agentsquid` in `pyproject.toml`, alongside
  the `agent.server:main` entry-point fix and added PyPI metadata
  (`readme`, `license`, `authors`, `classifiers`, `project.urls`).
  `agent/server.py`'s `_pkg_version()` lookup key updated to match — this
  had to change in the same commit as the rename, since a stale lookup key
  silently returns the `PackageNotFoundError` fallback (`"0+local"`) for
  every real install, which the version-compare logic below then reads as
  always-behind (see Consequences).
- **Install**: `curl -fsSL https://agentsquid.ai/install.sh | bash` —
  hosted on the marketing site (`agentsquid.ai/install.sh`, built from
  `~/Work/agentsquid.ai`'s `install.sh`, registered in
  `scripts/build-site.mjs`'s `STATIC_FILES`). The script bootstraps `pipx`
  if missing (`brew install pipx`, or `pip install --user pipx` +
  `pipx ensurepath` as fallback), then installs/upgrades `agentsquid`.
  Someone who already has `pipx` can skip the script and run
  `pipx install agentsquid` directly.
- **Update**: `pipx upgrade agentsquid`. No `bin/update.sh` — this
  replaces that planned script entirely, and the `PID_FILE`-to-`~/.squid/`
  move ADR-0030 flagged as a dependency is no longer needed: `pipx` keeps
  one venv location per app, upgraded in place, so there's no
  sibling-directory-per-version layout and no cross-directory old-process
  lookup to perform. `pipx upgrade` only replaces the installed files; a
  running server still needs to restart before the new code is active.
- **Upgrade-on-restart policy** is controlled by
  `updates.install_on_restart` in `~/.squid/squid.yaml`: `ask` (default),
  `always`, or `never`. When the UI has detected a newer PyPI version and
  the user restarts, `ask` reuses the existing themed restart modal to offer
  "Upgrade and Restart", "Restart Without Upgrading", or cancel. `always`
  runs `pipx upgrade agentsquid` before the restart without the extra
  update-choice prompt; `never` preserves restart-only behavior. Upgrade
  failures abort the restart and surface the pipx error, so the app does not
  silently restart while still on the old version.
- **In-app notice follows the install channel.** The installed version still
  comes from `/health`, which reports `importlib.metadata.version("agentsquid")`.
  The latest available version is read from PyPI's project JSON endpoint
  (`https://pypi.org/pypi/agentsquid/json`), not from GitHub `main`, so the
  Settings notice only tracks versions that `pipx upgrade agentsquid` can
  actually install from production PyPI. The frontend's version comparison
  (`ui/app.js`) parses PEP 440 pre-release/post-release suffixes rather than
  comparing dotted numbers alone, so a `0.1.2rc1` production `pyproject.toml`
  bump during the TestPyPI candidate window (see Release checklist below)
  doesn't rank as newer than an already-installed `0.1.2` final — needed
  because this ADR's own release process routes rc versions through
  `pyproject.toml` before the final version is cut.
- **Release process**: publish through GitHub Actions and PyPI Trusted
  Publishing. Bump `pyproject.toml`'s `version` only when the next release
  is ready, tag the same version as `vX.Y.Z`, then push the tag. The
  `.github/workflows/publish.yml` workflow builds the sdist/wheel, runs
  `twine check`, and publishes to PyPI using GitHub OIDC (`id-token:
  write`), so no long-lived PyPI API token is stored in GitHub. PyPI
  permanently rejects re-uploading an existing version, so the version bump
  is mandatory before every publish, not optional.
- **Existing GitHub tag tarball (`v0.1`) is left as-is**, not retagged or
  rebuilt — tags are treated as immutable once published. It's the last
  tarball-era release; no further tarball releases are planned. The
  `curl .../v0.1.tar.gz | tar xz` references in `README.md`,
  `ui/app.js`, and the `agentsquid.ai` site (`index.html`, `index1.html`,
  `docs/quick-start.html`) are all updated to the `pipx` path in the same
  change as this ADR.

## Publish Process

PyPI Trusted Publishing is configured against the exact GitHub workflow,
repository, and environment:

- PyPI project: `agentsquid`
- GitHub owner: `agent-squid`
- GitHub repository: `squid`
- Workflow filename: `publish.yml`
- Environment: `pypi`

TestPyPI has a separate Trusted Publishing entry for the test workflow:

- TestPyPI project: `agentsquid`
- GitHub owner: `agent-squid`
- GitHub repository: `squid`
- Workflow filename: `publish-testpypi.yml`
- Environment: `testpypi`

Release checklist:

1. Accumulate changes while `pyproject.toml` remains at the current
   published version.
2. When ready to test the next release, bump `[project].version` in
   `pyproject.toml` to a new pre-release version for TestPyPI, e.g.
   `0.1.2rc1`.
3. Run `python3 -m build` and `python3 -m twine check dist/*` locally if
   changing packaging metadata or included files.
4. Commit the version bump and release changes.
5. Create and push the matching TestPyPI tag, e.g. `test-v0.1.2rc1`, then
   run `bin/install-testpypi.sh 0.1.2rc1` after the TestPyPI workflow
   completes.
6. If TestPyPI exposes a packaging or install problem, fix it with another
   unused pre-release version, e.g. `0.1.2rc2` with tag `test-v0.1.2rc2`.
   Do not reuse `0.1.2rc1`; TestPyPI rejects replacing an existing
   distribution file for the same version, just like production PyPI.
7. Once the TestPyPI candidate is verified, bump `[project].version` to the
   final production version, e.g. `0.1.2`.
8. Commit the final version bump.
9. Create and push the matching production tag, e.g. `v0.1.2`.
10. Let GitHub Actions run `Publish to PyPI`; the job publishes only after
   the `pypi` environment's protection rules, if any, are satisfied.

The tag and package version must match. A tag `v0.1.1` with
`version = "0.1.0"` would try to publish `0.1.0`, and PyPI would reject it
if that version already exists. A tag `v0.1.1` with `version = "0.1.2"`
would publish `0.1.2`, which is misleading even though PyPI would accept it
if unused.

TestPyPI uses the same rule with a `test-` tag prefix: `test-v0.1.1` must
point to a commit where `pyproject.toml` has `version = "0.1.1"`. The
TestPyPI workflow publishes to `https://test.pypi.org/legacy/`; production
PyPI remains tied to `vX.Y.Z` tags only.

For repeated TestPyPI testing, use PEP 440 pre-release versions instead of
burning final versions: `0.1.2rc1`, `0.1.2rc2`, and so on. The corresponding
tags are `test-v0.1.2rc1`, `test-v0.1.2rc2`, etc., and each candidate is
installed with the exact same version string:
`bin/install-testpypi.sh 0.1.2rc1`. When the candidate is good, the real PyPI
release should be the final version (`0.1.2`), not the `rcN` version.

## Verified before publishing

The core risk in cutting over to `pipx upgrade` as the update mechanism is
whether it actually resolves and applies a newer version by package name
the way the in-app notice assumes. Verified locally, offline, before the
first real PyPI publish:

1. Built two wheels of the same package at different versions (`0.1.0`,
   `0.1.1`) from independent source copies.
2. Served them from a local directory via `pip`'s `--find-links` (no
   `--no-index`, so normal dependency resolution against the real index
   still applies to everything except the pinned local package) —
   resolving `agentsquid` **by name**, not by a fixed local path, matching
   how a real index lookup behaves.
3. `pipx install agentsquid --pip-args="--find-links <dir>"` with only
   `0.1.0` present in the directory installed `0.1.0`.
4. Added `0.1.1` to the directory. `pipx upgrade agentsquid` (the exact
   command shown in the Settings notice, no extra flags in production)
   correctly reported `upgraded package agentsquid from 0.1.0 to 0.1.1`.
5. Confirmed the installed app's own `importlib.metadata.version("agentsquid")`
   read back `0.1.1` post-upgrade — i.e. `/health`'s version field would
   correctly reflect the upgrade too, not just `pipx`'s own bookkeeping.

This confirms name-based version resolution and in-place upgrade work as
the notify-only mechanism assumes, without needing two real releases
published to PyPI/TestPyPI just to find that out.

One related, separately-fixed bug surfaced during this same testing pass:
running a second `agentsquid`/`python -m agent.server` process against the
shared `~/.squid` database (as happens when testing an installed package
locally) raced `agent/server.py`'s FastAPI startup-lifespan hooks — in
particular the orphaned-`pending`-message recovery — against a real,
still-running instance's in-flight write, incorrectly marking a live
message as errored before the second process ever discovered the port was
taken. Fixed by probing the port before calling `uvicorn.run()` in
`main()`, so a losing second process exits before the ASGI lifespan (and
its DB-mutating startup hooks) ever runs. Unrelated to the PyPI/pipx
decision itself, but found because of it.

## Consequences

- Good: `pip install`/`pipx install agentsquid` matches the standard
  expectation for a Python CLI tool, closer to `brew install`/`npm install
  -g` than a tarball-and-shell-script checkout.
- Good: no `bin/update.sh` to write or maintain, no `PID_FILE` relocation
  needed — `pipx` already solves both problems `bin/update.sh` existed to
  solve.
- Good: `agentsquid` avoids the name collision with the Squid proxy server
  that `squid` had (Homebrew formula name, search-result competition).
- Good: upgrade behavior verified against real name/version resolution
  before the first publish, not assumed.
- Bad: PyPI publish is a one-way door per version (no re-upload, no
  delete-and-reuse) — mistakes need a new version bump, not a fix-in-place.
- Bad: two release surfaces now exist in principle (PyPI for
  installed/`pipx` users, GitHub for source browsing) — mitigated by making
  GitHub version tags the trigger for PyPI publication, with the tag name
  matching `pyproject.toml`'s version.
- Bad: restart-time upgrades add a network/PyPI dependency to a path that
  users may expect to be local and quick. Keeping `ask` as the default makes
  that extra work explicit, while `always` remains available for users who
  prefer automatic upgrades on restart.
