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
that turn's changes.

## Considered Options

1. **No isolation.** Agents write directly to the real working tree. Diffs are
   computed from the pre-turn Git snapshot vs. the post-turn working tree state.
   Concurrent or cross-session changes may be included in the diff.
2. **Per-turn isolation via Git worktrees.** At the start of each turn, ensure a
   dedicated worktree exists for the `(topic, agent)` pair. The agent runs inside
   it. At turn end, commit any remaining changes, merge to main, and rebase the
   worktree onto the new HEAD — ready for the next turn without recreating it.

## Decision

**Option 2.** Squid creates a Git worktree keyed by `(topic, agent)` and keeps
it alive across turns, enforcing turn-level isolation via a commit-merge-rebase
cycle at the end of each turn.

### Naming and paths

```
worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
branch name:    sqd-<slug>-<md5>
```

`<slug>` is derived from `topic` and `agent`; `<md5>` is the first 6 hex chars
of `MD5(topic:agent)`. `<repo_hash>` is 8 hex chars of `MD5(repo_root)`.
Worktrees live outside the project directory so they never appear in the user's
repo. The `sqd-` prefix lets users identify Squid-managed branches.

### How the worktree becomes the agent's working directory

At the start of each non-adhoc turn, `_setup_worktrees` calls `ensure_worktree`
for each git repo under `code_roots`:

1. If the worktree path already exists (from a prior turn), it is reused as-is.
2. If not, `git worktree add -b sqd-<key> <path> HEAD` creates it from the
   current HEAD.

`_setup_worktrees` then remaps all paths:

- `effective_cwd` — the agent subprocess launch directory — is replaced with the
  worktree path.
- `effective_code_roots` — the paths injected into model context — are also
  replaced with their worktree equivalents via `map_to_worktree`.

The agent reads and writes files inside the worktree. The original working tree
is never touched while the worktree is live. Adhoc messages bypass this entirely
and run against the real working tree.

### Turn cycle

```
turn N starts
  → ensure_worktree (create or reuse for this topic+agent pair)
  → remap effective_cwd and effective_code_roots to worktree paths
  → take pre-turn git snapshot of the worktree for diff tracking
  → agent subprocess runs with cwd = worktree path

turn N ends
  1. emit git diff (pre-turn snapshot vs. current worktree state,
     including any uncommitted changes still in the worktree)
  2. sync_after_turn:
     a. auto-commit any remaining uncommitted changes ("squid: turn N")
     b. merge sqd-<key> → main (--no-ff)
     c. rebase worktree branch onto new main HEAD
        → worktree is clean and current for turn N+1
```

The diff is shown before the auto-commit; it captures the full set of changes
the agent made in the worktree during the turn, whether or not they have been
committed yet. After `sync_after_turn` those changes are committed on the session
branch and merged to main.

### Session close

On session close or clear, `_cleanup_worktrees` runs:
1. Final `sync_after_turn` (commit any mid-turn leftovers + merge + rebase).
2. `git worktree remove` + `git branch -D sqd-<key>` if merge was clean.
3. Worktrees with unresolved conflicts are kept alive (see below).

### Merge conflicts

Git auto-merges changes to different hunks within the same file. A conflict only
occurs when two sessions modify the same lines.

When `merge --no-ff` fails:
- The merge is aborted immediately (`git merge --abort`).
- The worktree is left intact; changes remain committed on the session branch.
- The conflicting file list is returned to the caller for surfacing in the UI.
- The session branch and worktree are not removed until the conflict is resolved.

## Fallback

Worktrees require a git repo with at least one commit. They also fail on
detached-HEAD state, disk exhaustion, or if the path already exists and is
corrupt. Non-git directories have no worktree support at all.

When `ensure_worktree` raises for any root, `_setup_worktrees` catches the
exception, logs it, and excludes that root from the worktree map. If no roots
produce a working worktree map, the function returns the original `code_roots`
and `cwd` unchanged — the agent runs directly in the real working tree.

In fallback mode:
- Changes land directly in the shared working tree (Option 1 behavior).
- The diff may include concurrent user edits or changes from other sessions.
- Squid labels the diff as "unscoped" and surfaces the worktree error in the
  session header.
- The next turn retries worktree creation; fallback is not permanent.

## Consequences

- Good: each turn diff is scoped to that turn's worktree changes, isolated from
  the real working tree and from other sessions.
- Good: the worktree persists across turns so there is no per-turn setup cost
  after the first turn.
- Good: multiple sessions on the same code root are isolated from each other via
  separate worktrees on separate branches.
- Good: auto-commit at each turn end produces a per-turn commit history on main.
- Bad: if Squid crashes mid-turn, stale `~/.squid/worktrees/` directories and
  `sqd-*` branches can accumulate; a startup sweep is needed to prune them.
- Bad: non-git directories and repos without an initial commit fall back to
  direct working-tree mode; bleed is possible until the condition is fixed.
- Bad: in fallback mode, the diff is not turn-scoped and the user must be told.
- Bad: merge conflicts at session close or turn end require user intervention
  before the worktree is removed.
