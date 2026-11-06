# Task workspaces and fix, review, revise and PR workflows

Status: implementation proposal. Watch publication remains disabled.

## Intended behavior

A human can ask: “Watch my new bug reports in Squid. Fix each, review separately,
address findings, and open a PR.” Squid records that intent in a bounded watch
grant. New issue text supplies task data, never additional authority. The host
owns the sequence after the original CLI process exits. No merge or deployment
permission is implied.

The same workspace and publication mechanisms support an ordinary human request
such as “Fix this and open a PR” from the main checkout. A watch is one owner of
a task, not a prerequisite for task workspaces. Direct requests inherit their
recorded human authorization; they do not require a watch grant.

Reuse the durable continuation scheduler and watch-owned Flow execution. Do not
remove source checks in isolation or label synthetic watch messages as human.

## Authorization and ownership

Extend the authenticated watch-grant path to record a structured, optional PR
publication scope: repository identity, remote, base branch, and permission to
push a dedicated task branch and create/update its PR. Existing local-only
grants retain their behavior. Bind the scope to the recorded human instruction,
watch, expiry, configured agents and canonical code roots. The original issue
body, agent output and generated continuation prompts cannot widen it.

Introduce an explicit owner on continuation sequences: direct human request,
watch run, or human-owned Flow run. Watch continuations retain watch/run/grant
IDs; human Flow continuations retain their original human request and owning
Flow. Factor the existing source checks in `continuations.mint`, `create` and
`dispatch` into owner-aware authorization checks. Keep message provenance intact.
A generic permission to run a local watch does not permit publication.

Check owner authority when accepting, claiming and starting every step. Revoking
or cancelling the owner prevents future steps. Expiry, scope drift and exhausted
budgets stop work. A predeclared sequence has at most eight follow-on steps;
children cannot append work. One watch attempt covers the bounded sequence,
with per-step deadlines and explicit successful outcomes. Reuse the existing
watch/Flow cancellation and user-work priority rules rather than adding a
competing dispatch path.

## Persistent task workspaces and per-turn isolation

For PR-mode grants, select one issue identity per run instead of batching
unrelated issue events into one PR. Persist a task workspace per issue and
repository, based on a pinned committed base SHA. Do not seed it from dirty
backing-checkout contents. Existing local-only watches may continue batching.
For direct human requests, allocate a task identity independent of issue number;
record its originating request and selected repository/base the same way.

The persistent task workspace is a real Git worktree with its own task branch.
Its `.git` entry may be a file pointing into the repository's worktree metadata.
It survives individual turns and holds the accumulated task changes. Per-turn
“worktrees” remain temporary materialized directories without `.git`; these are
two distinct layers. Persist task ID, canonical repository identity, worktree
path, branch, pinned base SHA and eventual PR identity. Opening the PR does not
discard the workspace: later revisions use the same task. Never automatically
remove dirty, conflicted or unresolved workspaces; cleanup must preserve their
recovery information.

Editing turns remain branchless isolated directories. Their successful sync
promotes into the task workspace, not the shared backing checkout. Review and
revision steps use that same accumulated workspace and pinned predecessor
results. Existing preparation, worker admission, cleanup and recovery must all
resolve the persisted sync destination; a missing destination blocks rather
than falling back to the shared checkout. This destination must be bound to the
owner's repository scope before the first edit.

A new publication step operates on the persisted task workspace after all local
steps complete and sync. It commits the reviewed changes and pushes only the
recorded task branch, then creates or updates its PR. Task branches are distinct
from internal per-turn `sqd-*` state. This requires a new explicit publication
mode; do not broaden `backing_git`, whose current-branch-only contract remains
unchanged. Cross-repository requests must explicitly select publication repos;
start with a single publication repository and reject unsupported combinations.

A hosted PR needs a remote branch. Never switch the human's checked-out branch,
force-push, push unrelated refs, merge, release, or deploy. These are future
implementation requirements, not authorization to publish during this task.

## Conflict resolution

A sync conflict retains the isolated turn directory and the Git integration
worktree, with both linked to the owning task and its exact sync destination.
Pause dependent steps and publication. Resolve conflicts in that retained
integration worktree, then promote the resolved result into the task workspace,
never into the human's shared checkout. Recheck destination identity and scope
before promotion; a missing or changed task destination blocks recovery.
Review and validation must run again on the resolved code before publication.

Conflicts with a newer PR base are handled in the persistent task workspace,
using an explicitly scheduled isolated resolution turn. Do not silently change
the pinned base or discard local edits. Record the selected new base and result;
use merge-based updates compatible with ordinary pushes rather than rewriting
published history. An unexpected remote task-branch head still blocks until it
is reconciled. Conflict recovery survives restart; it does not reset grants,
renew expiry or manufacture extra retry budget. If authority has expired, a new
human instruction is needed to continue. Preserve unresolved artifacts meanwhile.

## Main chat and later turns

Creating a task branch or completing its PR does not switch the main chat's
workspace. “Main chat” means the topic's configured/default checkout, whatever
branch it already has checked out; do not assume that branch is named `main`.
The task's automatic steps remain bound to its own workspace throughout.

Persist the topic's default workspace separately from an optional explicitly
selected active task. With no explicit selection, a new ordinary turn uses the
default checkout. A request such as “Address comments on PR #123” targets that
PR's saved task workspace for that turn and its declared follow-on steps only.
It does not silently change the destination of later unrelated turns.
“Switch to PR #123” selects that task for subsequent turns; “Back to the main
checkout” clears the selection. These select a workspace, not a branch checkout
in the human's repository. Resolve ambiguous PR/task references before editing.

Show the effective repository, workspace/task and branch on each turn, and make
any persistent selection visible. Capture the resolved workspace identity when
a turn is accepted so another selection cannot redirect queued or running work.
A missing/deleted workspace or scope mismatch blocks the turn instead of falling
back to the default checkout. Workspace selection alone authorizes neither a
push nor a PR update. When a PR is merged/closed, retain its identity and local
artifacts; do not silently redirect an explicitly selected task to another tree.

## Durable publication and completion

Persist repository, base SHA, task branch, intended commit SHA, push state and PR
URL/number before considering the sequence complete. Use a stable identity per
issue/workflow to find an existing PR before creating one. A retry must compare
remote state with the intended SHA; an unexpected branch head blocks execution.

GitHub PR creation and the local database cannot be one transaction. If a crash
occurs after the external action but before recording its result, reconcile the
known branch and PR identity. Confirmed effects may be recorded without repeating
them; uncertain or conflicting effects pause for inspection. Do not blindly
replay publication. Retain existing conservative continuation recovery.

The watch run remains unresolved, holding its ownership/concurrency slot, until
all authorized steps finish. `watch_runs.settle` must include descendant sequence
outcomes before marking events handled. Distinguish local success from publication
failure so a failed push or PR creation never reruns an already completed fix.
Record and show partial outcomes, including commit succeeded/push failed and push
succeeded/PR failed. Cancelled work does not undo an already published PR.

## Implementation order and verification

1. Add owner-aware continuation records and grant validation. Verify human
   continuations still work, local-only watch grants cannot publish, and issue
   content cannot create permissions. Cover human-owned and watch-owned Flows.
2. Add task workspace ownership and sync routing through normal preparation,
   worker completion, cleanup and restart recovery. Prove dirty backing files
   and concurrent human changes never enter the task diff or change destination.
   Cover both direct human tasks and issue-owned tasks, real Git task worktrees
   and branchless per-turn directories, and conflict resolution that promotes
   only to its recorded task workspace after restart.
3. Link bounded Flow/continuation completion to watch settlement, budgets,
   revocation and cancellation. Prove no event is acknowledged early and no
   second run starts while descendants remain unresolved.
4. Add the publication mode with durable intent and reconciliation. Use local
   bare remotes and a fake GitHub service to exercise push success/failure,
   duplicate suppression, conflicting branch heads and crash boundaries.
5. Add explicit workspace selection and per-turn target capture. Verify creating
   a task leaves the main chat unchanged, PR follow-ups target the existing task,
   one-turn targeting does not become sticky, explicit switches persist across
   restart, and selection changes cannot redirect queued work. Cover ambiguous
   references and missing, closed or deleted tasks without silent fallback.
6. Run a disposable real-model, two-issue E2E with an explicitly authorized test
   repository: fix → separate review → revise → PR for issue one, then issue two.
   Check both PR diffs, untouched backing checkout, no duplicate jobs after
   restart and no merge. Also cover an ordinary human “fix and open a PR” request,
   a later review-comment turn on that same PR, and an unrelated main-chat turn
   that still uses the default checkout. Keep mock/Echo coverage distinct from
   this live proof.

Relevant existing modules: `agent/continuations.py`, `agent/watch_flows.py`,
`agent/watch_runs.py`, `agent/watch_launch.py`, `agent/worktree.py` and the
ordinary turn preparation/worker completion paths. No new general-purpose
scheduler is needed.
