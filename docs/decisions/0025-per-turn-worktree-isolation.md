---
status: accepted
date: 2026-07-09
---
# ADR-0025: Per-Turn Git Worktree Isolation for Agent Changes

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
2. **Per-session worktree.** Create one Git worktree per Squid session, shared
   across all turns in that session. Isolates sessions from each other but not
   individual turns within a session.
3. **Per-turn worktree.** Create a Git worktree for each agent turn. At turn
   end, commit any uncommitted changes, merge back to main, and advance HEAD for
   the next turn.

## Decision

**Option 3.** Squid uses a per-turn Git worktree to isolate agent changes.

The lifecycle for turn N is:

```
turn N starts
  → create worktree branched from current HEAD (sqd-branch)
  → agent runs inside the worktree

turn N ends
  1. emit git diff scoped to the worktree branch (no bleed from other changes)
  2. auto-commit any uncommitted changes ("squid: turn N")
  3. merge sqd-branch → main
  4. rebase worktree onto new HEAD (ready for turn N+1)
  5. remove the spent worktree branch
```

Per-turn (not per-session) is the right granularity because the previous turn's
changes are already committed to the branch before the next turn begins. HEAD
after the merge is the clean starting point for the next turn regardless of
whether it is a continuation of the same session or a new session. Session
identity is irrelevant to the isolation unit.

On session clear or end:
1. Perform a final sync in case anything was left mid-turn.
2. Remove the worktree.

## Fallback

Git worktrees require a repository with at least one commit and a working Git
installation. Worktree creation can also fail if the repository is in a
detached-HEAD state, if disk space is exhausted, or if the target path already
exists.

When worktree creation fails, Squid falls back to running the agent directly in
the real working tree (same as Option 1). In fallback mode:

- Changes are **not isolated** per turn; edits land directly in the shared
  working tree.
- The diff shown after the turn may include changes from concurrent user edits,
  other processes, or other sessions targeting the same root.
- Squid labels the displayed diff as "unscoped" so the user knows the turn
  boundary was not enforced.
- Squid logs the specific worktree error and surfaces it in the session header.

Fallback mode is temporary: if the root cause is resolved (e.g., repository
initialized, disk freed), the next turn attempts worktree creation again.

## Consequences

- Good: each turn diff is exactly scoped to that turn's agent changes.
- Good: inter-turn isolation is maintained even across session boundaries,
  because HEAD always reflects committed, merged prior work.
- Good: multiple sessions targeting different topics on the same code root do
  not interfere with each other.
- Good: the auto-commit at turn end creates a legible per-turn commit history on
  the main branch.
- Bad: worktree creation adds latency at the start of each turn.
- Bad: if Squid crashes mid-turn, stale worktree directories and branches may
  be left behind and require manual cleanup or a startup sweep.
- Bad: repositories without an initial commit (e.g., freshly `git init`) cannot
  use worktrees; fallback to direct working-tree mode applies until at least one
  commit exists.
- Bad: in fallback mode, cross-session or cross-turn change bleed is possible
  and the diff shown is not turn-scoped.
