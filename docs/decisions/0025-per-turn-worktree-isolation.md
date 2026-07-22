---
status: accepted (branchless isolation implemented; prior worktree/branch mechanism superseded)
date: 2026-07-09
updated: 2026-07-22
---
# ADR-0025: Per-Turn Git Worktree Isolation for Agent Changes

## Status

**Current design.** The worktree/branch isolation mechanism originally
described by this ADR
(hidden base commit + `sqd-*` branch + `git worktree add`) shipped and worked
for isolation and diffing, but on 2026-07-22 it leaked: an agent (opencode
running a free-tier model) ran `git push` from inside its per-turn worktree
and pushed the internal `sqd-hive-6746-2a8899` branch — including the hidden
`squid: hidden base for hive/6746` commit — to the real GitHub remote,
unprotected. A follow-up turn on the same backend, after being told not to
push from the worktree, instead created a *new*, normally-named branch and
attempted a pull request — showing that neither prompt instructions nor a
naming-pattern-based guard (e.g. a pre-push hook rejecting `refs/heads/sqd-*`)
close this off, since the model can simply not use the blocked name. See
"Revised design" for the shipped replacement: per-turn isolation without ever
creating a branch, ref, or `.git` the agent can push from at all.

The rest of this document (Context, Considered Options, the original
Decision, and the "Reference: current implementation details" section) is retained
as the historical record of the worktree/branch approach and why it was
chosen over Options 1–3.

`WORKTREE_ISOLATION_ENABLED` (`agent/config.py`, driven by `worktree.enabled`
in `squid.yaml`, default `true`) gates the whole feature. With it off, every
turn runs in **Fallback** (below): agents write directly to the real working
tree, diffs are unscoped, and the model is not told it's a sole writer.

This ADR previously proposed "commit each turn on a per-turn branch, then
`merge --no-ff` into the source repo" as the sync mechanism, and left it
disabled because the resulting per-turn commit trail (`"squid: turn N"`)
had no story for turning back into normal-looking history short of a whole
new squash-on-push subsystem. A later draft tried to avoid that trail with
raw `git stash create` / `stash apply`, but that is not a reliable merge
primitive for dirty worktrees. The shipped design is now **branchless turn
directories + hidden snapshot commits + off-repo merge + patch promotion**:
Squid may create internal commit objects/refs for preprocessing and merge
mechanics, but it never advances the user's branch or creates user-visible
per-turn commits. Remaining work is rich conflict-resolution UX around the
integration worktree; unresolved conflict or promotion-failure rows block new
turns against the affected repo instead of silently starting a fresh isolated
turn.

### Configuring the shipped implementation

Shipped enabled in `config/squid.yaml.example`. To opt out, set
`enabled: false` in `~/.squid/squid.yaml`:

```yaml
worktree:
  enabled: false
  auto_link_ignored_dirs: true
  # Optional fallback for dependency/cache directory names Git does not report
  # as ignored. Leave unset; add only dependency/cache names Squid misses.
  # dependency_dirs:
  #   - custom-cache-dir
  # Whether a new turn should start from source checkout dirty/untracked files.
  # false seeds from HEAD only; promotion still snapshots the source checkout
  # at turn end so dirty source files are not blindly overwritten.
  track_dirty_changes: false
```

`auto_link_ignored_dirs` makes `ensure_worktree` discover ignored directories
from Git, then symlink only those whose basename is in the dependency/cache
allowlist (`dependency_dirs`, or Squid's built-in default list). This keeps
installed dependencies and local tool caches available without making every
ignored output directory shared mutable state. `dependency_dirs` remains the
extension point for project-specific dependency/cache names, including
unignored ones in tests or nonstandard repos. Existing user configs may still
contain an older expanded `dependency_dirs` list; that list is not needed in
YAML unless the user is adding project-specific names.

## Revised design: branchless per-turn checkout + patch promotion

Replaces the worktree/branch mechanism (`git worktree add -b sqd-*`) with a
per-turn directory that is never a git repo in its own right, so there is
nothing in it for an agent to `git branch`/`push`/`gh pr create` against,
regardless of what name it picks. This isn't a narrower guard than the
pre-push hook idea — it's not naming-pattern-based at all, since the failure
mode observed was a model routing around a naming-pattern guard by inventing
its own branch name.

### Why git-based diffing stays, why locking stays coarse

- **Diffing must stay git-based, not agent-reported.** Codex does not
  reliably emit a changed-file list with edited regions, so per-tool-event
  diff attribution (what MultiEdit/Write tool events already carry for
  Claude) can't be the universal source of truth. A plain filesystem/tree
  diff of the turn's directory, backend-agnostic, remains required.
- **Bleed prevention requires isolating the whole diffable window, not
  fine-grained locking.** A directory-level diff is contaminated by *any*
  other write landing in it between the pre- and post-turn snapshot, even to
  a file no lock would have covered. Per-file or per-tool-call locking (via a
  hypothetical tool-call hook, or a wrapped binary) only helps with
  corruption, not bleed, and — since real coding turns interleave edits with
  reads/thinking throughout, not just at the end — any scheme that toggles a
  lock on and off around individual writes reopens the same bleed gap between
  bursts *within one turn*. The fix is a private directory for the entire
  turn, not a smarter lock schedule.
- **The lock must not span the whole turn.** Squid can only intercept
  subprocess start/end, not individual tool calls inside a running backend
  CLI, so a lock that has to cover "no other writer during this window" would
  otherwise have to be held for the turn's full duration (seconds to many
  minutes) — serializing all same-repo_root turns, defeating ADR-0010's
  adhoc parallelism for no correctness reason. Materializing an isolated copy
  removes the need for any lock during the turn at all; the only lock left is
  the brief promotion-time critical section, same duration as today's
  `_lock_for`.

### Mechanics

```
turn starts
  → choose BASE_COMMIT:
    - with `worktree.track_dirty_changes: false`, BASE_COMMIT is HEAD.
    - with `worktree.track_dirty_changes: true`, Squid snapshots repo_root's
      current dirty/untracked tree and creates a hidden BASE_COMMIT parented
      to HEAD.
  → materialize BASE_COMMIT into a fresh directory with NO .git present via
    `git archive <base_commit> | tar -x`, not `git worktree add`.
    Dependency/cache dirs are symlinked in exactly as today
    (`_link_dependency_dirs`).
  → agent runs against that directory for however long it takes; no lock is
    held, no other turn is blocked, regardless of how many small edits it
    makes or how they're spaced out.

turn ends
  → snapshot the turn directory into a tree object and diff it against the
    seed tree; this is the turn's GitDiff, computed the same way regardless
    of backend. The post-turn tree cached for GitDiff is passed into
    `sync_after_turn` so promotion does not rescan the turn directory just to
    discover a no-op.
  → if TURN_TREE equals BASE_TREE, promotion returns without snapshotting
    repo_root or creating an integration worktree.
  → otherwise acquire the brief per-repo_root lock, snapshot repo_root's
    current dirty/untracked content as CURRENT_TREE, merge TURN_TREE with
    CURRENT_TREE in a disposable Squid-internal integration worktree, and
    apply `CURRENT_TREE..MERGED_TREE` back to repo_root with drift/rollback
    checks.
  → repo_root receives ordinary working-tree edits. Squid does not create a
    user-visible per-turn commit and does not install a squash-on-push hook.
  → release the lock.
```

No branch, ref, or `.git` is ever created in the agent-visible turn
directory, so there is no separate namespace for a push/PR to leak into. The
only real `git worktree` used by the shipped implementation is the disposable
integration worktree created after the turn, under Squid control, for merge
and conflict handling.

### Resolved: dirty-tree tracking is opt-in, off by default

Capturing repo_root's live dirty state (`SEED_TREE`/`CURRENT_TREE` via the
scratch-index overlay in "Capturing content trees") is not free on repos with
large tracked-file counts: it requires `stat`-ing every tracked file to
determine what changed, unconditionally, whether or not anything is actually
dirty — see "Capturing content trees" cost analysis. Doing this up to 3x per
turn (seed, turn-end, promotion-time drift recheck) is a standing per-turn
cost proportional to tracked file count, not repo byte size, and not
something Squid can safely assume is cheap without knowing the target repo's
scale in advance.

Decision: gate it behind a new config flag,
`worktree.track_dirty_changes`, **default `false`**.

- **`false` (default):** turns seed from `HEAD` only. Uncommitted changes in
  repo_root — from a user's IDE, or anything else outside Squid — are
  invisible to the next turn until committed. This is the cheaper turn-start
  path: no scratch-index overlay is needed to seed the isolated directory.
  Promotion still snapshots repo_root at turn end and still uses the
  CURRENT/TURN hidden-commit merge path, so disabling dirty tracking does not
  let Squid blindly overwrite dirty source files. Docs/UI must tell users
  plainly that edits made outside Squid need to be committed to become visible
  to a turn — this is a real behavior change from "the agent sees your unsaved
  edits," not just an internal simplification.
- **`true`:** restores today's documented behavior — dirty-tree capture,
  synthetic BASE/CURRENT/TURN commits, and the real 3-way merge, so
  non-overlapping concurrent edits (IDE vs. agent) auto-merge instead of
  requiring a commit first. The config comment should say plainly that this
  can be expensive on large-file-count repos and that enabling Git's
  `core.fsmonitor` is the recommended mitigation, since fsmonitor is what
  turns the unconditional stat sweep into a cheap "what changed since last
  check" lookup.

This is additive to `worktree.enabled`: dirty tracking only has meaning when
worktree isolation itself is on.

#### Trade-offs behind defaulting `false`

- **The cost is unconditional, not just a "dirty" tax.** `stat()`-ing every
  tracked file is how Git *finds out* whether anything changed — there's no
  cheaper pre-check, since the index only caches what was true as of the last
  refresh and nothing updates it automatically when a file changes on disk.
  So a perfectly clean repo pays the same sweep cost as a dirty one; only the
  content-hashing step (cheap, proportional to what changed) is skipped.
- **The seed cost is paid every isolated turn, not just code-editing turns**,
  because Squid must create the turn directory before it knows whether the
  backend will edit files. Promotion-time repo_root snapshotting is skipped
  when the cached GitDiff tree proves the turn made no file changes.
- **Reverting to real `git worktree add` instead of a bare directory does
  not reduce this cost.** The expensive step is capturing *repo_root's* dirty
  state, which is identical either way; only the turn side's materialization
  mechanism changes. Real worktrees also reopen the exact `git push`/`branch`
  leak this ADR's redesign exists to close (naming-pattern pre-push guards
  were already shown insufficient — a model just picks an unblocked name), so
  it's a worse trade on both axes, not a cost fix.
- **An agent's own `git status`/`git diff` habits don't substitute for this
  either.** Under the branchless design the turn directory has no `.git`, so
  those calls are simply inert there. In fallback mode (isolation off),
  where code roots do resolve to a real repo, an agent's own git commands can
  incur a similar cost independently — which is a reason the *aggregate*
  system cost is muddier than "Squid adds this from zero," but not a reason
  to make it Squid's default.
- **A cheap, unrelated check:** counting tracked files (`git ls-files | wc
  -l` or reading the index/HEAD tree directly) is not the same operation —
  it's a single sequential read of an already-existing structure, no
  filesystem `stat()` calls at all, and stays fast (low tens of ms) even at
  million-file scale. It's suitable for something like a startup-time repo
  size check, but counting files doesn't tell you dirtiness — that's the
  part that's actually expensive.

### Resolved: no auto-commit or squash-on-push in the shipped implementation

- **Per-turn auto-commit onto the real branch is not part of the shipped
  design.** Squid promotes the merged tree back to repo_root as ordinary
  working-tree edits. The user's branch does not advance until the user or an
  explicit future Squid action commits in repo_root.
- **Squash-on-push is not installed.** It was considered as a way to make
  automatic per-turn commits look like normal history before push, but the
  shipped design avoids creating those user-visible commits in the first
  place. There is therefore no Squid-owned `pre-push` hook in the current
  implementation.
- Per-backend tool-call hook support (for a future finer-grained corruption
  guard, separate from bleed prevention) hasn't been surveyed beyond
  confirming none exists in this codebase today.

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
   isolation off, two parallel prompts (e.g. two adhoc
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
   merge that commit into the source repo. This was an earlier implementation,
   but left noisy per-turn commits on the user's branch and exposed an
   agent-visible Git repository that could be pushed.
3. **Per-turn isolation via Git worktrees, synced by stash relay.** Create a
   worktree per turn; instead of committing, seed it from the source repo's
   current dirty tree and, at turn end, drain its changes back into the
   source repo's dirty tree — via `git stash create`/`apply`, never `commit`
   or `merge`. Rejected; see Rejected alternatives.
4. **Per-turn isolation via hidden snapshot commits and patch promotion.**
   Capture repo_root's dirty tree into internal tree/commit objects when
   configured, run the agent in a plain branchless directory seeded from
   HEAD or that hidden snapshot, merge concurrent updates in a disposable
   integration worktree, then promote the resolved delta back to repo_root as
   ordinary unstaged local file changes.

## Decision

**Option 4 is the shipped design.** It keeps everything Option 2 got right —
isolation during the turn, a turn-scoped diff, and parallel-safe merge
semantics — while avoiding both bad outcomes: no permanent per-turn commit
trail on the user's branch, and no fragile dirty-worktree stash apply. Squid is
allowed to create internal commits/refs because they are preprocessing state,
not product history: they are not merged into the user's branch, not shown as
"turn commits", and can be deleted after sync/conflict resolution. The source
repo's working tree remains the user's ordinary dirty tree. After promotion,
`git status` and `git commit` behave exactly as if the edits had been made
locally in repo_root.

This supersedes the "Planned follow-up:
squash-on-push" and "Alternative considered: stash-based no-commit merge"
sections of the prior version of this ADR; both are resolved by the hidden
snapshot/patch-promotion design.

### Shipped design: hidden snapshots, off-repo merge, patch promotion

The implementation should treat Git commits as an internal merge data
structure, not as user history. Squid may create temporary commits and refs
under its own namespace, but those objects never advance the user's branch and
never become the commit history the user sees.

```
turn starts
  → with `track_dirty_changes: false`, use HEAD as BASE_COMMIT
  → with `track_dirty_changes: true`, capture repo_root's dirty working-tree
    content as SEED_TREE and create hidden BASE_COMMIT from SEED_TREE
  → create a plain branchless turn directory from BASE_COMMIT
  → run the agent against that directory

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

Per-turn directory paths keep the current shape:

```
turn worktree path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
base ref:            refs/squid/worktrees/<slug>-<md5>/base
```

The key remains `(topic, str(asst_msg_id))`; see Reference for the current
derivation. The current implementation stores only the durable registry state
needed across process boundaries:

- `base_commit`: repo_root's content when the turn started, recorded under
  `refs/squid/worktrees/<turn-key>/base`.
- `worktree_path`: the isolated branchless turn directory.
- `integration_worktree_path`: the disposable worktree path used for merge
  conflicts.
- `status`: `pending`, `synced`, `conflict`, or `promotion_failed`.

The merge algorithm also creates short-lived tree/commit objects:

- `seed_tree` / `base_commit`: repo_root's content when the turn started, or
  HEAD when dirty tracking is disabled.
- `turn_tree` / `turn_commit`: the agent worktree content when the turn ended.
- `current_tree` / `current_commit`: repo_root's content at the last merge
  attempt.
- `merged_tree` / `merge_commit`: the clean integration result, when one
  exists.

Only `base_commit` is kept under a Squid-owned ref while active. Current and
turn commits are internal merge inputs and become unreachable after sync or
cleanup; ordinary Git GC can reclaim them later.

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
- **Drifted:** if repo_root no longer matches `CURRENT_TREE`, promotion aborts
  instead of applying a patch against the wrong filesystem state. The caller
  marks the row `promotion_failed`, keeps the worktree artifacts, and leaves
  repo_root untouched for manual follow-up.

Fatal Git errors are distinct from merge conflicts:

- Failure to create the integration worktree is fatal for that sync attempt.
- A merge failure with unmerged paths is a conflict; a merge failure without
  conflict markers is fatal and must not be treated as "nothing to merge."
- `git apply --check` failure is fatal before any write.
- `git apply` failure after a clean check is treated as a partial-write hazard:
  Squid captures the post-failure tree, applies a reverse patch back to
  `CURRENT_TREE`, then raises. If rollback itself fails, the error includes
  that rollback failure and the worktree row remains for manual inspection.

### Diff-viewer implications

The diff shown to the user remains Squid's own output, not the model's:
`GitChangeTracker.build_event()` computes it from tree snapshots. Under the
shipped design, the diff is `SEED_TREE..TURN_TREE` and is durable as soon as
the turn ends, regardless of whether promotion has completed.

The gap is repo_root visibility. Until promotion succeeds, repo_root does not
contain the turn output, so existing actions that resolve to repo_root can look
wrong:

- `revert_diff` reverse-applies stored diff text against repo_root. It should
  be unavailable while the diff is not promoted.
- "Open file" against repo_root can show pre-turn content while a conflict is
  pending.

Fix: store sync state with the `GitDiff` event or use the associated worktree
registry row's `status`. `get_diff_revert_eligibility` gains a `pending`
state, checked before `revertable` / `conflicting` / `reverted`, and the UI
hides stale repo_root file links while `status != 'synced'`.

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

- If `(topic, repo_root)` has an open `status = 'conflict'` row, route the
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

Both paths end with `status = 'synced'` on success or
`status = 'conflict'` with a refreshed conflict artifact if repo_root
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

- **Commit + merge into the source branch.** This was the earlier
  worktree/branch implementation. It gives good merge semantics, but creates
  a permanent per-turn commit trail on the user's branch and exposes a Git
  repo to the agent.
- **Raw stash relay.** `git stash create` is useful as a read-only snapshot of
  tracked dirty state, but it is not the target merge primitive:
  `stash create` does not capture untracked files, `stash apply` onto a dirty
  worktree can abort before producing conflict markers, and Git has no
  `stash create relative to arbitrary BASE` mode for promotion.
- **Apply into repo_root, then restore on conflict.** This writes failed merge
  attempts into the user's real directory and then tries to undo them without a
  single Git abort primitive. The shipped design avoids that by keeping risky
  merges inside the integration worktree.

### What changed vs. the old commit+merge implementation

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

Whenever `WORKTREE_ISOLATION_ENABLED` is `false`, or
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
- Good (shipped design): no commit-trail noise on the source branch, and no
  squash-on-push subsystem is needed to clean one up.
- Good (shipped design): internal commits/refs are allowed for preprocessing,
  but they are not user history and never advance the user's branch.
- Good (shipped design): repo_root ends each turn in the same kind of state
  a human editing it directly would leave it in — dirty, uncommitted,
  ready for the user's own `git commit` whenever they choose.
- Good (shipped design): fatal Git failures are not silently downgraded to
  clean sync. They either leave repo_root untouched, or roll back a partial
  patch apply before surfacing `promotion_failed`.
- Good (shipped design): conflicts are never written into repo_root — the
  merge happens entirely inside the integration worktree, so a conflict
  leaves the user's real directory untouched rather than mid-merge.
- Good (shipped design): conflict resolution has a structured base/ours/
  theirs form per file, so it can be handed to the user *or* to an LLM in a
  follow-up turn — resolution isn't limited to a human reading raw
  conflict markers.
- Good (shipped design): the diff shown to the user is computed by Squid
  from git tree snapshots at turn end regardless of conflict/promotion
  status — it's never blocked on, or generated from, anything the model
  outputs (see "Diff-viewer implications").
- Bad (shipped design): revert and "open file" resolve to repo_root, which
  can lag the displayed diff for as long as a conflict takes to resolve —
  a gap that didn't exist in the old synchronous commit+merge design.
  Needs an explicit status (`pending`/`synced`/`conflict`/`promotion_failed`) so the UI
  doesn't offer revert against a file repo_root doesn't have yet.
- Bad (shipped design): repo_root is dirty more of the time than under the
  old commit+merge design, which gave each turn a clean commit boundary
  automatically. There is no longer an automatic point-in-time marker for
  "this is what turn N produced" beyond the stored `GitDiff` and the
  temporary internal snapshot refs/worktrees (until they are swept).
- Bad (shipped design): because promoted changes are ordinary working-tree
  edits, the user's branch accumulates changes without user-visible commit
  logs. Squid's audit trail lives in stored turn metadata/diffs, not Git
  history, unless a future explicit commit action is added.
- Bad (shipped design): a user-visible commit cannot be a raw `git commit`
  performed inside the isolated worktree. Squid must promote the file state to
  repo_root first, then commit in repo_root if it supports an explicit commit
  action.
- Bad (all worktree designs): dependency directories are symlinks — deleting one
  from inside a worktree deletes the real directory in repo_root.
- Bad (all worktree designs): if Squid crashes mid-turn, stale turn
  directories, hidden base refs, and integration worktrees can accumulate
  until the sweep or a startup pass prunes them.
- Bad (all worktree designs): non-git directories and repos without an initial
  commit fall back to direct working-tree mode; bleed is possible until
  fixed.
- Bad (all worktree designs): in fallback mode, the diff is not turn-scoped
  and the user must be told.
- Bad (shipped design): conflicts still require resolution (by the user or an
  LLM turn) before the worktree is promoted and removed — this design
  changes where a conflict is safe to sit, not whether one can happen.
- Bad (shipped design): promotion re-checks repo_root for drift since
  `CURRENT_TREE` was captured. On drift, the current implementation stops and
  marks `promotion_failed` rather than retrying against the newer tree.
- Bad (shipped design): a resolution turn must not get the normal fresh
  per-turn worktree — `_setup_worktrees` needs a new special case that
  detects an open `status = 'conflict'` row for `(topic, repo_root)`
  and routes the turn to its `integration_worktree_path` instead of minting a
  new worktree for the new message id. Without it, "ask AI to resolve" would
  silently redo the original turn's work in a brand-new worktree that never
  saw the conflict at all (see "Conflict resolution across turns").
- Bad (shipped design): the conflict list needs its own stored event and
  prompt-summary function (`MergeConflict` / `_conflict_context_summary`,
  mirroring `GitDiff` / `_gitdiff_context_summary`) before a follow-up LLM
  turn can be handed it as context — not implemented today, since neither
  the event type nor the integration-worktree routing path above currently
  exist.

---

## Reference: current implementation details

*(This section tracks the branchless shipped implementation and the remaining
details around dependency-dir symlinking, drift detection, and conflict
handling.)*

### Naming and paths

```
turn dir path:  ~/.squid/worktrees/<repo_hash>/sqd-<slug>-<md5>/
dir name:       sqd-<slug>-<md5>
base ref:       refs/squid/worktrees/<slug>-<md5>/base
```

The key passed to the naming functions is `str(asst_msg_id)` — unique per
turn. It's stored in the `worktrees.agent` column because that table
predates per-turn keys; for worktree rows the column holds a worktree key,
not a configured agent name. `<slug>`/`<md5>` derive from `(topic, key)`;
`<repo_hash>` is 8 hex chars of `MD5(repo_root)`. Turn directories live
outside the project directory so they never appear in the user's repo; the
`sqd-` prefix identifies Squid-managed turn directories. `ensure_worktree`
records a base commit under the base ref and materializes that commit into a
plain turn directory. With `track_dirty_changes: false`, the base commit is
HEAD. With `track_dirty_changes: true`, Squid first snapshots repo_root's
dirty working tree into a hidden base commit.

Using the assistant message ID as the key means every turn — adhoc or
regular — gets a fresh, isolated worktree with no state carried over. The
assistant message row is inserted before worktree setup so the ID is
available as the key.

### How the worktree becomes the agent's working directory

`_setup_worktrees` calls `ensure_worktree(repo_root, topic, str(asst_msg_id))`
for each git repo under `code_roots`, which records the configured base commit
and extracts it into the branchless turn directory.
`effective_code_roots` is remapped to the worktree paths; **CWD is not
remapped** (`proc_cwd` stays the source repo path across all turns —
ADR-0003). The model reaches the worktree only through the absolute paths in
`<squid_code_roots>`.
`topic_queue._process` routes `proc_cwd` (source repo path) to the
subprocess `cwd` and `display_cwd` to stats/session-tracking/SSE.

**Sole-writer guarantee:** each turn has its own directory for the paths
surfaced in `<squid_code_roots>`, so the agent is the only writer to those
effective code roots during that turn. Process CWD still follows ADR-0003 and
is not remapped; relative writes against CWD can bypass isolation until CWD
enforcement is changed.

### Dependency directories

`ensure_worktree` calls `_link_dependency_dirs(repo_root, wt)` right after
materializing the branchless turn directory. It symlinks allowlisted ignored directories
discovered by Git when `auto_link_ignored_dirs` is enabled, plus optional
`worktree.dependency_dirs` entries, into the equivalent path under `wt`.
It only ever matches directories, never individual files, so it can't touch
gitignored *state* files (`.env`, `squid.db`, `*.log`, `config/squid.yaml`)
that must stay private to repo_root.

After creating symlinks, Squid writes slashless versions of the linked paths to
Git's local exclude file (`git rev-parse --git-path info/exclude`). This is
intentional even when the source repo has patterns like `.venv/`: a trailing
slash gitignore pattern matches a directory, but the worktree entry is a
symlink, so plain `git status` would otherwise report it as untracked.

Ignored directories outside the dependency/cache allowlist, such as build
outputs, are intentionally not linked. If a turn creates or edits those paths
inside its isolated worktree, they remain local to that worktree and do not
silently mutate repo_root outside the GitDiff/revert flow.

**Safety guard:** `_link_dependency_dirs` refuses to run if `wt` resolves
to the same path as `repo_root`, which would otherwise symlink a dependency
directory onto itself.

Snapshot creation filters external directory symlinks back out so stale or
ignored dependency links do not get promoted to repo_root as `120000` symlink
blobs. The local exclude entry keeps status clean; the snapshot filter is the
promotion safety net.

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
     a. capture TURN_TREE from the turn worktree and CURRENT_TREE from
        repo_root
     b. create hidden current/turn commits parented to the stored BASE_COMMIT
     c. merge TURN_COMMIT into CURRENT_COMMIT in a disposable integration
        worktree outside repo_root
     d. on a clean merge, apply `CURRENT_TREE..MERGED_TREE` to repo_root with
        `git apply --check` followed by `git apply`
     e. if apply fails after writing, roll back the partial patch before
        returning failure
  3. on success, mark_worktree_synced: DB row status → 'synced',
     last_used_at bumped. On conflict or failed promotion, mark the row
     `conflict` or `promotion_failed` and keep artifacts for inspection.

turn N+1 dispatch  [topic_queue.py: TopicDispatcher.dispatch]
  → fires worktree.cleanup_worktrees(topic) as a background task
  → sweep removes turn N's worktree once inactive and past grace period
```

### Sync now, remove later

Removing a worktree synchronously right after the CLI process exits is
unsafe — a backgrounded process (e.g. a `bash` tool call backgrounding
`pytest`) could still have it as `cwd`. So sync (merge into repo_root) and
remove (delete worktree dir, hidden base ref, DB row) are split:

- **Sync — synchronous, at turn end.** `sync_after_turn` runs for every
  registry row keyed by `str(asst_msg_id)`. On success, `mark_worktree_synced`
  sets `status='synced'` and bumps `last_used_at`, but doesn't delete the
  row or worktree — repo_root already has the turn's changes as ordinary
  working-tree edits; only the now-inert worktree copy remains.
- **Remove — asynchronous, best-effort sweep.** `cleanup_worktrees(topic)`
  is the single removal path, triggered by `TopicDispatcher._sweep_worktrees`
  on every new turn dispatch (background task, exceptions logged and
  swallowed) and by `server.py` directly on session close/clear. It skips
  rows whose key is an active turn (`get_active_msg_ids()`) or younger than
  `_CLEANUP_GRACE_SECONDS` (30s), and skips `conflict` / `promotion_failed`
  rows; otherwise re-runs `sync_after_turn` (a no-op if already synced,
  catching crash-orphaned rows) and, if clean, `remove_worktree` +
  `delete_worktree`.

### Merge conflicts

Git auto-merges changes to different hunks in the same file; a conflict
only occurs when two sessions modify the same lines. When the integration
merge fails, repo_root is left untouched, the integration worktree is kept
with conflict markers, the conflicting file list is returned for the UI, and
the original turn worktree plus integration worktree stay until the conflict
is resolved.

### Source of truth

The Git repository and active worktree are the source of truth for file
contents; SQLite stores only the temporary worktree registry and run
events/diffs, not a reconstructable copy of the tree. Once a turn syncs,
repo_root has the changes as ordinary working-tree edits and the DB row is
marked `synced` until cleanup deletes it.
Stored `GitDiff` payloads include a `source` field for the canonical repo
path alongside optional `worktree_repo`/`worktree_cwd` diagnostic fields;
the UI's `_gitDiffSourceRepo()` iterates `[source, repo, cwd]` and returns
the first non-worktree path so file-open buttons always point at repo_root.
