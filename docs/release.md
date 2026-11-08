# RC and stable releases

This is the operational checklist for Squid's `publish.yml` and Shore's
`release.yml`. There is currently no single CLI command that prepares versions,
writes notes, publishes both surfaces, and verifies completion.

## One-line requests

Use an explicit version. These are preparation requests to a coding agent;
publication is a separate authorized step after the prepared changes are synced.

- RC: “Prepare AgentSquid X.Y.ZrcN: update both version files, review the Shore
  source pin, run relevant checks, and draft notes since the preceding RC.”
- Stable: “Prepare AgentSquid X.Y.Z: update both version files, review the Shore
  source pin, run relevant checks, and write release highlights covering all
  changes since the previous stable release, including every intervening RC.”

Do not interpret a preparation request as permission to publish. In an isolated
Squid turn, finish edits and checks before publication; a `backing_git`
continuation cannot create tags or GitHub releases. The `task_review` / `task_publication` sequence is limited to reviewed task
branch pushes and PRs; it cannot create release tags, GitHub releases, or
deployments. Release publication requires a separately authorized, supported
release execution path after sync. See [task publication](task-publication.md)
for that mechanism's scope; do not use it as a release launcher.

## Release lanes

| Tag | Python package | Shore client | Recommendation |
| --- | --- | --- | --- |
| `vX.Y.ZrcN` | PyPI prerelease | Preproduction (`shore-dev`) | Unchanged |
| `vX.Y.Z` | PyPI stable | Production (`shore-prod`) | Advances `client/stable.json` |
| `test-vX.Y.ZrcN` | TestPyPI only | None | Unchanged |

An RC is available on real PyPI but normally requires explicit prerelease
selection. TestPyPI is an optional packaging test, not the coordinated RC lane.
Stable and post-release tags target production; other prereleases target
preproduction. Publishing a browser client does not deploy the Shore Worker,
bootstrap, or compute services.

## 0.1.6 exception

Stable 0.1.6 publishes only the Python package. Shore remains experimental and
disabled by default, and production Shore is not ready. The workflow skips
`publish-shore-client` for `v0.1.6`; no production bootstrap deployment, browser
client publication, or stable-pointer update is required for this release.
Verify package publication and installation, and confirm the Shore job is skipped.
Other version tags retain the coordinated release behavior described below.

## Preparation

1. Choose an unused exact version and the intended Squid source revision.
   Update `[project].version` in `pyproject.toml` and the `agentsquid` entry in
   `uv.lock` together. The eventual tag must match that version exactly.
2. Review `release/shore-client.json`. If Shore changed, pin the intended
   reviewed, remotely available full commit SHA. Do not substitute an arbitrary
   current branch head. Ensure the deployed Worker and bootstrap support it.
3. Run checks appropriate to the changes. For packaging changes, build and run
   `twine check`; for login/pairing changes, run Shore's Playwright E2E suite.
   Run `uv sync` in the backing Squid repo after sync and before restarting dev.
4. Prepare reviewed release notes as described below. Record the previous
   release baseline and the intended source revisions in the release handoff.
5. Confirm PyPI trusted publishing, `SHORE_RELEASE_DISPATCH_TOKEN`, and Shore's
   target signing/R2 configuration are ready. Environment protection rules may
   require approval; Shore's documented automatic client lane assumes no
   required reviewers on `shore-prod`.

## Stable release notes

Compare the previous **stable** Squid release with the intended release
revision, not just the most recent RC. Compare the Shore SHA pinned by that
stable release with the newly selected pin as well. For a first stable release,
review the full intended release history. RC notes use the preceding RC for
the same target version, or the previous stable release for the first RC.
If neither exists, review the full intended release history. RC notes do not
replace the stable-to-stable review.

Read the actual changes, merged PRs, and existing RC notes. Group related work
into a short, user-facing list of key features, then relevant fixes and upgrade
or compatibility instructions. Include changes delivered in earlier RCs;
deduplicate repeated work and omit reverted features, internal churn, and
unsupported claims. Call out known limitations and any required operator action.

Put the reviewed summary in the GitHub release body for the matching tag.
Mark RC entries as prereleases. Neither current publishing workflow creates a
GitHub release or generates its notes. Automatically generated PR lists alone
do not satisfy the stable feature-summary requirement.

## Publication and completion

After preparation, sync, review, and explicit publication authorization, the
matching Squid version tag launches `Release AgentSquid`. That workflow builds,
checks, and publishes the package, then dispatches Shore with the same version
and the committed source pin. The GitHub release notes are a separate surface;
creating notes alone does not verify package or client publication.

Verify all of the following before reporting deployment complete:

1. Squid's package publication succeeds and PyPI exposes the exact version.
2. The corresponding Shore workflow succeeds for the exact version, SHA, and
   target. Squid's green dispatch job confirms only dispatch acceptance; it
   does not wait for Shore publication.
3. Shore's read-back verifies the signed manifest and asset bytes at the target
   origin. For stable releases, confirm the recommendation advanced.
4. Smoke-test the installed version and exact remote client, and confirm the
   reviewed GitHub release notes are published with the right prerelease flag.

If PyPI succeeds and Shore fails, report a partial release. Fix the client lane
and retry with identical inputs; never overwrite immutable artifacts. The
current PyPI job has no skip-existing option, so rerunning a successful package
publication is not a general recovery command. Use Shore's independent workflow
for client recovery, following its runbook.

## Remaining automation gaps

A true one-command release still needs a preparation/publication entry point,
production tag-to-package version validation before upload, a reviewed notes
input with a previous-stable baseline, GitHub release creation, and correlated
waiting for Shore's completion. These are not implemented by this runbook.

See [ADR-0050](decisions/0050-paired-release-distribution.md) and Shore's
`docs/runbooks/shore-release.md` for trust boundaries, configuration, read-back,
revocation, and recovery details.
