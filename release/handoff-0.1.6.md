# AgentSquid 0.1.6 release preparation

Prepared 2026-11-08; publication has not been performed.

## Sources

- Previous stable baseline: Squid `v0.1.5`.
- Intended Squid source: `306c02c` plus the 0.1.6 preparation changes.
- Shore pin retained: `1df2195d14a7e9830010b483271693fd2e4f51af`, available
  on `origin/main`. Later commits change only docs, E2E configuration, and ignore
  rules. The previous stable Squid release had no Shore pin, so the Shore feature
  is new relative to 0.1.5.
- Public installer prerequisite confirmation: agentsquid.ai `052a5d0`.
- Version 0.1.6 was absent from PyPI (HTTP 404) and remote Squid tags at preparation.

## Validation

- `uv lock --check`: passed.
- `uv build` and `twine check`: wheel and source distribution passed.
- Isolated wheel install: package imports and reports 0.1.6.
- UI Node tests: 14 passed.
- Shore `pair-cli.spec.ts`: passed against preproduction with isolated Squid
  source, throwaway account, login, both pairing paths, reconnect, and audit export.
- Full Python suite: 1,839 passed, 11 subtests passed, 2 warnings (526 seconds).
- GitHub configuration names: Squid dispatch token and Shore production signing,
  Cloudflare, release bucket/origin settings present; no shore-prod approval rule.
  Secret values and production credentials were not exercised.
- Recent rc29 package and Shore release workflows succeeded.

## Release scope

0.1.6 is a Python-only release. Shore remains experimental and disabled by
default; production readiness is not a gate for this package release.
`publish-shore-client` explicitly skips `v0.1.6`; other tags retain their existing
behavior. No Shore Worker, bootstrap, client assets, or stable pointer will be
published by this release. The retained Shore source pin is unused for this tag.
Production bootstrap and stable-pointer URLs returned 404 during preparation;
this is expected for the deferred Shore production launch.

## Remaining work

- Review preparation changes and sync before any publication.
- Run `uv sync` in the backing Squid repo before restarting dev.
- With separate release authorization, publish the matching version tag and
  reviewed notes from `release/notes-0.1.6.md`.
- Verify PyPI 0.1.6, confirm the Shore job is skipped, and smoke-test the installed
  package before declaring completion.
