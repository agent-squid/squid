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

### Re-enabling

Not shipped in `config/squid.yaml.example` — keeping an experimental,
off-by-default block out of the example file that most users copy from. To
opt in, add this to `~/.squid/squid.yaml` yourself:

```yaml
worktree:
  # Isolate each turn's agent in its own Git worktree. Off by default. When
  # off, agents write directly to the code root's real working tree and
  # diffs are unscoped.
  enabled: true

  # Directories to symlink (not copy) from a code root into each fresh per-turn
  # worktree, so installed dependencies don't need reinstalling there. Matched
  # by directory name; not recursed into once matched; never applied inside .git.
  # If you add an entry, make sure its .gitignore pattern has no trailing
  # slash (e.g. "vendor" not "vendor/") — a trailing slash won't match the
  # symlink, so it'll get tracked into the repo by the turn-end auto-commit.
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

`dependency_dirs` only has any effect when `enabled: true` — both keys live
under `agent/config.py`'s `_worktree_cfg`, which defaults to `{}` and falls
back to the list above when the key is omitted, so leaving the whole
`worktree:` block out of `squid.yaml` (the common case) is equivalent to
pasting this in with `enabled: false`.

## Planned follow-up: squash-on-push

Per-turn auto-commits (`"squid: turn N"`, or the request/response-derived
message from `_build_commit_message`) are useful as an audit trail but not as
permanent history — nobody wants one commit per turn in `git log`. Before
re-enabling isolation by default, the plan is:

Note this is not a silent fix layered invisibly under the existing sync
path — it's a deliberate procedural addition. Per-turn auto-commit keeps
happening on every turn exactly as it does today; squashing that history
down only happens when something explicitly calls the new `push` command
(point 2 below). Nothing about turn-end sync changes to make the commit
trail disappear on its own; a user (or some other explicit trigger) has to
invoke the squash. This is also why it can't be done from inside the chat
agent itself — the agent is sandboxed to its per-turn worktree and has no
access to `repo_root`, so the squash has to live in its own privileged
command path outside the normal turn loop.

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

### Alternative considered: stash-based no-commit merge

Instead of auto-committing on `sqd-<key>` and `merge --no-ff`, sync could
bundle the worktree's changes into a stash and pop it onto the source repo,
producing zero commits instead of one per turn:

```bash
# in the worktree, at turn end
git stash push -u -m "turn <asst_msg_id>"

# in the source repo
git stash pop stash@{0}
```

`-u` is required so untracked files the agent created are included. This
sidesteps the squash-on-push problem entirely — there's no `"squid: turn N"`
commit trail to squash because nothing was ever committed.

The stack-index race (`stash@{0}` shifting if another topic pushes onto the
same source repo's stash concurrently) is avoidable: capture
`git rev-parse refs/stash` immediately after the `push`, store that SHA in
the worktree registry row already keyed by `str(asst_msg_id)`, and use the
hash — not `stash@{N}` — at sync time. Tested directly (`git` 2.x):

- `git stash apply <sha>` **works** — apply resolves its `<stash>` argument
  as an arbitrary commit-ish, not just a `stash@{N}` ref, so it's immune to
  the stack shifting from other topics' concurrent pushes/pops.
- `git stash pop <sha>` and `git stash drop <sha>` **fail** —
  `error: '<sha>' is not a stash reference`. Both require a live
  `stash@{N}` ref; the underlying commit existing (even reachable from
  `refs/stash`'s reflog) isn't enough.

So the safe sequence is apply-by-hash, then drop-by-rescanned-index: after
applying, scan `git stash list` and `git rev-parse stash@{i}` for each `i`
until one matches the stored SHA, then `git stash drop stash@{i}`. This
still can't mis-drop another topic's entry, because the match is by exact
hash, not position — worst case if the entry is already gone (raced away by
something else) the drop is just skipped, leaking a stash entry rather than
corrupting one. `-m "turn <asst_msg_id>"` on push remains useful as a
human-readable label in `git stash list`, but the actual lookup key Squid
would store and act on is the hash, not the message text.

What this doesn't fix, and why it's not adopted despite the race being
solvable:

- **Breaks turn-chaining.** `ensure_worktree` creates each turn's worktree
  with `git worktree add -b sqd-<key> <path> HEAD` (see "How the worktree
  becomes the agent's working directory"), which only materializes
  *committed* tree state. Today, `sync_after_turn`'s commit + `merge --no-ff`
  is what advances the source branch's HEAD every turn, so turn N+1's fresh
  worktree correctly includes turn N's changes. `stash apply` alone never
  commits, so HEAD never moves — turn N+1's worktree would branch from stale
  history, silently missing turn N's changes entirely rather than merely
  showing them as unscoped. That's worse than the bleed problem this ADR
  exists to prevent, not a variant of it.
- **Reintroduces bleed at the source repo.** The applied-but-uncommitted
  changes land directly in the source repo's own working tree — exactly
  "Option 1: No isolation," at the layer worktrees were built to keep clean.
  If another topic or session shares that code root, it now collides with
  turn N's uncommitted output sitting in the shared tree.
- **No net win even if you patch around both.** Avoiding these means
  committing immediately after the apply anyway — at which point stash has
  bought nothing over today's `merge --no-ff` (same one-commit-per-turn
  outcome), while adding SHA-capture, index-rescanning, and drop-by-hash
  bookkeeping for it.
- Separately, same-line conflicts still halt with markers (no data loss),
  but changes to *different* lines of the same file auto-merge silently with
  no merge commit produced, so there's no artifact to inspect afterward —
  this is the same underlying 3-way-merge behavior `git merge` already has
  for non-overlapping hunks, the difference is only that a merge commit
  leaves a reviewable record and a stash apply leaves none. (Revert itself
  doesn't depend on this trail — `/chat/{msg_id}/revert`
  (`agent/server.py`) reverts by applying the stored `GitDiff` text as a
  reverse patch (`apply_reverse_patch`), not by referencing a commit.)

The HEAD/worktree-chaining dependency, not the stack-index race or revert,
is the actual reason auto-commit + `merge --no-ff` is still the adopted
mechanism (see "Sync now, remove later").

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

### Considered: seeding a fresh worktree with the source repo's dirty state

Today `git worktree add -b sqd-<key> <path> HEAD` only ever materializes
*committed* state — if the user has uncommitted edits sitting in the source
repo when a turn starts, the agent never sees them. Whether that's desired
is a product question, not addressed here; this section only covers whether
it's mechanically possible and what it costs. Verified directly (`git`
2.50.1):

**Mechanism.** `git stash create` builds a commit object representing the
current index + working tree diff against HEAD **without touching the
source repo's index, working tree, or `refs/stash`** — unlike `stash push`,
which does mutate the source repo and would need a guaranteed `pop` to
restore it. That snapshot commit can then be applied inside the *new*
worktree instead:

```bash
# unchanged: worktree still built from HEAD
git worktree add -b sqd-<key> <path> HEAD

# read-only snapshot of the source repo's dirty tracked state
SHA=$(git -C <repo_root> stash create -m "turn <asst_msg_id> seed")

# apply it into the worktree, not the source repo
git -C <path> stash apply --index "$SHA"
```

**Footgun: `-u`/`--include-untracked` is not a valid flag for `stash
create`** (only for `stash push`) — git does not error on it, it silently
folds `-u`/`-m` into the stash's commit message text as literal characters.
Confirmed via `git cat-file -p`: a snapshot created with `stash create -u -m
"snapshot"` came out with only 2 parents (no untracked-tree parent) and a
message of literally `"On main: -u -m snapshot"`. So `stash create` alone
**never captures untracked files**, regardless of flags passed — untracked
files need a second, separate step:

```bash
git -C <repo_root> ls-files --others --exclude-standard -z |
  xargs -0 -I{} cp --parents {} <path>/
```

This is also read-only on the source repo (`ls-files` doesn't touch
anything; `cp` only reads the source path).

**Races, and how each is handled:**

- **HEAD drifts between the `worktree add` and the `stash create`.** If
  another turn's `sync_after_turn` (or a `push`/squash) advances the source
  branch's HEAD in that gap, the worktree was built from the old HEAD but
  the stash's recorded parent is the new one — `stash apply` then does a
  3-way merge against a base the worktree wasn't actually built from.
  Not silent (conflicts still surface exactly as any `git merge` would
  produce them), but nothing flags that the base moved. **Mitigation:**
  resolve HEAD to a concrete SHA once, pass that same SHA to both
  `worktree add <path> <sha>` and compare it against `git rev-parse
  "$SHA^1"` (the stash's first parent) before applying; on mismatch, retry
  the whole capture against the new HEAD rather than applying blind.
- **Torn snapshot across the two-part capture.** Tracked state comes from
  `stash create` (one instant); untracked files come from a separate
  `ls-files` + copy (a later instant). If the user's editor or a formatter
  is mid-save across several files exactly then, the combined snapshot can
  mix pre-edit and post-edit state across files. No mitigation beyond
  narrowing the window between the two calls as much as possible — this is
  inherent to capturing dirty state from a repo that is not sole-writer.
- **Read-during-write on individual files.** Same root cause as above, one
  level down: both `stash create`'s hashing and the untracked-file copy are
  plain reads of a live working tree, so either can observe a torn/partial
  write from an in-progress save. The rest of this ADR's design sidesteps
  this entirely via the sole-writer guarantee above — nothing but that
  turn's own agent ever writes to *its* worktree. The source repo has no
  such guarantee; the user's own editor is the whole reason this feature
  would exist, so this hazard can't be engineered away, only accepted.
- **Concurrent captures across topics sharing a code root are *not* a
  problem.** `stash create` touches no shared git state (`.git/index`,
  `refs/stash`) — unlike `push`/`pop`, it can't lock-contend on
  `index.lock`. N topics can each call it concurrently against the same
  source repo with zero interference between them.
- **Snapshot racing an in-flight `sync_after_turn` on the same repo.** The
  ADR already names topics sharing a code root as a bleed vector (see
  "Context" below). If Topic B's turn-start capture runs while Topic A's
  `merge --no-ff` from a previous turn is still applying on that same
  source repo, the repo can transiently be in a conflicted-merge state
  (`MERGE_HEAD` present, unmerged index stages). `stash create`'s behavior
  there isn't something to rely on. **Mitigation:** check for `MERGE_HEAD`
  (or a non-empty `git status --porcelain` unmerged-entry prefix) before
  capturing, and skip/retry rather than proceeding against a repo mid-merge.
- **Untracked file vanishes or moves between listing and copying.** The
  `ls-files --others` walk and the `cp` are a second TOCTOU window —
  editors that save via temp-file-then-rename can make a path transiently
  disappear. **Mitigation:** treat each file's copy failure as a soft
  per-file skip+log, not an abort of the whole capture.

None of these lose data silently — the failure modes are conflict markers
or an outright missing-file error, matching the safety bar the rest of this
ADR holds sync to. What they cost is *consistency*: the source repo's dirty
state has none of the guarantees the worktree's own pre-turn snapshot has,
because the source repo isn't sole-writer. Not implemented; recorded here
in case "should a turn see the user's uncommitted source-repo edits" comes
up as a real feature request.

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
