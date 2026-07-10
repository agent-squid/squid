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
2. **Per-turn isolation via Git worktrees.** At the start of each turn, create
   or reuse the worktree keyed by that turn's assistant message ID. The agent
   runs inside it. At turn end, capture the diff, commit any remaining changes,
   merge to the source repo, and remove the worktree and branch on success.

## Decision

**Option 2.** Squid creates a Git worktree keyed by `(topic, assistant message
ID)`, runs the agent inside it, captures a turn-scoped diff, syncs successful
changes back to the source repository, and removes the worktree on success.

### Naming and paths

```
worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
branch name:    sqd-<slug>-<md5>
```

The key passed to the naming functions is `str(asst_msg_id)` — the assistant
message ID for this turn, which is unique across all turns. It is stored in the
`worktrees.agent` column because the table predates per-turn keys; for worktree
rows, that column is a worktree key, not the configured agent name. `<slug>` and
`<md5>` are derived from `(topic, key)`. `<repo_hash>` is 8 hex chars of
`MD5(repo_root)`. Worktrees live outside the project directory so they never
appear in the user's repo. The `sqd-` prefix lets users identify Squid-managed
branches.

Using the assistant message ID as the key means every turn — adhoc or regular —
gets a fresh, isolated worktree with no state carried over from prior turns.
The assistant message row is inserted before worktree setup so the ID is
available as the key.

### How the worktree becomes the agent's working directory

At the start of each turn, `_setup_worktrees` calls `ensure_worktree` with
`str(asst_msg_id)` as the key for each git repo under `code_roots`:

- `git worktree add -b sqd-<key> <path> HEAD` creates the worktree from the
  current HEAD (which always includes all prior turns' merged changes).

`_setup_worktrees` then remaps all paths:

- `effective_cwd` — the agent subprocess launch directory — is replaced with the
  worktree path so that relative file paths the agent resolves (e.g. `./src`)
  land inside the worktree, not the source repo.
- `effective_code_roots` — the paths injected into model context — are also
  replaced with their worktree equivalents via `map_to_worktree`.

The original `cwd` (source repo path) is preserved as `source_cwd` and passed
alongside `effective_cwd` throughout the pipeline. `topic_queue._process`
routes them to different consumers:

- `proc_cwd` (`= item.cwd`, the worktree path) → subprocess `cwd` argument
- `display_cwd` (`= item.source_cwd or proc_cwd`, the source repo path) →
  `save_stats`, `set_topic_session`, and the `cwd` field in the SSE stats event

This keeps stats and the UI showing the source repo path while the subprocess
still runs inside the isolated worktree.

**Known limitation — agent session logs:** Agent CLIs (e.g., Claude Code) key
their local session history to the process working directory. Because Squid
runs each turn in a distinct worktree path, Claude Code creates a separate
project entry in `~/.claude/projects/` per turn. Those entries accumulate until
the worktrees are pruned. There is currently no CLI flag to separate "project
directory for session logs" from "process working directory," so this cannot be
fixed without upstream changes to the agent CLI.

The agent reads and writes files inside the worktree. The original working tree
is never touched during the turn. Both regular and adhoc turns follow the same
path; there is no special-casing.

### Turn cycle

```
turn N starts  [server.py: _setup_worktrees]
  → ensure_worktree(repo_root, topic, str(asst_msg_id)) for each git root
  → save worktree row to DB: (topic, str(asst_msg_id), repo_root, wt_path, branch)
  → remap effective_cwd and effective_code_roots to worktree paths
  → take pre-turn git snapshot of the worktree for diff tracking

  [topic_queue.py: _process]
  → load worktree_sources map from DB (wt_path → repo_root) for text remapping
  → agent subprocess runs with cwd = worktree path

turn N in flight  [topic_queue.py: _stream]
  → each text chunk is passed through remap_worktree_paths() before being
    stored in the DB or forwarded to the UI; ephemeral worktree path prefixes
    are rewritten to their source repo equivalents so stored text never
    contains paths that will not exist after the worktree is removed

turn N ends  [topic_queue.py: _process]
  1. emit git diff (pre-turn snapshot vs. current worktree state,
     including any uncommitted changes still in the worktree)
  2. sync_after_turn:
     a. auto-commit any remaining uncommitted changes ("squid: turn N")
     b. merge sqd-<key> → source repository's current branch (--no-ff)
     c. rebase worktree branch onto the new source HEAD
  3. remove worktree + delete branch (every turn is ephemeral; next turn
     creates a fresh worktree from the new HEAD)
  4. delete worktree DB row
```

The diff is shown before the auto-commit; it captures the full set of changes
the agent made in the worktree during the turn, whether or not they have been
committed yet. After `sync_after_turn` those changes are committed on the
per-turn branch and merged to the source repository's current branch.

### Source of truth

The Git repository and active worktree are the source of truth for file
contents. SQLite is not the source of truth for reconstructing the working tree.
It stores:

- the temporary worktree registry needed for cleanup and source-root mapping;
- run events and the assistant message context, including the captured
  `GitDiff` event used for UI display, revert checks, search, and pinned context.

Once a turn syncs successfully, the source repository contains the actual
changes as Git commits and the DB's worktree registry row is deleted. The stored
`GitDiff` is durable history/metadata for the completed turn, not the canonical
copy of the change.

Pinned or review context built from stored messages must present the source
repository as the canonical repo. Stored `GitDiff` payloads include a `source`
field for the canonical source repo path alongside optional `worktree_repo` and
`worktree_cwd` diagnostic fields. The UI's `_gitDiffSourceRepo()` helper
resolves the path for file-open buttons by iterating `[source, repo, cwd]` and
returning the first value that is not a Squid worktree path; this ensures
buttons point to the source repo even if the `repo` field captured a worktree
path. When old assistant text contains Squid worktree paths or prior
`<changed_files>` blocks, prompt construction sanitizes that text and appends a
fresh source-repo summary from the stored `GitDiff`.

### Session close

On normal turn completion, `TopicQueue._process` calls `sync_after_turn`,
`remove_worktree`, and `delete_worktree` for every registry row keyed by
`str(asst_msg_id)`. Successful turns therefore leave no live worktree behind.

On session close or clear, `_cleanup_worktrees(topic)` runs an orphan sweep.
It first calls `get_active_msg_ids()` to get the set of `msg_id`s for all
processes currently in the registry (both regular and adhoc). Any worktree
whose `wt_key` (the `agent` column, which stores `str(asst_msg_id)`) matches
an active `msg_id` is skipped — the turn is still running and must not be
interrupted. For remaining worktrees, the sweep syncs any uncommitted changes
and removes them. This handles leftovers from crashes, cancelled runs,
exceptions during cleanup, and conflict paths.

If a code review or follow-up agent is launched while a turn is active, or
against an unresolved/stale worktree path, that agent may legitimately inspect
the worktree because Squid has remapped the topic code roots to the isolated
worktree for that run. That does not make the DB the file source of truth; it
means the worktree is the live execution root until cleanup succeeds.

### Merge conflicts

Git auto-merges changes to different hunks within the same file. A conflict only
occurs when two sessions modify the same lines.

When `merge --no-ff` fails:
- The merge is aborted immediately (`git merge --abort`).
- The worktree is left intact; changes remain committed on the per-turn branch.
- The conflicting file list is returned to the caller for surfacing in the UI.
- The per-turn branch and worktree are not removed until the conflict is resolved.

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

- Good: all turns — both regular and adhoc — are isolated in their own worktree.
- Good: each turn diff is scoped to that turn's worktree changes, isolated from
  the real working tree and from other sessions.
- Good: multiple sessions and parallel adhoc turns on the same code root are
  isolated from each other via separate worktrees on separate branches.
- Good: auto-commit at each turn end produces a per-turn commit history on the
  source repo's current branch.
- Good: successful turns clean up their isolated worktree and branch
  immediately after sync.
- Bad: if Squid crashes mid-turn, stale `~/.squid/worktrees/` directories and
  `sqd-*` branches can accumulate until topic close/clear or a startup sweep
  prunes them.
- Bad: non-git directories and repos without an initial commit fall back to
  direct working-tree mode; bleed is possible until the condition is fixed.
- Bad: in fallback mode, the diff is not turn-scoped and the user must be told.
- Bad: merge conflicts at session close or turn end require user intervention
  before the worktree is removed.
