---
status: accepted, disabled by default
date: 2026-07-09
updated: 2026-07-11
---
# ADR-0025: Per-Turn Git Worktree Isolation for Agent Changes

## Status (2026-07-11)

**Disabled by default.** The design below is implemented and unchanged, but
as of 2026-07-11 it is gated behind `WORKTREE_ISOLATION_ENABLED`
(`agent/config.py`), driven by `worktree.enabled` in `squid.yaml` (default
`false`). With the flag off, every turn runs in the "Fallback" mode described
below — agents write directly to the real working tree, diffs are unscoped,
and the model is not told it's a sole writer (`code_roots_prompt_block(...,
isolated=False)` omits that guarantee).

This was turned off, not removed, because the sync/cleanup lifecycle (see
"Sync now, remove later") was judged more operational complexity than the
project currently wants to carry — locking, merge-conflict handling, and a
background grace-period sweep — while the per-turn auto-commits it produces
just accumulate unsquashed on the source branch with no way to condense them
back into a normal-looking history. The plan is to re-enable once squash-on-push
exists (see "Planned follow-up" below), at which point per-turn commits become
an implementation detail instead of visible history noise. Set
`worktree.enabled: true` per-machine to opt back in early; the code path is
still exercised by tests and not being left to rot.

## Planned follow-up: squash-on-push

Per-turn auto-commits (`"squid: turn N"`, or the request/response-derived
message from `_build_commit_message`) are useful as an audit trail but not as
permanent history — nobody wants one commit per turn in `git log`. Before
re-enabling isolation by default, the plan is:

1. **Checkpoint ref.** A plain ref per `(topic, repo_root)`, e.g.
   `refs/squid/checkpoint/<repo_hash>-<topic>` — git's version of a bookmark —
   tracking the last point that was squashed/pushed.
2. **Explicit `push` command.** Added to the existing `/cmd` dispatcher
   (`agent/server.py`, alongside `stop`/`clear`/`compact`), never routed
   through the general chat agent, since the agent can't safely touch
   `repo_root` from its sandboxed worktree.
3. **Squash on push, git-native.** `git merge --squash` (or `rebase -i
   --autosquash`) from the checkpoint ref to `HEAD`. Git assembles `SQUASH_MSG`
   from the per-turn commit messages already being recorded — no separate
   notes or bookkeeping needed. Optionally, one cheap LLM call polishes just
   the subject line from that already-concise log.
4. **Advance the checkpoint** to the new HEAD after a successful push; refuse
   to squash past the last-known-pushed SHA, so this never needs a
   force-push or history rewrite of already-published commits.

Not started yet. Tracked here so the "why is this off" question has an answer
beyond "it got complicated."

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
ID)`, runs the agent inside it, captures a turn-scoped diff, and syncs
successful changes back to the source repository. Worktree *removal* is
deferred to a later best-effort sweep rather than happening synchronously at
turn end — see "Sync now, remove later" below.

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

`_setup_worktrees` remaps `effective_code_roots` to their worktree equivalents
via `map_to_worktree`. **CWD is not remapped.** The subprocess launch directory
(`proc_cwd`) stays as the source repo path across all turns (see ADR-0003).

The model accesses the worktree exclusively through the absolute paths in
`effective_code_roots`, which are injected into its context as
`<squid_code_roots>`. Relative path resolution from CWD is not needed and not
relied upon. This separation keeps CWD stable for agent session continuity.

`topic_queue._process` routes the two values:

- `proc_cwd` (`= item.cwd`, the source repo path) → subprocess `cwd` argument
- `display_cwd` (`= item.source_cwd or proc_cwd`, same value) →
  `save_stats`, `set_topic_session`, and the `cwd` field in the SSE stats event

The agent reads and writes files inside the worktree via absolute paths.
The original working tree is never touched during the turn. Both regular and
adhoc turns follow the same path; there is no special-casing.

**Sole-writer guarantee:** Because each turn runs in its own isolated worktree
and CWD is the source repo (not shared with any running turn), the agent is
the only process writing to `effective_code_roots` during that turn. This is
surfaced to the model in the `<squid_code_roots>` context block so it can skip
redundant re-reads and existence checks.

### Dependency directories

`git worktree add` only materializes tracked files. Installed dependencies
(`node_modules`, `.venv`, etc.) are always gitignored, so a fresh worktree
would be missing them entirely — any turn that runs the project's tooling
would need to reinstall from scratch before it could do anything.

`ensure_worktree` closes this gap by calling `_link_dependency_dirs(repo_root,
wt)` right after `git worktree add` succeeds. It walks `repo_root` up to a
bounded depth (`_DEPENDENCY_SCAN_MAX_DEPTH = 4`, skipping `.git`), and for
every directory whose name matches `config.DEPENDENCY_DIRS`, symlinks it
(not copies) from `repo_root` into the equivalent path under `wt`. Matched
directories are not recursed into further, so a nested `node_modules` inside
a matched `node_modules` is reached through the outer symlink, not linked a
second time.

`config.DEPENDENCY_DIRS` defaults to a cross-ecosystem list (`node_modules`,
`.venv`, `venv`, `env`, `.tox`, `__pypackages__`, `vendor`, `target`,
`.bundle`, `Pods`, `.cargo`, `.stack-work`, `elm-stuff`) and is overridable
per-machine via `worktree.dependency_dirs` in `~/.squid/squid.yaml`, since
dependency-directory naming is a tooling convention, not a per-topic setting.
The list intentionally only ever matches directories, never individual
files — this keeps it structurally unable to touch gitignored *state* files
(`.env`, `squid.db`, `*.log`, `config/squid.yaml`), which must stay private
to the source repo and not be shared across concurrent worktrees.

Because this runs inside `ensure_worktree`, it is naturally scoped per
`repo_root`: with multiple code roots, each unique git repo gets its own
`ensure_worktree` call and therefore its own independent scan and symlink
set — one repo's dependency dirs are never linked into another repo's
worktree.

**Safety guard:** `_link_dependency_dirs` refuses to run (logs and returns)
if `wt` resolves to the same path as `repo_root`. This exists because a
worktree that is accidentally the same path as its source repo would
otherwise symlink a dependency directory onto itself, replacing the real
directory with a broken self-referential symlink.

**Gitignore trailing slash gotcha:** entries like `.venv/` or
`tests/e2e/node_modules/` (with a trailing slash) are directory-only
gitignore patterns — Git does not match them against a symlink, even one
that points at a directory. Since these paths are always symlinks inside a
worktree, the turn-end auto-commit's `git add -A` (`commit_worktree`) would
otherwise pick them up as untracked-but-not-ignored and commit them into the
source repo as tracked `120000` (symlink) blobs on every turn. Any gitignore
entry covering a path in `config.DEPENDENCY_DIRS` must omit the trailing
slash so the pattern matches directories and symlinks alike.

Since these are symlinks and not copies, deleting one from inside a worktree
(e.g. `rm -rf node_modules`) deletes the real directory in the source repo.
Agents are not expected to write into dependency directories, but this is a
sharper edge than the rest of the worktree, where all writes are isolated.

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
  3. on success, mark_worktree_synced: DB row status → 'synced',
     last_used_at bumped to now. The worktree directory and branch are
     left in place — actual removal happens later (see "Sync now, remove
     later" below).

turn N+1 dispatch  [topic_queue.py: TopicDispatcher.dispatch]
  → fires worktree.cleanup_worktrees(topic) as a background asyncio task
  → sweep removes turn N's worktree once it is no longer active and past
    the grace period; DB row is deleted at that point
```

The diff is shown before the auto-commit; it captures the full set of changes
the agent made in the worktree during the turn, whether or not they have been
committed yet. After `sync_after_turn` those changes are committed on the
per-turn branch and merged to the source repository's current branch. The
worktree itself outlives the turn that produced it until the sweep collects it.

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

### Sync now, remove later

Removing a worktree synchronously right after its turn's CLI process exits is
unsafe: if the agent left a background process running with that worktree as
`cwd` (e.g. a `bash` tool invocation backgrounding `pytest`), deleting the
directory yanks it out from under that process mid-run. So syncing (merging
committed changes into the source repo) and removing (deleting the worktree
directory, branch, and DB row) are split into two separate steps with
different timing:

- **Sync — synchronous, at turn end.** `TopicQueue._process` calls
  `sync_after_turn` for every registry row keyed by `str(asst_msg_id)`. On
  success it calls `mark_worktree_synced(topic, agent, repo_root)`
  (`agent/stats_db.py`), which sets the row's `status` to `'synced'` and
  bumps `last_used_at`, but does **not** delete the row or touch the
  worktree directory. The source repository already has the turn's changes
  as commits at this point; only the now-inert worktree copy remains.
- **Remove — asynchronous, best-effort sweep.** `worktree.cleanup_worktrees(topic)`
  (`agent/worktree.py`) is the single removal path, shared by two triggers:
  - `TopicDispatcher._sweep_worktrees` fires it as a background
    `asyncio.create_task` on every new turn dispatch for that topic
    (`TopicDispatcher.dispatch`). It never blocks admitting the new turn;
    exceptions are logged and swallowed.
  - `server.py` calls it directly (`await cleanup_worktrees(topic)`) on
    session close/clear, so an idle topic doesn't wait for a future turn to
    reclaim its worktrees.

  The sweep loads all worktree rows for the topic
  (`get_all_worktrees_for_topic`) and, for each: skips it if `get_active_msg_ids()`
  shows its `wt_key` (the `agent` column, storing `str(asst_msg_id)`) is
  still an active turn; skips it if `last_used_at` is under
  `_CLEANUP_GRACE_SECONDS` (30s) old, giving a spawned background process
  more time to finish even after the turn itself has ended; otherwise calls
  `sync_after_turn` again (a no-op if already synced, but catches rows that
  were left mid-flight by a crash) and, if clean, `remove_worktree` +
  `delete_worktree`. This one function now handles both the steady-state
  per-turn reclaim and the orphan cleanup formerly done only at session
  close — `server.py` no longer has its own separate `_cleanup_worktrees`
  implementation.

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

This is now the default mode (see "Status" above), not just an error path.
`_setup_worktrees` is skipped entirely — and every turn runs this way —
whenever `WORKTREE_ISOLATION_ENABLED` is `false`.

It's also still the error path it always was: worktrees require a git repo
with at least one commit, and also fail on detached-HEAD state, disk
exhaustion, or if the path already exists and is corrupt. Non-git directories
have no worktree support at all. When `ensure_worktree` raises for any root
(with the flag on), `_setup_worktrees` catches the exception, logs it, and
excludes that root from the worktree map; if no roots produce a working
worktree map, the function returns the original `code_roots` and `cwd`
unchanged — the agent runs directly in the real working tree, same as the
config-disabled case.

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
- Good: successful turns sync their changes into the source repo immediately;
  worktree/branch removal is deferred to a background sweep so a background
  process the turn spawned isn't left with its `cwd` deleted out from under it.
- Good: CWD is stable across all turns (source repo path, not worktree); agent
  CLI session logs accumulate under a single `~/.claude/projects/` entry.
- Good: installed dependencies (`node_modules`, `.venv`, etc.) are symlinked
  into every fresh worktree, so turns can run project tooling immediately
  without reinstalling anything.
- Bad: dependency directories are symlinks, so deleting one from inside a
  worktree deletes the real directory in the source repo.
- Bad: gitignore entries for any path matched by `config.DEPENDENCY_DIRS`
  must not use a trailing slash, or the turn-end auto-commit will track the
  worktree symlink into the source repo's history (see "Dependency
  directories" above).
- Bad: if Squid crashes mid-turn, stale `~/.squid/worktrees/` directories and
  `sqd-*` branches can accumulate until topic close/clear or a startup sweep
  prunes them.
- Bad: a synced worktree lingers on disk for at least `_CLEANUP_GRACE_SECONDS`
  (30s) after its turn ends, and potentially longer if no new turn is
  dispatched on that topic before session close — the removal sweep only
  runs on dispatch or close, not on a standalone timer.
- Bad: non-git directories and repos without an initial commit fall back to
  direct working-tree mode; bleed is possible until the condition is fixed.
- Bad: in fallback mode, the diff is not turn-scoped and the user must be told.
- Bad: merge conflicts at session close or turn end require user intervention
  before the worktree is removed.
