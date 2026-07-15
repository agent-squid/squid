---
status: design revised; shipped code (see Reference) unchanged, disabled by default
date: 2026-07-09
updated: 2026-07-14
---
# ADR-0025: Per-Turn Git Worktree Isolation for Agent Changes

## Status

**Disabled by default.** `WORKTREE_ISOLATION_ENABLED` (`agent/config.py`, driven
by `worktree.enabled` in `squid.yaml`, default `false`) gates the whole
feature. With it off, every turn runs in **Fallback** (below): agents write
directly to the real working tree, diffs are unscoped, and the model is not
told it's a sole writer.

This ADR previously proposed "commit each turn on a per-turn branch, then
`merge --no-ff` into the source repo" as the sync mechanism, and left it
disabled because the resulting per-turn commit trail (`"squid: turn N"`)
had no story for turning back into normal-looking history short of a whole
new squash-on-push subsystem. A later draft tried to avoid that trail with
raw `git stash create` / `stash apply`, but that is not a reliable merge
primitive for dirty worktrees. The target design is now **hidden snapshot
commits + off-repo merge + patch promotion**: Squid may create internal commit
objects/refs for preprocessing and merge mechanics, but it never advances the
user's branch or creates user-visible per-turn commits. The commit+merge
version is still what's actually implemented (see **Reference: current
implementation**); migrating to the design below is the open follow-up work.

### Re-enabling the shipped implementation

Not shipped in `config/squid.yaml.example` — keeping an experimental,
off-by-default block out of the file most users copy from. To opt in, add
this to `~/.squid/squid.yaml`:

```yaml
worktree:
  enabled: true
  dependency_dirs:
    - node_modules
    - .venv
    - venv
    - env
    - .tox
    - __pypackages__
    - vendor
    - target
    - .bundle
    - Pods
    - .cargo
    - .stack-work
    - elm-stuff
```

`dependency_dirs` controls which directories `ensure_worktree` symlinks
(not copies) from a code root into each fresh worktree, so installed
dependencies don't need reinstalling there — matched by directory name, not
recursed into once matched, never applied inside `.git`. If you add an
entry, its `.gitignore` pattern must have no trailing slash (`vendor`, not
`vendor/`) — see "Dependency directories" under Reference for why.

## Context

Squid runs coding agents (Claude, Codex, etc.) against real repository paths
declared as topic code roots. When an agent edits files during a turn, those
changes land directly in the working tree. This creates two problems:

1. **Bleed between turns.** Uncommitted changes from turn N are visible to the
   agent in turn N+1 before they've been reviewed, mixing in-progress state
   with the clean starting point.
2. **Bleed between sessions.** If two topics share a code root, an agent
   editing for one topic can interfere with another topic's diff tracking or
   working state.
3. **Observed data loss under concurrent writes (not just bleed).** With
   isolation off — today's default — two parallel prompts (e.g. two adhoc
   turns, or an adhoc turn racing a regular one, per ADR-0010) writing
   directly to the same real working tree is a plain filesystem overwrite,
   not a Git operation. There is no line-based 3-way merge and no conflict
   marker to catch it: if turn A's write lands on a file after turn B wrote
   it, B's change is simply gone, with nothing surfaced anywhere. This has
   happened in practice — a change was silently overwritten by another
   parallel prompt's output, with no diff, warning, or conflict to flag it.
   This is strictly worse than "bleed" (seeing stale/uncommitted state);
   it's unrecoverable loss with no signal that it occurred.

Isolating each turn also makes it trivial to compute a diff scoped to exactly
that turn's changes.

## Considered Options

1. **No isolation.** Agents write directly to the real working tree. Diffs
   are computed from the pre-turn Git snapshot vs. the post-turn working
   tree state. Concurrent or cross-session changes may be included.
2. **Per-turn isolation via Git worktrees, synced by commit + merge.** Create
   a worktree per turn, auto-commit whatever's left uncommitted at turn end,
   merge that commit into the source repo. This is what's currently shipped
   (disabled by default) — see Reference.
3. **Per-turn isolation via Git worktrees, synced by stash relay.** Create a
   worktree per turn; instead of committing, seed it from the source repo's
   current dirty tree and, at turn end, drain its changes back into the
   source repo's dirty tree — via `git stash create`/`apply`, never `commit`
   or `merge`. Rejected; see Rejected alternatives.
4. **Per-turn isolation via hidden snapshot commits and patch promotion.**
   Capture repo_root's dirty tree into internal tree/commit objects, run the
   agent in a worktree seeded from that hidden snapshot, merge concurrent
   updates in a disposable integration worktree, then promote the resolved
   delta back to repo_root as ordinary unstaged local file changes.

## Decision

**Option 4 is the target design.** It keeps everything Option 2 got right —
isolation during the turn, a turn-scoped diff, and parallel-safe merge
semantics — while avoiding both bad outcomes: no permanent per-turn commit
trail on the user's branch, and no fragile dirty-worktree stash apply. Squid is
allowed to create internal commits/refs because they are preprocessing state,
not product history: they are not merged into the user's branch, not shown as
"turn commits", and can be deleted after sync/conflict resolution. The source
repo's working tree remains the user's ordinary dirty tree. After promotion,
`git status` and `git commit` behave exactly as if the edits had been made
locally in repo_root.

This is not implemented yet. It supersedes the "Planned follow-up:
squash-on-push" and "Alternative considered: stash-based no-commit merge"
sections of the prior version of this ADR; both are now resolved by the hidden
snapshot/patch-promotion design rather than left as unadopted alternatives.

### Target design: hidden snapshots, off-repo merge, patch promotion

The implementation should treat Git commits as an internal merge data
structure, not as user history. Squid may create temporary commits and refs
under its own namespace, but those objects never advance the user's branch and
never become the commit history the user sees.

```
turn starts
  → capture repo_root's dirty working-tree content as SEED_TREE
  → create hidden BASE_COMMIT from SEED_TREE
  → create per-turn worktree from BASE_COMMIT
  → run the agent against that worktree

turn ends
  → capture the turn worktree as TURN_TREE
  → compute the visible GitDiff as SEED_TREE..TURN_TREE
  → capture repo_root's current dirty content as CURRENT_TREE
  → merge TURN_TREE with CURRENT_TREE in a disposable integration worktree
  → if clean and repo_root has not drifted, patch MERGED_TREE into repo_root
  → if conflicted, keep the integration worktree and surface the conflict
```

After promotion, repo_root contains ordinary local file changes. `git status`
and `git commit` behave as if the edits had been made directly in repo_root.

### Naming and stored state

Per-turn worktree paths can keep the current shape:

```
turn worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
turn branch/ref:      sqd-<slug>-<md5> or detached hidden commit
```

The key remains `(topic, str(asst_msg_id))`; see Reference for the current
derivation. The target design also needs registry fields for internal state:

- `seed_tree` / `base_commit`: repo_root's content when the turn started.
- `turn_tree` / `turn_commit`: the agent worktree content when the turn ended.
- `current_tree` / `current_commit`: repo_root's content at the last merge
  attempt.
- `merged_tree` / `merge_commit`: the clean integration result, when one
  exists.
- `integration_worktree_path`: the disposable worktree that holds conflict
  markers while `sync_status = 'conflict'`.
- `sync_status`: `pending`, `conflict`, or `promoted`.

These commits can live under Squid-owned refs such as
`refs/squid/worktrees/<topic>/<turn-key>/...` while active. Once promotion or
abandonment finishes, Squid deletes the refs and lets ordinary Git GC reclaim
the unreachable objects later.

### Capturing content trees

Squid needs a portable "content tree" capture helper. Under
`_lock_for(repo_root)`, build a scratch index from `HEAD`, overlay the
working-tree content, remove tracked deletions, and add non-ignored untracked
files. Ignored files and dependency/state directories remain ignored.

This replaces the rejected `git stash create` seed step. It has two important
properties:

- It captures tracked and non-ignored untracked files in one tree model, so
  untracked same-path additions can participate in the later 3-way merge.
- It does not mutate repo_root's working tree, index, branch, or `refs/stash`.

The snapshot is still a read of a live directory, so a user's editor can race
it with a partial save. Holding `_lock_for(repo_root)` prevents Squid from
observing its own mid-promotion state, but it cannot make the user's editor a
sole writer.

### Merge and conflict artifact

At turn end, Squid creates an artificial ancestry graph for Git's merge
machinery:

```
BASE_COMMIT     tree = SEED_TREE
CURRENT_COMMIT  tree = CURRENT_TREE, parent = BASE_COMMIT
TURN_COMMIT     tree = TURN_TREE,    parent = BASE_COMMIT
```

Squid checks out `CURRENT_COMMIT` in a disposable integration worktree and runs
a normal Git merge of `TURN_COMMIT` there. repo_root is not written during this
merge. Clean merges produce `MERGED_TREE`; conflicts remain entirely inside the
integration worktree.

For every path in:

```bash
git -C <integration_wt> diff --name-only --diff-filter=U
```

the conflict artifact is:

| side | source | meaning |
|---|---|---|
| base | `git show :1:<path>` | repo_root's content when the turn started |
| ours | `git show :2:<path>` | repo_root's current content at merge time |
| theirs | `git show :3:<path>` | this turn's output |
| marked-up | file in `<integration_wt>` | Git's conflict-marked file |

This reverses the old stash draft's "ours/theirs" labels because the
integration worktree starts from `CURRENT_COMMIT` and merges `TURN_COMMIT`.
The UI can present friendlier labels such as "current repo" and "turn output"
to avoid exposing Git's perspective.

### Promotion and drift

After a clean merge or a resolved conflict, Squid re-acquires
`_lock_for(repo_root)` and captures repo_root again:

- **Unchanged:** if repo_root still matches `CURRENT_TREE`, Squid applies
  `git diff --binary CURRENT_TREE MERGED_TREE` to repo_root with
  `git apply --check` followed by `git apply` without `--index`. This updates
  ordinary working-tree files and leaves the user's staging choices alone.
- **Drifted:** if repo_root no longer matches `CURRENT_TREE`, Squid captures a
  new current tree, rebuilds `CURRENT_COMMIT` parented to the original
  `BASE_COMMIT`, and repeats the same integration merge. There is no separate
  second-round mechanism.

`BASE_COMMIT` stays anchored to the original `SEED_TREE` for every retry.
The integration worktree's resolved tree becomes the new turn side for the
retry, so already-resolved content is preserved while genuinely new repo_root
drift is merged or reported as a new conflict.

Unbounded contention is still possible: if repo_root keeps drifting faster than
resolution can finish, Squid can keep finding fresh drift. The implementation
should cap retries and surface a stuck-sync error instead of looping forever.

### Diff-viewer implications

The diff shown to the user remains Squid's own output, not the model's:
`GitChangeTracker.build_event()` computes it from tree snapshots. Under the
target design, the diff is `SEED_TREE..TURN_TREE` and is durable as soon as
the turn ends, regardless of whether promotion has completed.

The gap is repo_root visibility. Until promotion succeeds, repo_root does not
contain the turn output, so existing actions that resolve to repo_root can look
wrong:

- `revert_diff` reverse-applies stored diff text against repo_root. It should
  be unavailable while the diff is not promoted.
- "Open file" against repo_root can show pre-turn content while a conflict is
  pending.

Fix: store `sync_status` with the `GitDiff` event or the associated worktree
registry row. `get_diff_revert_eligibility` gains a `pending` state, checked
before `revertable` / `conflicting` / `reverted`, and the UI hides stale
repo_root file links while `sync_status != 'promoted'`.

### Conflict resolution across turns

Conflict resolution should reuse the **integration worktree**, not the original
turn worktree. The turn worktree contains the agent's output; the integration
worktree contains the actual 3-way merge against current repo_root, including
Git's conflict markers and index stages.

The conflict list should be stored as a first-class tool event, for example
`MergeConflict`, on the assistant message that hit the conflict. Its shape is
the conflict tuple above plus `repo`, `integration_worktree_path`, and the
internal commit/tree ids needed for retry. Add a sibling
`_conflict_context_summary()` to `_gitdiff_context_summary()` so a follow-up
LLM turn receives a `<merge_conflict>` block with the same base/current/turn
content the UI shows.

Resolution turns need a special case before ordinary `_setup_worktrees`:

- If `(topic, repo_root)` has an open `sync_status = 'conflict'` row, route the
  follow-up turn to the existing `integration_worktree_path`.
- Do not mint a fresh per-turn worktree for "ask AI to resolve"; a fresh
  worktree would be seeded from repo_root and would not contain the conflict.
- Once no unmerged entries remain, capture the integration worktree as the
  candidate `MERGED_TREE` and run the promotion/drift check again.

The UI needs conflict-specific actions, separate from revert:

- **Ask AI to resolve:** dispatch a follow-up turn scoped to the integration
  worktree with the `<merge_conflict>` summary.
- **Retry merge:** after manual edits in the integration worktree, verify
  `git status --porcelain` has no unmerged entries, then attempt promotion.

Both paths end with `sync_status = 'promoted'` on success or
`sync_status = 'conflict'` with a refreshed conflict artifact if repo_root
drift collides again.

### Commit requests

An agent may still run `git commit` inside its isolated turn or integration
worktree if the user asks it to "commit it." That commit is not user history by
default, because it lands outside the source branch. Sync should promote the
resulting file tree to repo_root the same way as any other turn output.

A user-visible commit, if Squid supports one, must happen only after successful
promotion and must run in repo_root against the promoted local changes. That
keeps the mental model simple: Squid sync creates local changes; committing
those changes is a separate normal repo_root operation.

### Rejected alternatives

- **Commit + merge into the source branch.** This is the shipped implementation
  behind the disabled flag. It gives good merge semantics, but creates a
  permanent per-turn commit trail on the user's branch.
- **Raw stash relay.** `git stash create` is useful as a read-only snapshot of
  tracked dirty state, but it is not the target merge primitive:
  `stash create` does not capture untracked files, `stash apply` onto a dirty
  worktree can abort before producing conflict markers, and Git has no
  `stash create relative to arbitrary BASE` mode for promotion.
- **Apply into repo_root, then restore on conflict.** This writes failed merge
  attempts into the user's real directory and then tries to undo them without a
  single Git abort primitive. The target design avoids that by keeping risky
  merges inside the integration worktree.

### What changes vs. the shipped implementation

- No source-branch per-turn commits, no `merge --no-ff` into repo_root, and no
  squash-on-push subsystem.
- repo_root stays dirty across turns until the user commits, matching ordinary
  local editing.
- Turn chaining comes from capturing repo_root's dirty content into the next
  turn's seed tree, not from advancing `HEAD`.
- Conflicts are resolved in a disposable integration worktree, never in
  repo_root.
- Dependency directories are still symlinked into fresh worktrees the same way
  as today.
- CWD still needs a separate enforcement decision: if process CWD remains
  repo_root, relative writes can bypass isolation unless Squid changes CWD or
  adds tool/path enforcement.
- Fallback behavior and the sync/error semantics around it remain as below.

## Fallback

Whenever `WORKTREE_ISOLATION_ENABLED` is `false` — the default — or
`ensure_worktree` raises for a root (not a git repo, no initial commit,
detached HEAD, disk exhaustion, corrupt path), that root is excluded from
the worktree map. If no root produces a working worktree, the turn runs
directly against the real working tree, same as Option 1:

- Changes land directly in the shared working tree.
- The diff may include concurrent edits from the user or other sessions.
- Squid labels the diff "unscoped" and surfaces the worktree error in the
  session header.
- The next turn retries worktree creation; fallback is not permanent.
- **This is the exact mode "Observed data loss under concurrent writes" (in
  Context) happens in.** Two turns writing straight to repo_root is a raw
  filesystem overwrite with no Git operation involved, so there's no 3-way
  merge and nothing to surface — unlike a real conflict under Option 2 or 4,
  which at minimum aborts and reports conflicting files, a fallback-mode
  collision can silently drop one turn's change with no trace. Isolation
  (Option 2 or 4) doesn't just scope diffs; it's what turns this class of
  loss into an ordinary, visible, line-based merge/conflict instead.

## Consequences

- Good: turns are isolated in their own worktree; diffs are turn-scoped.
- Good: multiple sessions/parallel adhoc turns on the same code root are
  isolated from each other.
- Good (target design): no commit-trail noise on the source branch, and no
  squash-on-push subsystem is needed to clean one up.
- Good (target design): internal commits/refs are allowed for preprocessing,
  but they are not user history and never advance the user's branch.
- Good (target design): repo_root ends each turn in the same kind of state
  a human editing it directly would leave it in — dirty, uncommitted,
  ready for the user's own `git commit` whenever they choose.
- Good (target design): conflicts are never written into repo_root — the
  merge happens entirely inside the integration worktree, so a conflict
  leaves the user's real directory untouched rather than mid-merge.
- Good (target design): conflict resolution has a structured base/ours/
  theirs form per file, so it can be handed to the user *or* to an LLM in a
  follow-up turn — resolution isn't limited to a human reading raw
  conflict markers.
- Good (target design): the diff shown to the user is computed by Squid
  from git tree snapshots at turn end regardless of conflict/promotion
  status — it's never blocked on, or generated from, anything the model
  outputs (see "Diff-viewer implications").
- Bad (target design): revert and "open file" resolve to repo_root, which
  can lag the displayed diff for as long as a conflict takes to resolve —
  a gap that doesn't exist in the shipped synchronous commit+merge design.
  Needs an explicit `sync_status` (`pending`/`conflict`/`promoted`) so the UI
  doesn't offer revert against a file repo_root doesn't have yet.
- Bad (target design): repo_root is dirty more of the time than under the
  shipped commit+merge design, which gave each turn a clean commit boundary
  automatically. There is no longer an automatic point-in-time marker for
  "this is what turn N produced" beyond the stored `GitDiff` and the
  temporary internal snapshot refs/worktrees (until they are swept).
- Bad (target design): a user-visible commit cannot be a raw `git commit`
  performed inside the isolated worktree. Squid must promote the file state to
  repo_root first, then commit in repo_root if it supports an explicit commit
  action.
- Bad (all worktree designs): dependency directories are symlinks — deleting one
  from inside a worktree deletes the real directory in repo_root.
- Bad (all worktree designs): if Squid crashes mid-turn, stale worktree
  directories and branches can accumulate until the sweep or a startup pass
  prunes them.
- Bad (all worktree designs): non-git directories and repos without an initial
  commit fall back to direct working-tree mode; bleed is possible until
  fixed.
- Bad (all worktree designs): in fallback mode, the diff is not turn-scoped
  and the user must be told.
- Bad (target design): conflicts still require resolution (by the user or an
  LLM turn) before the worktree is promoted and removed — this design
  changes where a conflict is safe to sit, not whether one can happen.
- Bad (target design): promotion re-checks repo_root for drift since
  `CURRENT_TREE` was captured, so a long-running resolution can loop
  (re-merge against a newer current tree) rather than promote on the first
  attempt.
- Bad (target design): a resolution turn must not get the normal fresh
  per-turn worktree — `_setup_worktrees` needs a new special case that
  detects an open `sync_status = 'conflict'` row for `(topic, repo_root)`
  and routes the turn to its `integration_worktree_path` instead of minting a
  new worktree for the new message id. Without it, "ask AI to resolve" would
  silently redo the original turn's work in a brand-new worktree that never
  saw the conflict at all (see "Conflict resolution across turns").
- Bad (target design): the conflict list needs its own stored event and
  prompt-summary function (`MergeConflict` / `_conflict_context_summary`,
  mirroring `GitDiff` / `_gitdiff_context_summary`) before a follow-up LLM
  turn can be handed it as context — not implemented today, since neither
  the event type nor the integration-worktree routing path above currently
  exist.

---

## Reference: current implementation (commit + merge --no-ff)

This is what's actually shipped, gated off by default (see Status). It
predates the hidden snapshot/patch-promotion design above and remains in place
until that design is built. Kept here as the accurate record of current
behavior, not as the recommended direction for new work.

### Naming and paths

```
worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
branch name:    sqd-<slug>-<md5>
```

The key passed to the naming functions is `str(asst_msg_id)` — unique per
turn. It's stored in the `worktrees.agent` column because that table
predates per-turn keys; for worktree rows the column holds a worktree key,
not a configured agent name. `<slug>`/`<md5>` derive from `(topic, key)`;
`<repo_hash>` is 8 hex chars of `MD5(repo_root)`. Worktrees live outside the
project directory so they never appear in the user's repo; the `sqd-`
prefix identifies Squid-managed branches.

Using the assistant message ID as the key means every turn — adhoc or
regular — gets a fresh, isolated worktree with no state carried over. The
assistant message row is inserted before worktree setup so the ID is
available as the key.

### How the worktree becomes the agent's working directory

`_setup_worktrees` calls `ensure_worktree(repo_root, topic, str(asst_msg_id))`
for each git repo under `code_roots`, which runs
`git worktree add -b sqd-<key> <path> HEAD` — built from current `HEAD`,
which includes all prior turns' merged changes. `effective_code_roots` is
remapped to the worktree paths; **CWD is not remapped** (`proc_cwd` stays
the source repo path across all turns — ADR-0003). The model reaches the
worktree only through the absolute paths in `<squid_code_roots>`.
`topic_queue._process` routes `proc_cwd` (source repo path) to the
subprocess `cwd` and `display_cwd` to stats/session-tracking/SSE.

**Sole-writer guarantee:** because each turn has its own worktree and CWD is
never shared with a running turn, the agent is the only writer to
`effective_code_roots` during that turn — surfaced to the model via
`<squid_code_roots>` so it can skip redundant re-reads.

### Dependency directories

`ensure_worktree` calls `_link_dependency_dirs(repo_root, wt)` right after
`git worktree add` succeeds: it walks `repo_root` to a bounded depth
(`_DEPENDENCY_SCAN_MAX_DEPTH = 4`, skipping `.git`) and symlinks (not
copies) any directory matching `config.DEPENDENCY_DIRS` into the equivalent
path under `wt`, without recursing into a matched directory. The default
list (`node_modules`, `.venv`, `venv`, `env`, `.tox`, `__pypackages__`,
`vendor`, `target`, `.bundle`, `Pods`, `.cargo`, `.stack-work`, `elm-stuff`)
is overridable via `worktree.dependency_dirs`. It only ever matches
directories, never individual files, so it can't touch gitignored *state*
files (`.env`, `squid.db`, `*.log`, `config/squid.yaml`) that must stay
private to repo_root.

**Safety guard:** `_link_dependency_dirs` refuses to run if `wt` resolves
to the same path as `repo_root`, which would otherwise symlink a dependency
directory onto itself.

**Gitignore trailing-slash gotcha:** patterns like `.venv/` (trailing
slash) are directory-only and don't match a symlink even one pointing at a
directory. Since these paths are always symlinks inside a worktree, the
turn-end `git add -A` would otherwise track them into repo_root's history
as `120000` (symlink) blobs. Any gitignore entry covering a path in
`config.DEPENDENCY_DIRS` must omit the trailing slash.

Because these are symlinks, deleting one from inside a worktree (e.g.
`rm -rf node_modules`) deletes the real directory in repo_root.

### Turn cycle

```
turn N starts  [server.py: _setup_worktrees]
  → ensure_worktree for each git root; save worktree row to DB
  → remap effective_cwd / effective_code_roots to worktree paths
  → pre-turn git snapshot of the worktree for diff tracking

turn N in flight  [topic_queue.py: _stream]
  → each text chunk passes through remap_worktree_paths() so stored/forwarded
    text never contains paths that won't exist once the worktree is removed

turn N ends  [topic_queue.py: _process]
  1. emit git diff (pre-turn snapshot vs. current worktree state)
  2. sync_after_turn:
     a. commit_worktree: auto-commit remaining changes ("squid: turn N",
        or request/response-derived via _build_commit_message)
     b. merge_worktree: merge sqd-<key> → source branch, --no-ff
     c. rebase worktree branch onto the new source HEAD
  3. on success, mark_worktree_synced: DB row status → 'synced',
     last_used_at bumped. Worktree directory/branch stay in place —
     removal is deferred (below).

turn N+1 dispatch  [topic_queue.py: TopicDispatcher.dispatch]
  → fires worktree.cleanup_worktrees(topic) as a background task
  → sweep removes turn N's worktree once inactive and past grace period
```

### Sync now, remove later

Removing a worktree synchronously right after the CLI process exits is
unsafe — a backgrounded process (e.g. a `bash` tool call backgrounding
`pytest`) could still have it as `cwd`. So sync (merge into repo_root) and
remove (delete worktree dir, branch, DB row) are split:

- **Sync — synchronous, at turn end.** `sync_after_turn` runs for every
  registry row keyed by `str(asst_msg_id)`. On success, `mark_worktree_synced`
  sets `status='synced'` and bumps `last_used_at`, but doesn't delete the
  row or worktree — repo_root already has the turn's changes as commits;
  only the now-inert worktree copy remains.
- **Remove — asynchronous, best-effort sweep.** `cleanup_worktrees(topic)`
  is the single removal path, triggered by `TopicDispatcher._sweep_worktrees`
  on every new turn dispatch (background task, exceptions logged and
  swallowed) and by `server.py` directly on session close/clear. It skips
  rows whose key is an active turn (`get_active_msg_ids()`) or younger than
  `_CLEANUP_GRACE_SECONDS` (30s); otherwise re-runs `sync_after_turn`
  (a no-op if already synced, catching crash-orphaned rows) and, if clean,
  `remove_worktree` + `delete_worktree`.

### Merge conflicts

Git auto-merges changes to different hunks in the same file; a conflict
only occurs when two sessions modify the same lines. When `merge --no-ff`
fails: the merge is aborted immediately (`git merge --abort`), the worktree
is left intact with changes still committed on the per-turn branch, the
conflicting file list is returned for the UI, and the branch/worktree stay
until the conflict is resolved.

### Source of truth

The Git repository and active worktree are the source of truth for file
contents; SQLite stores only the temporary worktree registry and run
events/diffs, not a reconstructable copy of the tree. Once a turn syncs,
repo_root has the changes as commits and the DB's worktree row is deleted.
Stored `GitDiff` payloads include a `source` field for the canonical repo
path alongside optional `worktree_repo`/`worktree_cwd` diagnostic fields;
the UI's `_gitDiffSourceRepo()` iterates `[source, repo, cwd]` and returns
the first non-worktree path so file-open buttons always point at repo_root.
