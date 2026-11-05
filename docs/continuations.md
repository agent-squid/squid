# Durable turn continuations

Continuations are enabled automatically with worktree isolation. In
`~/.squid/squid.yaml`, set the existing option and restart Squid:

```yaml
worktree:
  enabled: true
```

There is no separate continuation setting; any older `continuations` section
is ignored. With worktree isolation disabled, continuations are disabled and
ordinary turns can edit and commit directly. Existing pending work remains
subject to its recorded mode during cleanup and recovery.

A human one-shot CLI turn receives a separate, short-lived
`SQUID_CONTINUE_TOKEN` and continuation tool guidance. Watch enablement is not
required. Watch, Flow, shell and persistent-harness turns cannot schedule work.
Continuation turns cannot extend or change their predeclared sequence; sequence
steps receive a credential only to report their outcome and inspect/cancel work. No production setting is changed by shipping this feature.

For a request such as “fix this and commit it”, the editing agent writes and
checks the fix, then calls:

```sh
agentsquid continue create --input - <<'JSON'
{"request_key":"commit-fix","mode":"backing_git","prompt":"Inspect and commit the fix requested by the user, excluding unrelated changes."}
JSON
```

The returned record means **scheduled**, not completed. The parent can then
respond that the edit is finished and the commit step is waiting for sync.
Squid, rather than the exiting CLI, owns subsequent execution. After successful
completion and sync of every participating repo (including unchanged repos),
Squid prepares a linked successor message in the same topic using the same
agent and normal queue. For “fix, commit and push”, queue both Git steps in that
one successor. The edit response reports the completed edits and queued Git
steps; the successor reports the actual commit and push outcomes, including a
successful commit followed by a failed push. It must not promise another turn.
Ordinary edits never schedule or commit automatically.

`backing_git` permits inspection, requested local commits, and pushes explicitly
requested by the original human user. Push only the current backing-repo branch;
never force-push, push all branches/mirror, or create/publish per-turn `sqd-*`
branches. The parent result and successor prompt cannot authorize a push on
their own. File edits, tags, PRs, releases and other publication remain prohibited.
`isolated` permits local inspection, edits and checks, with ordinary post-turn sync, but no Git mutation
or publication. The original human request and parent result accompany the
successor; its generated prompt cannot expand the original authorization.
Accumulated changes may be committed when requested; the last turn's diff does
not define mandatory commit scope. The successor must inspect current state
because other work can land between sync and execution.

One human turn can accept one request: either the single-successor form above,
or an ordered sequence of up to eight follow-on steps (48,000 prompt characters
in total, 16,000 per step). Split work at each Git boundary. For example, after
the initial edit, “edit, commit, edit, commit and push” schedules:

```json
{
  "request_key": "two-commits",
  "steps": [
    {"mode": "backing_git", "prompt": "Commit the first requested edit."},
    {"mode": "isolated", "prompt": "Make and check the second requested edit."},
    {"mode": "backing_git", "prompt": "Commit the second edit and push the current branch, as explicitly requested by the user."}
  ]
}
```

Each step receives the original human request, the immediately preceding result,
and the remaining plan. Its mode applies only to that step. Edit steps get fresh
isolation through normal turn preparation. Git steps inspect the backing repo's
current state; existing commits on the current branch are included in a push.
The plan does not freeze the repository or expand the original authorization.

Before its final response, every sequence step must record an explicit outcome:

```sh
agentsquid continue result --input - <<'JSON'
{"success":true}
JSON
```

Use `false` for failed commands/checks or incomplete work. Reporting success is
required but insufficient: persisted successful completion and all sync results
must also succeed. Missing or false results stop the sequence. Outcome retries
are idempotent; a different outcome conflicts. The single-successor input remains
compatible and does not require this acknowledgement.

The next step is persisted atomically with its predecessor's completion gate,
so a restart cannot lose or duplicate an acknowledged transition. An identical
request-key/payload retry returns the original record; a different payload
conflicts. No step can append further steps. Each response deadline is capped at
600 seconds. The scheduler dispatches one step at a time globally. Queue wait is
not charged as execution time. Configurable route, cost and aggregate budgets
remain outside this implementation.

Use `agentsquid continue list`, `show <id>` and `cancel <id>` from a human turn
in the same topic. Cancelling any step cancels waiting/eligible work in its sequence and clears
all remaining steps. It does not stop a claimed/running turn (use the existing
turn stop control), but that turn cannot advance the cancelled sequence. A later human turn
can inspect earlier records after the parent's credential has been revoked.
Records include parent and child message IDs, state and reason. The continuation
message itself names its parent; no custom browser UI is introduced.

Errors, cancellation, unknown completion, sync lookup failure, conflicts and
promotion failures block eligibility. Restart recovers eligible work. A claim
is persisted before message preparation; interrupted claimed/running work with
no durable completion gate becomes `ambiguous` and is never automatically
replayed. This is conservative even when the claim might not have started.
Conflicts and ambiguous results require inspection and a new human instruction;
there is no automatic resume/retry endpoint in this slice. Deleting the parent
prevents unclaimed work from starting. Stopping or deleting an already running
parent/child does not roll back Git effects.

These modes are prompt restrictions, **not an OS sandbox**. Detected file
changes in a Git-only turn fail the turn before isolated promotion; this cannot
prevent arbitrary shell actions, detect every backing-repo edit, or roll back
commits. Credentials are isolated from child environment inheritance and revoked
when the issuing process ends; accepted work survives revocation.

CLI/HTTP delivery is implemented. Shared watch-registry discovery, native MCP
mapping, richer UI, atomic preparation/message linkage, configurable aggregate
budgets, route/watch support and broader publication remain future ADR-0054 work.
Watch publication remains disabled; explicit human-requested continuation pushes
do not expand watch execution grants.

The issuing turn captures its canonical code roots and configured agent directory
in the launch credential; creation copies that scope into the durable record.
Dispatch validates the current configuration and the prepared roots, and the
worker checks again before starting the agent. A mismatch blocks the successor
with `repository_scope_changed`. Existing records without captured scope are
also blocked rather than assigned a new scope. Changing topic roots while a
continuation waits therefore cannot redirect it to another repository.

Detected Git-only file edits mark the affected isolated worktrees
`promotion_failed` for manual attention before the turn fails. Both cleanup and
admission settlement preserve these worktrees without automatic promotion.
The rejected edits remain available for inspection.

Automatic promotion independently checks the persisted Git-only mode before
copying opted-in ignored files or applying any patch. Dirty Git-only worktrees
are retained with `promotion_failed`, even after runner exceptions,
cancellation or restart, and even if continuations have since been disabled.
The check reads the actual tree rather than trusting a cached no-op snapshot.
Unchanged Git-only worktrees still complete as successful no-ops.

Server lifespan cleanup covers startup as well as serving and shutdown. If
startup fails after a maintenance worker is created, that worker is cancelled
and awaited before the failed lifespan exits. Retrying startup in the same
process therefore cannot accumulate continuation dispatch loops.
