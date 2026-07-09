---
status: accepted
date: 2026-07-09
---
# ADR-0025: Git Worktree Isolation for Agent Changes

## Context

Squid runs coding agents (Claude, Codex, etc.) against real repository paths
declared as topic code roots. When an agent edits files during a turn, those
changes land directly in the working tree. This creates two problems:

1. **Bleed between turns.** Uncommitted changes from turn N are visible to the
   agent in turn N+1 before they have been reviewed or committed, mixing
   in-progress state with the clean starting point.
2. **Bleed between sessions.** If two topics share a code root, an agent editing
   for one topic can interfere with another topic's diff tracking or working
   state.

Isolating each turn also makes it trivial to compute a diff scoped to exactly
that turn's changes, independent of anything else happening in the repository.

## Considered Options

1. **No isolation.** Agents write directly to the real working tree. Diffs are
   computed from the pre-turn Git snapshot vs. the post-turn working tree state.
   Concurrent or cross-session changes may be included in the diff.
2. **Per-session worktree with per-turn commit cycles.** Create one Git worktree
   per session. At the end of each turn, commit any remaining changes to the
   session branch, merge to main, and rebase the worktree onto the new HEAD —
   ready for the next turn without recreating the worktree.
3. **Per-turn worktree.** Create and destroy a fresh Git worktree for every
   agent turn.

## Decision

**Option 2.** Squid creates one Git worktree per session and enforces isolation
at the turn boundary via a commit-merge-rebase cycle.

### Naming and paths

```
worktree path:   <code-root>/.worktrees/sqd-<session-id>/
branch name:     sqd-<session-id>
```

The `.worktrees/` directory is gitignored. The `sqd-` prefix lets users and
agents identify Squid-managed branches at a glance.

### How the worktree is passed as the agent's working directory

When Squid launches an agent subprocess for a turn, it sets the process `cwd`
to the worktree path (`.worktrees/sqd-<session-id>/`) instead of the real code
root. The worktree is a full checkout of the repository at the session branch
HEAD, so the agent's file reads and writes land in the isolated branch. The real
working tree of the original repository is never touched while the worktree is
live.

The real code-root paths injected into model context remain the canonical
repository locations (see ADR-0020), not the worktree path. The agent sees the
correct project structure while its edits are isolated.

### Session lifecycle

```
session open
  → git worktree add .worktrees/sqd-<id> -b sqd-<id> HEAD

prompt runs
  → agent subprocess launched with cwd = .worktrees/sqd-<id>/

turn ends (see turn cycle below)

session close / clear
  → run final turn cycle if there are uncommitted changes
  → git worktree remove .worktrees/sqd-<id>
  → git branch -d sqd-<id>

session resume
  → reuse existing worktree if .worktrees/sqd-<id>/ exists
  → if not (expired or pruned): recreate from HEAD, then cherry-pick
    committed session-branch changes on top
```

### Turn cycle (end of every prompt)

```
turn N ends:
  1. auto-commit any uncommitted changes in the worktree ("squid: turn N")
  2. compute diff: sqd-<id>..HEAD^ (changes committed this turn vs. pre-turn HEAD)
  3. display diff to user — changes are already committed to sqd-<id> at this point
  4. merge sqd-<id> → main (fast-forward when possible)
  5. rebase worktree branch onto new main HEAD
     → worktree is now clean and current, ready for turn N+1
```

The diff is always shown from the committed branch state. By the time the user
sees the diff, the turn's changes exist as a commit on `sqd-<id>` and (after
merge) on main. No uncommitted state is left in the worktree between turns.

### Session expiry (1 hour idle)

If a session has been idle for more than one hour:

1. Commit any pending changes in the worktree to `sqd-<id>`.
2. `git worktree remove .worktrees/sqd-<id>/`.
3. The branch `sqd-<id>` is retained so work is not lost.

On resume after expiry:
1. `git worktree add .worktrees/sqd-<id> sqd-<id>` (recreate from the retained branch).
2. If `sqd-<id>` has diverged from main, rebase onto HEAD before the next prompt.

### Staleness and refresh

A worktree becomes stale when main accumulates commits since the worktree's
base (e.g., another session merged its changes). At the start of each prompt,
Squid checks whether the worktree branch is behind HEAD:

- **No local changes in the worktree** → `git reset --hard HEAD` (no rebase needed).
- **Has local changes** → `git rebase sqd-<id> onto HEAD`; surface conflicts to
  the user if the rebase cannot proceed automatically.

Refresh is also triggered unconditionally on resume after expiry.

### Merge conflicts at session close

When two sessions have edited the same lines of the same file, `git merge` at
session close will produce a conflict. Different hunks in the same file
auto-merge cleanly.

When a conflict is detected, Squid:

1. Keeps the worktree alive (does not delete it).
2. Surfaces conflicting files in the UI.
3. Opens a diff/editor view showing conflict markers.
4. Presents a "Mark as Resolved" button per file; validates that no conflict
   markers (`<<<<<<<`) remain before accepting.
5. Once all files are resolved: `git merge --continue`, then removes the
   worktree and branch.

## Fallback

Git worktrees require a repository with at least one commit and a working Git
installation. Worktree creation also fails on repositories in a detached-HEAD
state, when disk space is exhausted, or if the target path already exists and
is stale.

Non-git directories have no worktree support at all.

When worktree creation fails for any reason, Squid falls back to running the
agent directly in the real working tree (same as Option 1):

- Changes are **not isolated** per turn; edits land directly in the shared
  working tree.
- The diff shown after the turn may include changes from concurrent user edits,
  other processes, or other sessions targeting the same root.
- Squid labels the displayed diff as "unscoped" so the user knows the turn
  boundary was not enforced.
- Squid logs the specific worktree error and surfaces it in the session header.

Fallback is temporary: if the root cause is resolved (repository initialized,
disk freed, detached-HEAD fixed), the next session open retries worktree
creation.

## Consequences

- Good: each turn diff is exactly scoped to that turn's agent changes, and those
  changes are already committed before the diff is displayed.
- Good: inter-turn isolation is maintained within a session; each turn starts
  from a clean, rebased HEAD.
- Good: multiple sessions targeting different topics on the same code root do
  not interfere with each other.
- Good: the auto-commit at each turn end creates a legible per-turn commit
  history on main.
- Good: one worktree per session is cheaper than one per turn; session resume
  reuses the existing worktree without any git operations.
- Bad: if Squid crashes mid-turn, stale `.worktrees/sqd-*` directories and
  branches may be left behind; a startup sweep is needed to prune them.
- Bad: non-git directories and newly-initialized repos without an initial commit
  cannot use worktrees; bleed behavior applies until the condition is fixed.
- Bad: in fallback mode, cross-session or cross-turn change bleed is possible
  and the diff shown is not turn-scoped.
- Bad: merge conflicts at session close require user intervention via the
  conflict resolution UI before the worktree can be removed.
