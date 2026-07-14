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
  → drain: pull repo_root's current dirty state into the worktree and merge
    it there (repo_root itself is not written to yet)
  → on clean merge: promote the worktree's merged tree into repo_root;
    mark_worktree_synced; worktree removal deferred to the same background
    sweep as today ("sync now, remove later")
  → on conflict: repo_root is untouched (nothing was ever written to it);
    the worktree is left intact, not removed, with conflict markers in
    place; the base/ours/theirs list is surfaced to the user or to a
    follow-up LLM turn for resolution, still scoped to the same worktree
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

`SHA` is retained for the rest of the turn (in the worktree registry row,
alongside the path/branch already stored there) as `BASE` — the common
ancestor the drain step's 3-way merge needs later. Without it, drain would
have no way to tell "what repo_root already had when this turn started"
apart from "what the turn itself changed."

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

### Drain, revised: merge in the worktree, promote to repo_root only once clean

An earlier draft of this design applied the worktree's changes *onto*
repo_root and, on conflict, restored repo_root back to a pre-drain
snapshot (kept below as "Superseded" for the record). That works, but it
means every conflict writes a failed 3-way merge attempt directly into the
user's real, live directory before undoing it — and undoing it is manual
reconstruction (index reset, orphan-file cleanup, re-copying untracked
files) rather than one atomic Git command, because `stash apply` has no
`--abort` the way `merge` does.

The fix is to **never let a conflict touch repo_root at all.** Invert the
direction: instead of pushing the worktree's drain onto repo_root, pull
repo_root's current dirty state into the worktree. The worktree is already
disposable and sole-writer; repo_root isn't. Doing the risky merge in the
disposable side and only copying the result over once it's fully resolved
means repo_root is either read-only or written-to-with-a-known-clean-result
— never mid-conflict.

```bash
# brief hold of _lock_for(repo_root) — just to read, not to write:
BASE = <the seed snapshot SHA this worktree started from, captured at turn start>
PRE  = $(git -C <repo_root> stash create -m "pre-drain read")   # read-only
# lock released here — resolution below may take arbitrarily long

git -C <wt> stash apply --index "$PRE"
# 3-way merge: base=BASE, ours=worktree's own tree (this turn's output),
# theirs=PRE (repo_root's current dirty state)
```

**If this applies cleanly:** the worktree's tree now equals "repo_root's
latest dirty state plus this turn's changes," fully merged — and repo_root
itself was never written to during the attempt.

**If it conflicts:** the conflict markers land inside the worktree's own
files — a disposable, isolated location — not in repo_root. repo_root is
simply left exactly as it was; there is nothing to restore, because nothing
there was ever touched. This is what makes the whole "restore to snapshot"
problem disappear rather than solving it: the earlier design needed a safe
undo path *because* it risked mutating repo_root first and asking questions
later. This design never risks it.

### The 3-way conflict list — for the user, or for an LLM's next turn

Once the merge lands in the worktree, git's own index already has
everything needed to describe each conflict as a clean three-way payload —
nothing here is invented, it's plain plumbing on state git already wrote:

For every path in `git -C <wt> diff --name-only --diff-filter=U`:

| side | source | meaning |
|---|---|---|
| base | `git show :1:<path>` | the seed snapshot's version — what neither side had changed yet |
| ours | `git show :2:<path>` | this turn's output (the worktree's own change) |
| theirs | `git show :3:<path>` | repo_root's concurrent version — the user's hand edit, or another topic's already-drained turn |
| marked-up | the file on disk in `<wt>` | git's own conflict markers, already written by `stash apply`; re-running `git -C <wt> checkout --conflict=diff3 -- <path>` upgrades these to include the base line (`\|\|\|\|\|\|\|`) as well, not just ours/theirs |

That `{path, base, ours, theirs, marked-up file}` tuple per conflicting path
is the entire conflict artifact. It can go to either resolver, unchanged:

- **User-resolved.** Surface the conflict list in the UI the same way any
  diff is shown; the marked-up files are editable directly, in place, in
  the worktree (still isolated — nothing else writes there while resolution
  is pending). "Confirm" is just re-checking
  `git -C <wt> status --porcelain` for zero remaining `U`-staged paths — a
  plain git-native check, not new bookkeeping.
- **LLM-resolved.** Feed the identical `{base, ours, theirs}` triples to a
  follow-up turn scoped to the same worktree, with an instruction to
  reconcile the two sides and remove the markers. The sole-writer guarantee
  still holds — it's the same worktree, still exclusive to this turn's
  resolution. Confirmation is the identical `status --porcelain` check.

Either path converges on the same state: once no unmerged entries remain,
the worktree's tree *is* the fully resolved result, ready to promote.

Non-conflicting hunks in the same file still auto-merge silently, same as
`git merge` always does for non-overlapping hunks — that's inherent to
3-way merge, not new here.

### Promoting the resolved worktree into repo_root

repo_root was only ever read (for `PRE`), never written, during resolution
— but resolution has no time bound (a human or an LLM turn may take a
while), so repo_root may have drifted since `PRE` was captured. Re-acquire
`_lock_for(repo_root)` for the promotion and check whether repo_root still
matches `PRE`:

- **Unchanged:** apply the worktree's now-clean tree onto repo_root
  (`stash create` on the worktree relative to `BASE`, `stash apply` into
  repo_root, plus untracked files). This is guaranteed conflict-free — the
  worktree already merged `PRE` into itself before repo_root could move
  again, so there's nothing left to reconcile.
- **Drifted:** the resolution was computed against a `PRE` that's now
  stale. Loop: capture the new `PRE'`, re-apply it into the worktree
  (repeating the exact same 3-way merge from "Drain, revised" — same
  function, same conflict-list mechanism, not a special case) against
  repo_root's current state.

**Yes — a second drift is handled exactly like the first, not silently
absorbed.** This falls out of the merge being content-based rather than
step-counted, and it's worth walking through why it's actually guaranteed,
not just hoped for:

- `BASE` stays anchored at the original seed snapshot (`BASE`/`SEED`) for
  every retry — it never shifts to `PRE` or `PRE'`. That's safe because
  `HEAD` never advances in this design (nothing commits), so every
  `stash create` against repo_root — `PRE`, `PRE'`, `PRE''`, however many —
  is a snapshot of the *same* accumulating diff off the *same* frozen
  `HEAD`. `PRE'` is `PRE` plus whatever changed since, not a disjoint
  snapshot, so diffing everything against the one fixed `BASE` is
  consistent across retries.
- On retry, "ours" is the worktree's *current* tree — which, after a first
  conflict was resolved, already contains that resolution merged in. It is
  not the raw, pre-resolution turn output. So the second merge is
  `(BASE=SEED, ours=turn output + first resolution, theirs=PRE')`, and the
  portion of `PRE'` that's identical to already-merged `PRE` content
  produces no conflict at all — git resolves "both sides independently
  arrived at the same hunk" trivially. Only the genuinely new drift
  (`PRE'` minus `PRE`) has to reconcile against `ours`, which is exactly
  the comparison that should happen.
- If that new drift touches lines the first resolution already decided,
  it's a real second conflict — `ours` and `theirs` disagree and neither
  matches `BASE` — and it's reported through the identical
  `{path, base, ours, theirs, marked-up file}` list, resolved the same way
  (user or a follow-up LLM turn), then re-checked for drift again on the
  next promotion attempt. There's no separate "second-round" code path;
  it's the same function called again, so nothing about the mechanism gets
  weaker or more ad hoc the more times it retries.

**What this doesn't fully solve: unbounded contention.** If repo_root
keeps drifting faster than resolution completes — a pathological case, not
the ordinary "one more edit landed while this was stuck resolving" case —
the promotion loop has no built-in retry cap and could in principle keep
finding fresh drift indefinitely. Worth a practical bound in the actual
implementation (e.g., cap retries and escalate to the user as a stuck-sync
error past N attempts) rather than looping forever; not solved by the
design above, just not silently mishandled by it either — every retry
still goes through the same safe merge-and-report path, it just might do
so more times than is useful.

This keeps the lock's job narrow: held only for the cheap
read-then-compare-then-apply step, never across an open-ended resolution.
Conflict resolution, however long it takes, never blocks another topic from
using the same repo_root in the meantime.

Worktree removal is unaffected: still deferred to the existing background
sweep ("sync now, remove later" in Reference) once promotion succeeds.

### Diff-viewer implications of a deferred promotion

Worth checking directly, since it's easy to assume the diff viewer reads
from repo_root and would therefore go stale during a pending conflict: it
doesn't, and it wouldn't. Two things are true here and they cut in opposite
directions.

**The diff itself is Squid's own output, not the model's, and isn't
affected.** `GitChangeTracker.build_event()` (`agent/git_changes.py`)
builds the `GitDiff` tool event entirely from git plumbing — `base_tree`
is snapshotted at turn start (`_snapshot_tree`, a scratch-index
`read-tree`/`add -A`/`write-tree` against `self.repo_root`, which under
isolation *is* the worktree's own repo root) and `head_tree` the same way
at turn end; `diff`/`stat`/`files` are `git diff` between those two tree
objects. None of it reads repo_root, none of it is LLM-generated text, and
none of it depends on whether drain/promotion has run yet. So the diff
itself — the thing that answers "what did this turn change" — is fully
computed and durable the moment the turn ends, conflict or not. There's no
window where it fails to show up.

**But `revert` and "open file" resolve to repo_root specifically, and
that's exactly what promotion can leave stale during a pending conflict.**
The UI's `_gitDiffSourceRepo()` (`ui/app.js:3484`) picks `source`, falling
back to `repo`/`cwd`, and explicitly rejects anything under
`.squid/worktrees/` (`_isSquidWorktreePath`) — by design, so a displayed
path always survives worktree cleanup. `revert_diff` (`agent/server.py`)
takes that resolved path as `repo_root` and reverse-applies the stored
diff text directly against it. Under the shipped commit+merge
implementation this was never a problem, because sync is synchronous —
by the time a turn's response (and its diff) reaches the user, repo_root
already has the merge, or the merge was aborted and repo_root was
untouched from the start. There was never a gap between "diff is visible"
and "repo_root matches it."

The stash/promote design opens that gap on purpose, for exactly as long as
a conflict takes to resolve: the diff is visible immediately, but repo_root
doesn't have the change until promotion succeeds. In that window:

- **Revert would correctly fail, not corrupt anything** — `apply_reverse_patch`
  runs `git apply --reverse --check` first, and reversing a patch against
  content that was never forward-applied fails that check cleanly. But the
  UI has no reason today to know *why* it failed, since
  `get_diff_revert_eligibility` (`agent/stats_db.py`) only knows about
  `revertable`/`conflicting`/`reverted` — it has no concept of "not synced
  yet," so a pending-conflict file would likely present as `revertable`
  right up until the revert call itself fails.
- **"Open file" against repo_root would show pre-turn content** that
  doesn't match the diff being displayed — again not wrong data, just a
  view that looks inconsistent with what's on screen, for as long as the
  conflict is open.

**Fix: give sync state its own field, not just conflict/no-conflict.**
Extend the stored `GitDiff` event (or the worktree registry row it's
derived from) with a `sync_status` of `pending` / `promoted`, set to
`pending` at drain time and flipped to `promoted` only when the promotion
step in the previous section actually succeeds. Two call sites then read
it:

- `get_diff_revert_eligibility` gains a fourth state, `pending`, checked
  before `revertable`/`conflicting`/`reverted` — a file can't be reverted
  out of repo_root before it was ever applied to repo_root.
- The UI shows a "pending — resolve conflict to apply" indicator on the
  `GitDiff` block in place of (or alongside) the revert bar while
  `sync_status === 'pending'`, and skips rendering an "open file" link
  that would otherwise point at stale content.

This is new, small plumbing — a status field and two call sites reading it
— not a rework of how the diff is captured or displayed. It exists because
this design deliberately widens the window between "turn produced a diff"
and "repo_root reflects it" from zero (shipped implementation) to
"however long resolution takes," and that widening is real UI surface, not
just a backend concern.

### Carrying a conflict across turns: capturing the list, reusing the worktree, and the UI actions

Three follow-up questions worth answering precisely, since none of them are
"obviously fine" without checking against the actual code: is the conflict
list captured anywhere an LLM turn could see it; does a follow-up "please
resolve this" turn actually run against the *same* worktree; and what does
the user click.

**Is the conflict list captured by the response, so a next turn can use
it? Not today — but there's an exact existing pattern to copy.** Right
now nothing like this design's conflict artifact exists in the codebase.
The closest analog is how `GitDiff` already flows forward:
`_gitdiff_context_summary()` (`agent/stats_db.py:892`) reads a stored
`GitDiff` tool event back out of `chat_messages.context` and renders it as
a plain `<changed_files>` text block, which prompt construction threads
into later turns (`get_messages_by_ids`, `agent/stats_db.py:965`) — that's
literally how a later turn learns what an earlier one changed, without the
model needing to re-read every file. The conflict list needs the identical
treatment: store it as its own event (call it `MergeConflict`, same shape
as the per-file tuple from "The 3-way conflict list" —
`{path, base, ours, theirs, marked-up file}`, plus `repo` and the
still-live worktree path) on the assistant message that hit the conflict,
and add a sibling `_conflict_context_summary()` that renders it as a
`<merge_conflict>` block the same way `_gitdiff_context_summary` renders
`<changed_files>`. Once that exists, "hey, merge it" in the next turn isn't
special — the model already has the file list and the three-way content in
context, the same way it already has prior diffs.

**Does the resolution turn actually reuse the existing worktree, or does
it get a fresh one? Not for free — this needs an explicit special case.**
Checking `_setup_worktrees` (`agent/server.py:519`): every turn calls
`ensure_worktree(repo_root, topic, agent)` with `agent = str(asst_msg_id)`
— and every turn has a *new* assistant message id, so by construction this
mints a brand-new worktree path and branch every time (`ensure_worktree`
only reuses a path if it already exists on disk, which it never does for a
key that's never been used before). Left alone, a follow-up "resolve the
conflict" turn would get its own fresh worktree — seeded from repo_root
same as any other turn — not the one that's sitting there with conflict
markers in it. That fresh worktree wouldn't have the conflict at all; it'd
just silently redo the original turn's work from scratch against whatever
repo_root looks like now, which is exactly the kind of silent-wrong-thing
this whole design exists to avoid.

So resolution turns need to be recognized as a distinct case in
`_setup_worktrees`: before minting a new key for a repo root, check whether
that `(topic, repo_root)` has an open worktree registry row with
`sync_status = 'conflict'`. If so, reuse *that* row's existing
`(wt_path, branch)` for this turn instead of calling `ensure_worktree` with
the new turn's own id. The registry row's key — stored in the `agent`
column, per the existing "predates per-turn keys" caveat — stays pinned to
the *original* conflicted turn's id across as many resolution attempts as
it takes; only once promotion finally succeeds does that row get deleted
and the next ordinary turn go back to getting a fresh worktree and key.
This means several assistant messages (the original turn, plus however
many resolution turns) can share one worktree row — a deliberate,
narrow exception to "every turn gets a fresh worktree," scoped only to
the conflict window.

**Explicit UI action, distinct from revert.** The existing revert bar
(`gitdiff-revert-bar`, `ui/app.js:3552`) undoes an *already-promoted*
change — it doesn't apply here, since a conflicted turn was never promoted
in the first place. This needs its own affordance, shown only when
`sync_status === 'conflict'`:

- **"Ask AI to resolve"** — dispatches a follow-up turn scoped to the
  existing worktree (via the reuse mechanism above), with the
  `<merge_conflict>` summary as context and an instruction to reconcile
  the marked files. Ordinary turn otherwise — same sole-writer worktree,
  same streaming, same everything.
- **"Retry merge"** — for manual resolution: the user has edited the
  marked-up files directly (in an editor pointed at the worktree path, or
  a future in-UI conflict view), and this button tells Squid to re-check
  `git -C <wt> status --porcelain` for zero remaining unmerged entries and,
  if clean, run the promotion step (which itself may loop once more on
  drift, per "Promoting the resolved worktree into repo_root"). If markers
  are still present, this should report that plainly rather than silently
  no-op-ing or generating a fresh conflict list from nothing having
  changed.

Both actions end at the same place — a clean worktree tree, promotion
attempted, `sync_status` flipped to `promoted` on success or back to
`conflict` (with a fresh base/ours/theirs list) if new drift collided.

### Superseded: repo_root-side apply + restore-to-snapshot

Kept for the record, not adopted. The original version of this section had
the worktree's drain applied directly onto repo_root, with conflicts
handled by restoring repo_root to a pre-drain snapshot. That restore was
real, hand-rolled work with no `merge --abort` equivalent to lean on:
`stash apply` has no abort of its own, so undoing a conflicting apply meant
manually resetting the index/tree to the snapshot, deleting stray files the
failed apply created, and re-copying the snapshot's untracked set — three
sequential steps with their own crash window, instead of one atomic Git
call. The inverted design above doesn't do that recovery better; it removes
the need for it, by never giving a conflict a chance to land in repo_root
in the first place.

### What this changes vs. the shipped implementation

- No per-turn commits, no `merge --no-ff`, no rebase, no branch that
  accumulates history. The `sqd-<key>` branch still exists as the
  worktree's anchor but never gains commits of its own.
- No squash-on-push subsystem needed — there's nothing to squash.
- repo_root now stays dirty across turns until the user commits, instead of
  gaining one commit per turn automatically. This is an intentional product
  choice, not a gap: it's the same state a user editing by hand would be in.
- Conflict handling moves from "abort a merge in repo_root" to "merge inside
  the worktree, only promote to repo_root once clean" — repo_root is never
  at risk of ending up mid-conflict, so there's no restore path to build at
  all. Conflicts, when they happen, surface as a structured base/ours/theirs
  list per file (see "The 3-way conflict list"), resolvable by the user or
  by an LLM in a follow-up turn, still scoped to the same worktree.
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
- `GitDiff` capture and storage, and the "source repo is canonical, not the
  DB" rule, are unaffected — `GitChangeTracker.build_event()` computes the
  diff from tree snapshots regardless of how or when sync moves changes
  between worktree and repo_root (see "Diff-viewer implications" above).
- Revert (`apply_reverse_patch`) is *not* fully unaffected — it still
  reverse-applies stored diff text against repo_root exactly as today, but
  now needs the new `sync_status` gating (see "Diff-viewer implications")
  so it doesn't present as available before repo_root actually has the
  change to revert.
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
- Good (target design): conflicts are never written into repo_root — the
  merge happens entirely inside the disposable worktree, so a conflict
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
  Needs an explicit `sync_status` (`pending`/`promoted`) so the UI doesn't
  offer revert against a file repo_root doesn't have yet.
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
- Bad (target design): conflicts still require resolution (by the user or an
  LLM turn) before the worktree is promoted and removed — this design
  changes where a conflict is safe to sit, not whether one can happen.
- Bad (target design): promotion re-checks repo_root for drift since `PRE`
  was captured, so a long-running resolution can loop (re-merge against a
  newer `PRE'`) rather than promote on the first attempt.
- Bad (target design): a resolution turn must not get the normal fresh
  per-turn worktree — `_setup_worktrees` needs a new special case that
  detects an open `sync_status = 'conflict'` row for `(topic, repo_root)`
  and reuses its worktree/branch instead of minting one for the new
  message id. Without it, "ask AI to resolve" would silently redo the
  original turn's work in a brand-new worktree that never saw the conflict
  at all (see "Carrying a conflict across turns").
- Bad (target design): the conflict list needs its own stored event and
  prompt-summary function (`MergeConflict` / `_conflict_context_summary`,
  mirroring `GitDiff` / `_gitdiff_context_summary`) before a follow-up LLM
  turn can be handed it as context — not implemented today, since neither
  the event type nor the worktree-reuse path above currently exist.

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
