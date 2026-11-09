---
status: proposed
date: 2026-11-08
updated: 2026-11-08 (review revisions)
---
# ADR-0055: Named Branch Workspaces

## Context

ADR-0025 isolates every turn in a branchless turn directory (no `.git`) and
syncs its edits into the backing checkout (e.g. `~/Work/squid`) as ordinary
uncommitted changes. That keeps agents from pushing internal refs, but it
gives each topic exactly one destination: the user's current checkout. Running
two independent pieces of work in parallel means either mixing them in one
working tree or switching branches in the checkout under a running dev server
and other topics' syncs.

Squid already has the building block. Task workspaces
(`agent/task_workspaces.py`, used by reviewed task publication) are persistent
real `git worktree`s under `~/.squid/tasks/<uuid>` on branch
`squid/task/<uuid>`. Per-turn directories sync into them instead of the backing
checkout, with identity validation (`validate()`), immutable per-turn
destination binding (`turn_task_workspaces`), and conflict recovery. They are
anonymous, creatable only through a recorded grant, and not addressable by
the user. No ADR records them.

## Decision

Generalize task workspaces into **named branch workspaces**: user-addressable,
persistent worktrees that a topic's turns sync into instead of the backing
checkout.

### Vocabulary: branch outside, worktree inside

- Users see and say **branch** (`history-paging`). Prompts, UI, and agent
  replies use that word.
- Each branch is backed by one Squid-managed `git worktree` per code root at
  `~/.squid/workspaces/<repo_hash>/<name>`. Its path is an implementation
  detail, shown only in tooltips.
- `main` is the implicit default workspace: the backing checkout itself, and
  the **only branch with no Squid-managed worktree**. With no named branch
  active, behavior is exactly today's, including `worktree.enabled`.

### ADR-0025 is unchanged; branch turns are always isolated

Agents never run in the worktree. Each branch-bound turn gets a branchless
turn directory with no `.git`. The only change is the **sync destination**: the
bound branch's worktree instead of the backing checkout. `sync_after_turn`,
the integration worktree, and conflict recovery run against that worktree as
`repo_root`. ADR-0025's safety property (no agent-reachable `.git`) holds.

**Branch-bound turns use per-turn isolation regardless of
`worktree.enabled`.** That setting defaults to `false` (ADR-0025, 2026-11-03).
Without isolation, the agent would run inside a real worktree with a
pushable `.git`, which is the 2026-07-22 leak. `main`-bound turns keep
following the setting.

### Data model (SQLite, `~/.squid/squid.db`)

The database is the source of truth. Git is verified against it, never
the reverse: `git worktree list` knows nothing of topics, active state, or turns.

| Table | Change | Purpose |
|---|---|---|
| `task_workspaces` | add `name`, `origin` (`publication` \| `named`), `parent_id`, `parent_head_at_fork`; `UNIQUE(topic, name, repo_root)` | name → branch + worktree per repo |
| `topic_workspaces` | new: `(topic PK, name)`; absent row = `main` | the topic's active branch |
| `turn_task_workspaces` | primary key becomes `(msg_id, repo_root)`; add `branch_name` | immutable per-repo record of where each turn ran |

Today `turn_task_workspaces` is keyed by `msg_id` alone, and `bind()` rejects a
second destination, so one turn cannot bind both Squid and Shore. `bind`,
`binding`, `for_turn`, `check_destination`, and the recovery and integration
tables become per-repo. `branch_name` is stored with the binding so
history renders correctly after the workspace row is gone.

One name spans all of a topic's code roots (`squid@history-paging`,
`shore@history-paging`). Repos with no changes may create their worktree
lazily. Topic memory is **not** used. It is agent-visible and editable, and
binding must be host-fixed.

### Binding: decided by the host before dispatch

A turn's branch is resolved and recorded **before** its turn directory is
created and cannot change during the turn. Resolution order:

1. **Reply-to inheritance**: replying to a message uses that message's
   branch. If that branch was merged or dropped, fall back to the active
   branch and never recreate it. The fallback is user-visible: the
   composer chip shows the resolved branch before sending, and the response
   states the fallback.
2. **Explicit prefix**: `@name …` or a leading `in <name>, …` binds this
   message only.
3. **Topic active branch** (`topic_workspaces`), else `main`.

Agents cannot redirect their own turn. Fuzzy requests ("start a branch for
X", "the paging thing") are handled by the agent calling `agentsquid ws
new|use`. That call is authorized only by the original human request (same
check as `task_workspaces.create`). The actual work is then scheduled as an
ADR-0054 continuation step bound to the new branch. Retrieved history or tool
output can never switch branches.

### `agentsquid ws` contract

| Command | Effect | Authorization |
|---|---|---|
| `ws list [--json]` | branches for the current topic, per-repo state | read-only |
| `ws new <name> [--from <branch>] [--move [paths…]]` | create (optionally stacked or moving changes) | original human request |
| `ws use <name>` | set topic active branch | original human request |
| `ws merge <name> --mode squash\|merge\|apply\|pr` | open the merge confirmation | original human request plus modal confirmation |
| `ws drop <name>` | remove worktree, retain ref | original human request plus confirmation if unmerged |

All commands emit JSON with `--json`. State changes are recorded as
requests that the host executes. The CLI never runs Git effects in the
agent's process. Each command is listed in `agentsquid tools` like other
ADR-0054 capabilities.

### Prompts

| Prompt | Effect | Sticky? |
|---|---|---|
| "start a branch for history paging, then add cursor paging" | create `history-paging`, queue work there as a continuation | yes |
| "in history-paging, handle empty pages" / `@history-paging …` | bind this message | no |
| "switch to history-paging" / "back to main" | change active branch | yes |
| "what's open?" | list branches: ahead/behind, uncommitted changes, last turn, size | n/a |
| "merge it" / "squash it in" / "bring it over" | merge flow (below) | n/a |
| "open a PR for it" | `task_review` → `task_publication` | n/a |
| "drop it" | remove worktree (confirm if unmerged) | n/a |

Names are slugged from the request by the agent (`history paging` →
`history-paging`), validated with `git check-ref-format`, and checked for
collisions locally across topics and on the remote. Renaming changes the
display name only; the git branch name is stable. Ambiguous references ask
and never guess.

### Creating a branch with uncommitted changes present

- **Default: start clean** from the source's HEAD. Uncommitted changes stay
  where they are, with a notice: "`main` has 3 uncommitted files; left on
  `main`. Say 'move them' to bring them along."
- **Move only on request** ("move my changes to a new branch", "move just
  the history files", "put what you just did on a branch"). The last of
  these limits the move to that turn's recorded diff. Move procedure: hold the
  common-git-dir lock → snapshot the source's dirty state as a tree
  (`_snapshot_tree`-style temporary index with `add -A`, so **untracked files
  are included**) → create worktree from HEAD → materialize that tree →
  verify the branch's snapshot tree hash equals the source's → only then
  restore the selected paths on the source to HEAD. Any failure leaves the
  source untouched. Ignored files never move; that is the defined scope of a
  move, not a partial move. All repos move or none.
- **Never copy silently.** Duplicated uncommitted changes conflict at merge.
- In-flight turns bound to the source sync into the source after the move.
  Wait or warn.

### Merging

Within a branch, turn edits land in its worktree as uncommitted changes
(same as `main` today). A branch may therefore hold commits plus uncommitted
changes at merge time.

| Mode | Result on target | Separation after merge |
|---|---|---|
| **Squash** (default) | one commit | kept: one revertable commit |
| Merge | uncommitted changes committed (message confirmed), then `git merge`, fast-forward if possible | kept: full history, `git revert -m1` |
| Apply as changes | total diff as uncommitted changes; **ends the branch** | **lost**: mixed with target's working tree |
| PR | push + PR via task publication | kept |

Rules:
- Every merge opens a confirmation in the **common themed modal**:
  per-repo commits/uncommitted counts, how far the target moved, and buttons
  `Squash` · `Merge` · `Apply as changes` · `Cancel`. The prompt's
  wording preselects a button.
- **Target with uncommitted changes:**
  - Squash/Merge: allowed only if none of the target's uncommitted paths overlap
    the branch's changed paths. The merge is computed in an integration
    worktree and committed from its index. The user's unrelated uncommitted
    files are left untouched and never included (no `commit -a`). Overlap
    blocks the merge with the overlapping paths listed.
  - Apply as changes: only allowed when the target has no uncommitted
    changes. Otherwise suggest Squash or committing first.
- Conflicts use the existing integration-worktree recovery turn. Nothing is
  left half-applied.
- Cross-repo order: **shore → pin `release/shore-client.json` → squid**,
  with a check that the pinned commit is reachable on the target. All repos merge
  or none.
- After **Merge**, the branch is kept. Git tracks what was merged, so further
  work merges only the new changes.
- After **Squash**, the branch is kept and Squid resets it to the new target
  commit. The squash commit contains exactly the branch's tree delta, so no
  work is lost and later merges cannot replay it.
- **Apply as changes ends the branch.** Its changes exist on the target only
  as uncommitted edits. No commit records them, so there is no baseline
  to rebase the branch onto, and continuing would replay them. The worktree is
  removed and the ref retained, as with drop. To keep working, use Squash or
  Merge.

### Stacks

- **Default parent is `main`**, even when another branch is active. "Start a
  branch" creates a sibling.
- Stacking only on request ("branch off paging", "build on paging"). Store
  `parent_id` and `parent_head_at_fork`. Depth is the `workspaces.max_stack_depth`
  setting, default 2. Each level multiplies restack conflict surface, since one
  parent change replays through every descendant.
- Squid owns restacks. **Restacking always goes through the parent's final head**
  so parent commits added after the fork are never dropped:
  1. `git rebase --onto <parent_final_head> <parent_head_at_fork> child`.
     This reconciles the child with every parent commit, and conflicts are
     resolved here against the real parent work.
  2. On parent merge (Squash or Merge): `git rebase --onto <target>
     <parent_final_head> child`. Only the child's own commits move.
  3. Update `parent_head_at_fork` after each step.
- Triggers: parent merged → automatic (steps 1–2, conflicts go to the recovery
  turn). Parent gained commits → no prompt per commit. Restack is offered
  when the child is merged or the user asks. Parent dropped → offer rebase
  onto `main` or drop the child too. A parent merged with Apply ends the parent,
  which blocks the child until the target's changes are committed.
- Merging a child whose parent is unmerged prompts: "Merge the stack
  (paging → paging-tests)?" PR mode bases the child PR on the parent branch
  and switches it to the target when the parent merges.

### Git-only turns and push

`agent/runtime/worktree.md` redefines "backing repo" and "current branch" as
**the turn's bound branch and its worktree**. Commit, push, and status in a
git-only turn run there. Push remains current-branch-only. `main`-bound turns
are unchanged.

### Environment

- **Each branch gets a private environment at creation, before any turn
  runs.** A turn can run `uv sync` / `npm install` through a shared
  `.venv` / `node_modules` link mid-turn, so detecting a lockfile change
  after the turn is too late. Named branches are long-lived, which makes
  divergence likely.
- Implementation: copy-on-write clone (`cp -c` / APFS `clonefile`, `cp --reflink`
  where supported) of the source's configured dependency directories into
  the worktree. This is near-instant and uses no extra disk until modified. Where cloning is
  unavailable, fall back to a full copy, or to a fresh `uv sync` / `npm ci`
  from the branch's lockfile. Per-turn directories link to the **branch's**
  environment, never `main`'s.
- Ignored-file links (`.env` etc.) reuse `_link_ignored_files`.
- The dev server runs from the backing checkout, so only `main` can be tested
  live. A per-branch dev server on its own port is future work. The UI states
  this rather than implying otherwise.

### UI

1. **Composer branch chip**: parent `#chip-row` in `#input-area`
   (`ui/index.html`), new `#branch-chip` after `#topic-chip`.
   - `⎇ history-paging +3 •` (ahead count; dot = uncommitted changes). Muted or
     hidden on `main`.
   - Reflects the **resolved binding for the message being typed**: typing
     `@foo` or replying to a message on another branch updates it before
     send.
   - Tap menu: switch, new branch, merge, drop. The menu shows per-repo status for
     mixed states.
   - Stacks render as `⎇ paging › paging-tests`.
   - States: `· conflict` (links to recovery turn), red `needs repair`
     (worktree missing or `validate()` failed; send blocked until repaired
     or forgotten).
   - Mobile: collapse to `⎇` + truncated name.
2. **Per-response tag**: in each assistant message's meta line,
   `⎇ history-paging` from `turn_task_workspaces.branch_name` (not current state).
   Hidden for `main`. Strikethrough plus a tooltip once the branch is merged or
   dropped.
3. **GitDiff source label**: `_gitDiffSourceRepo()` (`ui/app.js`)
   renders `squid @ history-paging`. The path goes in the tooltip.
   `_isSquidWorktreePath` must not treat named-branch worktrees as per-turn
   directories.
4. **Topic list badge**: rows in `#topics-list` show muted `⎇ 2`
   when a topic has open branches; hover lists them.
5. **Merge modal**: common themed modal (see Merging).

Per the PWA rule, UI changes bump all five version strings across `sw.js`
and `index.html`.

### Lifecycle and recovery

- Create, move, merge, and restack are each recorded intent-first (`preparing`
  → `ready`, as `create()` does today) and resumable after a Squid restart.
  Two-repo operations commit as one unit.
- Drop with unmerged or unpushed work requires confirmation. The worktree is
  removed but the branch ref is retained for 14 days.
- Named branches are never removed by `cleanup_worktrees` grace-period
  cleanup. Removal is explicit only. Branches idle more than 14 days surface
  a reminder in the list. The reminder appears once per branch per idle period,
  not on every turn.
- Out-of-band git changes are detected, not repaired silently:
  - branch checked out in the backing checkout → `worktree add` fails; explain,
    never `--force`;
  - worktree deleted, branch renamed/deleted, `git worktree prune` →
    `needs repair` (recreate from branch, or forget);
  - force-push/outside rebase breaks `base_sha` ancestry → offer re-base.
- Locking has two levels:
  - **Common git dir** (shared by `main` and every branch worktree of a
    repo): object and ref writes such as `commit-tree`, `update-ref`,
    `worktree add/remove`, merge, move, and restack. This serializes `main`
    turns against branch turns where they actually share state.
  - **Worktree path**: promotion into that worktree (`sync_after_turn` apply).
    Turns on different branches promote in parallel; turns on the same
    branch serialize.
  Lock order is always common git dir, then worktree path.

### Watches and Flows

Out of scope for v1. Watches and Flows keep using publication-origin task
workspaces and cannot target named branches.

## Alternatives

- **Real branch checkout in the backing repo**: one checked-out branch per
  directory. Switching disturbs the dev server and other topics. No
  parallelism.
- **Expose the worktree to the agent as cwd**: reintroduces the 2026-07-22
  push leak that ADR-0025 closed.
- **Git as the mapping store**: cannot represent topics, active branch, or
  immutable turn binding.
- **Copy the environment only after a lockfile change**: too late. Dependency
  commands mutate the shared environment during the turn.
- **Topic memory frontmatter for the active branch**: agent-editable, so
  binding would not be host-fixed.
- **One branch per topic, no switching**: simpler, but forces a new topic per
  work stream and loses conversation continuity. Viable as a v0 (see below).

## Consequences and scope

- Parallel work streams without touching the user's checkout, and without
  weakening per-turn isolation.
- Task-publication workspaces and named branches share one table and one
  validation path.
- New surface: binding resolution, merge/move/restack engines, cross-repo
  atomicity, UI states. Highest-risk items: shared-checkout conflicts,
  cross-repo pin order, wrong-branch edits (mitigated by the UI), atomic
  multi-repo operations, redefining "current branch".
- Branch-bound turns are always isolated, so a user who never creates a
  branch sees no change. A user with `worktree.enabled: false` gets
  isolation only for branch turns.

### Phasing

- **v0**: one active branch per topic. Create/switch/list/drop, forced
  isolation for branch turns, per-repo turn binding, private environment at
  creation, two-level locking, composer chip, response tag, GitDiff label,
  Squash and Apply (branch-ending) merges with the target-overlap rule,
  start-clean only, `agentsquid ws` (`list/new/use/merge/drop`).
- **v1**: `@name` / reply-to binding, Merge mode, PR mode, move-changes,
  topic-list badge.
- **v2**: stacks with final-head restack, per-branch dev server.

### Review revisions (2026-11-08)

Incorporated from two reviews:
- Per-repo turn binding. `turn_task_workspaces` previously allowed one
  destination per turn.
- Apply now ends the branch. The earlier rule to rebase onto the target after Apply
  had no commit to rebase onto.
- Restack goes through the parent's final head. The earlier one-step `--onto` dropped
  parent commits added after the fork.
- Squash and Merge rules for a target with uncommitted changes (overlap blocks;
  never `commit -a`).
- Branch turns are isolated regardless of `worktree.enabled`. `main` has no worktree.
- Private environment at creation in v0, replacing the earlier post-hoc
  lockfile detection.
- Two-level locking (common git dir, then worktree path).
- Move verification by snapshot tree hash, including untracked files.
- `agentsquid ws` contract, configurable stack depth, a visible
  reply-to fallback, `branch_name` recorded with each turn, and a reminder
  that does not repeat.

### Doc follow-ups on acceptance

- ADR-0025: "Extended by ADR-0055" in frontmatter and Status.
- `docs/task-publication.md`: link task-workspace explanation here.
- `docs/db-schema-target.md`, `docs/data-model-and-api.md`: new columns,
  `topic_workspaces`, `ws` API.
- `docs/continuations.md`: branch-bound steps. Dropping a branch cancels its
  pending steps (fail closed).
- `agent/runtime/worktree.md`: bound-branch wording for git-only turns, and
  injection whenever a turn is branch-bound, even with `worktree.enabled: false`.
- `docs/continuations.md` / `agentsquid tools`: document `ws` commands.

## References

- ADR-0025: Per-turn worktree isolation
- ADR-0054: Agent-callable capabilities and durable continuations
- `agent/task_workspaces.py`, `agent/worktree.py`
- `docs/task-publication.md`
