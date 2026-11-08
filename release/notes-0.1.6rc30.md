# AgentSquid 0.1.6rc30 — draft release notes

Draft for the next RC after source version `0.1.6rc29`. The proposed version
has not been checked for availability on PyPI. Baseline: Squid's rc29 version
commit `2269f18`. These are incremental RC notes, not the eventual stable
0.1.6 feature summary.

## Highlights

- **More useful watch status.** Watches have durable chat messages showing
  monitoring progress, with navigation from the status panel. GitHub watch
  tooling adds filtered bug reads and an idle-poll cap to bound inactive watches.
- **Better search.** Choose AND or OR matching for keyword searches, with
  refined result highlighting and navigation.
- **Ordered follow-on work.** Declare up to eight continuation steps, such as
  edit, commit, edit, then commit and push. Each step waits for successful
  completion and repository sync before the next starts.
- **Agent effort controls.** Configure effort on agent requests alongside model
  selection. Model and effort use the `/` separator.
- **Connection resilience.** Improved resource handling during reconnects,
  including a higher file-descriptor limit where supported and bounded
  concurrent diff-revert work.

## Pending inclusion in the release

The following capabilities are present in the working tree but include
uncommitted implementation changes at draft time. Include these highlights in
the published body only after those changes are reviewed and included in the
release revision:

- **Agents can consult topic history.** Read-only `agentsquid search` finds
  previous messages in the current topic or its subtree and returns message IDs
  for `/jump`. Retrieved history supplies context, not new authorization.
- **Reviewed task publication.** Explicitly authorized tasks can run a separate
  read-only review before the host commits the reviewed tree, pushes its saved
  task branch, and creates or updates a PR. Publication requires a recorded
  human grant; partial results and uncertain outcomes are retained for recovery.
- **Bounded agent routes.** Opt-in routes let configured agents schedule local
  work through existing Flow syntax, with shared limits on turns, depth, time,
  and concurrency. Routes do not authorize Git publication.
- **Stronger continuation ownership and recovery.** Watch and Flow descendants
  retain their original execution scope, while cancellation and recovery track
  linked work. Ad hoc continuation sequences keep a private session across
  their steps.

## Configuration and limitations

- Watches remain disabled by default. Monitoring requires `watches.enabled`,
  configured read tools, and an activated observation grant. Automatic local
  agent work additionally requires execution enablement and a separate grant.
- Continuations require `worktree.enabled: true`; routes additionally require
  `routes.enabled: true`. Routes are disabled by default.
- Explicitly requested direct-user pushes remain limited to the current backing
  branch. Task publication uses its separately granted task branch and PR scope.
- Release tags, GitHub releases, and deployments are not supported continuation
  actions. This RC does not provide an end-to-end one-command release process.

## Before publishing this draft

- Confirm the next unused RC number and final source revision, then resolve the
  pending-inclusion section against that revision.
- Review the intended Shore source pin. The current pin is
  `1df2195d14a7e9830010b483271693fd2e4f51af`; this draft makes no claim that later
  Shore changes are included.
- Run the relevant release checks and remove this editorial checklist. No
  release validation or deployment was performed while writing these notes.
- Publish as a prerelease only after verifying both the PyPI package and the
  matching Shore preproduction client. Squid's accepted dispatch alone does
  not confirm Shore publication.
