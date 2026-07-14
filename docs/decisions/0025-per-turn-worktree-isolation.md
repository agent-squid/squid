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
new squash-on-push subsystem. That design is superseded by the one below —
**seed/drain via `git stash`, no commits, ever** — which needs no squash
step because it never creates a commit trail to squash. The commit+merge
version is still what's actually implemented (see **Reference:
current implementation**); migrating to the design below is the open
follow-up work.

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
   or `merge`.

## Decision

**Option 3 is the target design.** It keeps everything Option 2 got right —
isolation during the turn, a turn-scoped diff, a sole-writer guarantee — and
drops the one thing that made Option 2 not worth turning on: a permanent,
ever-growing commit trail that only a not-yet-built squash-on-push feature
could clean up. Option 3 needs no such feature, because it never commits
anything on the user's behalf. The source repo's working tree stays exactly
what it always was: the place the user's dirty, uncommitted changes live,
same as if they'd typed the edits themselves. Committing remains a manual,
ordinary `git commit` — Squid never does it for them.

This is not implemented yet. It supersedes the "Planned follow-up:
squash-on-push" and "Alternative considered: stash-based no-commit merge"
sections of the prior version of this ADR; both are now resolved by the
design below rather than left as unadopted alternatives.

### Naming and paths

Unchanged from the current implementation:

```
worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
branch name:    sqd-<slug>-<md5>
```

keyed by `(topic, str(asst_msg_id))`; see Reference for the full derivation.
The branch still exists (for identifiability of an in-flight turn's
worktree) but, unlike today, nothing ever commits onto it — with no commits
landing there, `HEAD` also never advances on the source repo's branch, and
every fresh worktree is built from the same base commit until the user
commits by hand. Carrying the turn's actual changes across turns is the
seed/drain relay's job, not `HEAD`'s.

### Turn cycle

```
turn N starts  [worktree.ensure_worktree]
  → git worktree add -b sqd-<key> <path> HEAD   (unchanged)
  → seed: relay repo_root's current dirty tree into the fresh worktree
  → take pre-turn snapshot of the seeded worktree, for diff scoping

turn N runs
  → agent reads/writes the worktree exclusively; sole writer, same as today

turn N ends
  → diff = seeded-snapshot vs. current worktree state (turn-scoped, as today)
  → drain: relay the worktree's changes back into repo_root's dirty tree
  → on success: mark_worktree_synced (unchanged); worktree removal deferred
    to the same background sweep as today ("sync now, remove later")
  → on conflict: repo_root is restored to its pre-drain state exactly;
    the worktree (and its changes) is left intact, not removed; conflicting
    files are surfaced to the UI
```

### Seed: relaying repo_root's dirty state into a fresh worktree

`git worktree add ... HEAD` only ever materializes committed state. If
repo_root has uncommitted changes when a turn starts — the user's own edits,
or a prior turn's drained-but-uncommitted output — the fresh worktree
wouldn't see them unless seeded explicitly.

`git stash create` builds a commit object representing the current index +
working tree diff against `HEAD`, **without touching repo_root's index,
working tree, or `refs/stash`** — unlike `stash push`, this is read-only on
the source repo:

```bash
# unchanged: worktree still built from HEAD
git worktree add -b sqd-<key> <path> HEAD

# read-only snapshot of repo_root's dirty tracked state
SHA=$(git -C <repo_root> stash create -m "turn <asst_msg_id> seed")

# apply into the new worktree, not repo_root
git -C <path> stash apply --index "$SHA"
```

`stash create` never captures untracked files, regardless of flags passed
(confirmed directly: passing `-u` silently becomes literal text in the
commit message, not a flag). Untracked files need a second, equally
read-only step:

```bash
git -C <repo_root> ls-files --others --exclude-standard -z |
  xargs -0 -I{} cp --parents {} <path>/
```

If `SHA` is empty (nothing dirty in repo_root), the apply step is skipped.

**Races and how they're handled:**

- **Torn snapshot across the two-part capture.** Tracked state comes from
  one instant (`stash create`); untracked files from a later one (`ls-files`
  + copy). If the user's editor is mid-save across several files exactly
  then, the combined snapshot can mix pre- and post-edit state. No
  mitigation beyond narrowing the window between the two calls — inherent
  to capturing dirty state from a repo that isn't sole-writer.
- **Read-during-write on individual files.** Same root cause, one level
  down — both calls are plain reads of a live tree and can observe a
  torn/partial write. The rest of this design sidesteps this for the
  worktree itself (nothing but that turn's agent ever writes there); it
  can't be engineered away for repo_root, since the user's own editor is the
  reason seeding exists.
- **Concurrent seeds across topics sharing a code root are fine.**
  `stash create` touches no shared git state (`.git/index`, `refs/stash`),
  so it can't lock-contend the way `push`/`pop` would. N topics can seed
  from the same repo_root concurrently with zero interference.
- **Seed racing an in-flight drain on the same repo.** If Topic B's
  turn-start seed runs while Topic A's turn-end drain is applying to that
  same repo_root, repo_root can transiently be mid-apply (unmerged index
  stages). **Mitigation:** the per-repo lock (`_lock_for(repo_root)`,
  already used by the shipped sync path) is held across both seed reads and
  drain writes, so a seed never observes a mid-drain repo_root.
- **Untracked file vanishes or moves between listing and copying.** A
  second, smaller TOCTOU window — editors that save via
  temp-file-then-rename can make a path transiently disappear.
  **Mitigation:** treat each file's copy failure as a soft per-file
  skip+log, not an abort of the whole seed.

None of these lose data silently — worst case is a slightly stale seed, not
corruption, and the sole-writer guarantee for the worktree itself (below)
is untouched.

### Drain: relaying the worktree's changes back into repo_root

The reverse of seeding, run at turn end instead of turn start, and it
**writes** to repo_root instead of only reading it — so it needs the
per-repo lock for its full duration, not just to avoid observing a
mid-drain state but because it causes one:

```bash
# under _lock_for(repo_root):

# 1. safety snapshot of repo_root's current dirty state, read-only
PRE = $(git -C <repo_root> stash create -m "pre-drain safety snapshot")

# 2. snapshot of the worktree's changes since it was seeded, read-only
SHA = $(git -C <path> stash create -m "turn <asst_msg_id> drain")

# 3. apply into repo_root
git -C <repo_root> stash apply --index "$SHA"
# + copy the worktree's untracked files into repo_root the same way
#   seeding does it, in reverse
```

If step 3 applies cleanly, repo_root's working tree now holds the turn's
changes, dirty and uncommitted — deliberately: this is the whole point of
dropping commits from the design. No commit trail accumulates on the
source branch; the user's own `git commit`, whenever they choose to run it,
is what turns these changes into history, same as if they'd typed them.

If step 3 conflicts (a same-line overlap between what the worktree started
from and what's now sitting in repo_root — e.g. the user hand-edited the
same lines mid-turn, or a concurrent topic's drain landed first): restore
repo_root's tree to `PRE` exactly (`git checkout <tree of PRE> -- .`, plus
re-copying `PRE`'s untracked set) so repo_root is never left mid-conflict or
partially applied. Surface the conflicting file list to the UI. The turn's
actual changes are not lost — they remain in the still-intact worktree,
which is not removed, so the user can resolve and retry the drain manually.

Non-conflicting hunks in the same file still auto-merge silently via the
same 3-way logic `git apply`/`stash apply` always uses — that's inherent to
3-way merge, not new to this design, and matches how `git merge` already
behaved for non-overlapping hunks in the shipped implementation.

Worktree removal is unaffected: still deferred to the existing background
sweep ("sync now, remove later" in Reference), since the worktree is safe to
discard once drained, same reasoning as today (a spawned background process
might still have it as `cwd`).

### What this changes vs. the shipped implementation

- No per-turn commits, no `merge --no-ff`, no rebase, no branch that
  accumulates history. The `sqd-<key>` branch still exists as the
  worktree's anchor but never gains commits of its own.
- No squash-on-push subsystem needed — there's nothing to squash.
- repo_root now stays dirty across turns until the user commits, instead of
  gaining one commit per turn automatically. This is an intentional product
  choice, not a gap: it's the same state a user editing by hand would be in.
- Conflict handling moves from "abort a merge" to "restore repo_root to its
  pre-drain snapshot" — same guarantee (nothing is lost, nothing is left
  half-applied), different mechanism.
- Turn-chaining no longer depends on `HEAD` advancing (it doesn't, since
  nothing commits) — it depends on the seed step explicitly relaying
  whatever's dirty in repo_root into the next fresh worktree. This is what
  the previously-considered "no-commit stash" alternative was missing: it
  only had a drain, not a seed, so `HEAD` never moved *and* nothing else
  carried turn N's changes into turn N+1's fresh worktree. Adding the seed
  step closes that gap.

### Unchanged from the shipped implementation

- Dependency directories are symlinked into every fresh worktree the same
  way, at the same point (`_link_dependency_dirs`, right after
  `worktree add`).
- CWD stays the source repo path across all turns; the model addresses the
  worktree only through `effective_code_roots` (`<squid_code_roots>`). See
  Reference and ADR-0003.
- The sole-writer guarantee during a turn: only that turn's agent writes to
  its worktree while the turn is in flight, whether sync happens via merge
  or via stash relay.
- `GitDiff` storage, revert (`apply_reverse_patch`), and the "source repo is
  canonical, not the DB" rule are unaffected — they only depend on there
  being a stored diff and a source repo path, not on how sync moves changes
  between them.
- Fallback behavior (below) and the sync/error semantics around it.

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
  merge and nothing to surface — unlike a real conflict under Option 2 or 3,
  which at minimum aborts and reports conflicting files, a fallback-mode
  collision can silently drop one turn's change with no trace. Isolation
  (Option 2 or 3) doesn't just scope diffs; it's what turns this class of
  loss into an ordinary, visible, line-based merge/conflict instead.

## Consequences

- Good: turns are isolated in their own worktree; diffs are turn-scoped.
- Good: multiple sessions/parallel adhoc turns on the same code root are
  isolated from each other.
- Good (target design): no commit-trail noise on the source branch, and no
  squash-on-push subsystem is needed to clean one up.
- Good (target design): repo_root ends each turn in the same kind of state
  a human editing it directly would leave it in — dirty, uncommitted,
  ready for the user's own `git commit` whenever they choose.
- Bad (target design): repo_root is dirty more of the time than under the
  shipped commit+merge design, which gave each turn a clean commit boundary
  automatically. There is no longer an automatic point-in-time marker for
  "this is what turn N produced" beyond the stored `GitDiff` and the
  worktree itself (until it's swept).
- Bad (both designs): dependency directories are symlinks — deleting one
  from inside a worktree deletes the real directory in repo_root.
- Bad (both designs): if Squid crashes mid-turn, stale worktree directories
  and branches can accumulate until the sweep or a startup pass prunes them.
- Bad (both designs): non-git directories and repos without an initial
  commit fall back to direct working-tree mode; bleed is possible until
  fixed.
- Bad (both designs): in fallback mode, the diff is not turn-scoped and the
  user must be told.
- Bad (target design): conflicts at drain time require user intervention
  before the worktree is removed, same as merge conflicts do today.

---

## Reference: current implementation (commit + merge --no-ff)

This is what's actually shipped, gated off by default (see Status). It
predates the stash-relay design above and remains in place until that
design is built. Kept here as the accurate record of current behavior, not
as the recommended direction for new work.

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
