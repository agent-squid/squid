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

Sequences started from an ad hoc turn keep a private session across their steps.
Each step resumes the preceding step's native session when available, with only
that sequence's user/assistant history supplied as a fallback for stateless
runners or failed resumes. They never read or update the route's regular
session. A later independent ad hoc prompt starts fresh. Sequences started from
a regular session retain the normal route session behavior.

A human one-shot CLI turn receives a separate, short-lived
`SQUID_CONTINUE_TOKEN` and continuation tool guidance. Watch enablement is not
required. Watch and durable human-owned Flow turns can schedule isolated local
steps under their recorded owner; shell and persistent-harness turns cannot
schedule work. Watch/Flow grants never authorize `backing_git`. Explicitly activated task scopes
can use read-only `task_review` followed by terminal host-run `task_publication`;
see [task publication](task-publication.md).
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

For ordinary human continuations, the visible input contains the next action
and current sync/sequence facts. Source message IDs carry the original request
and sequence results separately from pins. At runner launch Squid reuses source
content from the actual resumed session, injecting only missing sources. A fresh
retry recomputes delivery; an ad hoc sequence's supplied history is not injected
again. Keep scheduled prompts concise rather than repeating history or standard
execution rules. Watch/Flow-owned continuation prompts retain their scoped
handoff behavior.

The ctx popup links source messages (including a Flow's starting message) and
shows whether each source was referenced, already in session/history, or
injected. It also previews recorded runtime and human-continuation instruction
templates, with their revisions and rendered values. Each launch attempt keeps
its own snapshot, so a fresh retry remains distinguishable from its failed
resume. Older turns without snapshots show references only; they do not claim
historical template delivery. Mandatory instructions still reach the runner;
these viewer links do not replace their delivery.

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
conflicts. No step can append further steps. Each step uses the ordinary
`agent.response_timeout` deadline, without a separate ten-minute cap. Its result
credential lasts for the launch's response deadline plus 60 seconds, and is
revoked when the issuing process ends. The scheduler dispatches one step at a time globally. Queue wait is
not charged as execution time. Configurable route, cost and aggregate budgets
remain outside this implementation.

Use `agentsquid continue list`, `show <id>` and `cancel <id>` from a human turn
in the same topic. Cancelling any step cancels waiting, eligible and claimed
work in its sequence and clears all remaining steps. If preparation is in
progress, its prepared child is cancelled before dispatch. Cancellation first
persists the sequence freeze, then drains linked queued children and stops active
children through the existing turn controls. Maintenance retries interrupted
stops even when new continuations are disabled or dispatch is waiting on an
active child. The server owns and joins both maintenance loops at shutdown.
A stop's blocking worker is joined before shutdown completes, including repeated
cancellation. Failed process signals retain their registry entry for retry.
A failed stop is logged and retried; the cancellation response still reports
the accepted durable freeze. A reconciliation failure is logged separately and
does not skip cancellation attempts in that maintenance cycle. Completed effects are retained;
cancellation cannot undo a commit or other external action. A later human turn
can inspect earlier records after the parent's credential has been revoked.
Records include parent and child message IDs, state and reason. The continuation
message itself names its parent; no custom browser UI is introduced.
Lifecycle changes also append scoped `continuation.changed` events to the
existing durable realtime stream. Events include sequence/message links, mode,
state, reason, cancellation flag and reported result, without prompts or
credentials. The event and domain update commit together; retries that change
nothing emit no extra event. Existing clients may ignore this event type and
continue using CLI/HTTP inspection.

Errors, cancellation, unknown completion, sync lookup failure, conflicts and
promotion failures block eligibility. Restart recovers eligible work. Periodic
maintenance also reconciles saved completion gates: a waiting parent with a
known sync outcome becomes eligible or blocked, and a child with a known
completion gate settles and advances its saved sequence once. Message status
alone never proves sync success. Ungated live claims remain untouched; only
startup classifies interrupted claims as ambiguous. Reconciliation neither
revokes live credentials nor replays external actions, and a cancelled plan
cannot advance even if a successful gate arrives later. A claim
is persisted before message preparation; interrupted claimed/running work with
no durable completion gate becomes `ambiguous` and is never automatically
replayed. This is conservative even when the claim might not have started.
Conflicts and ambiguous results require inspection and a new human instruction;
there is no automatic resume/retry endpoint in this slice. Deleting the parent
prevents unclaimed work from starting. Stopping or deleting an already running
parent/child does not roll back Git effects.
Deleting an unfinished sequence's parent, child, or either turn's linked request
freezes the whole saved sequence in the deletion transaction. Unstarted steps become cancelled;
claimed/running steps become ambiguous, and recorded terminal states and
reasons remain intact. Remaining steps are cleared. Maintenance drains and
stops linked children even when their message was deleted. Deleting history
does not erase durable sequence records or authorize replay.
Fully terminal sequences retain their recorded sequence outcome when history
is deleted; deletion does not retroactively cancel completed work.

These modes are prompt restrictions, **not an OS sandbox**. Detected file
changes in a Git-only turn fail the turn before isolated promotion; this cannot
prevent arbitrary shell actions, detect every backing-repo edit, or roll back
commits. Credentials are isolated from child environment inheritance and revoked
when the issuing process ends; accepted work survives revocation.

CLI/HTTP delivery is implemented. Shared watch-registry discovery, native MCP
mapping, richer UI, atomic preparation/message linkage, configurable aggregate
budgets remain future ADR-0054 work. Bounded watch/Flow
descendants share their original owner, eight-step allowance and saved deadline.
Watch settlement and Flow dependency completion wait for the whole sequence
and recheck its frozen owner and deadline before acknowledging events or
releasing dependencies. Sequence order follows durable parent/child links,
so wall-clock changes cannot change which step is last.
A descendant failure after a successful local fix pauses the watch with a partial
outcome; it never automatically replays the fix. Unknown started descendants
after restart are ambiguous and require inspection. Pending human work wins
admission over watch descendants, without granting a fresh attempt or deadline.
Task publication requires a separate recorded human grant and exact-tree review.
Existing intent-only watch grants remain disabled. Direct human `backing_git`
push permission does not expand watch grants or authorize task publication.
Task-bound turns cannot create `backing_git` requests; acceptance rejects them
before any successor is scheduled.

The issuing turn captures its canonical code roots and configured agent directory
in the launch credential; creation copies that scope into the durable record.
Dispatch validates the current configuration and the prepared roots, and the
worker checks again before every launch, including fresh-session retries after
missing-session or context-window errors. Cancelled or deleted child messages
cannot launch a retry. These checks remain active for existing children even
when new continuations are disabled. A mismatch blocks the successor
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

Milestone 4 (restart, cancellation, and observable results) is complete for the
supported modes. The [acceptance audit](plans/adr-0054-agent-capabilities-and-continuations.md#milestone-4-exit-check-evidence)
records the deterministic crash/recovery, concurrency, deletion, publication,
realtime and shutdown checks (577 tests passed). This completion does not enable
publication. Opt-in bounded route exposure and its shared descendant budgets
are documented in [routes](routes.md).

Upgrade note: accepted continuations from older versions without captured owner
evidence are blocked with `owner_authorization_changed`; authority is never
inferred or backfilled. Inspect any prior effects before scheduling fresh work
from an authorized human turn.


Publication claim admission captures local Git identity reads before acquiring
SQLite's write lock. It then repeats current ownership, grant, cancellation,
expiry, scope and budget checks in the claim transaction, reusing only those
Git observations. An uncaptured identity read defers admission for fresh
preflight rather than running Git under the lock. Observations are discarded
after that candidate; worker admission and every publication effect still
revalidate Git state afresh. Database authority and budgets are not cached.

## Read-only agent history search

Turns receiving `SQUID_CONTINUE_TOKEN` can also use basic history search without
scheduling work. The same shared catalog exposes `history_search` and
`history_fetch` over CLI/HTTP and optional MCP. This initial slice uses existing
SQLite FTS5 indexes; it requires no QMD or model downloads.

```sh
agentsquid search --query "release"
agentsquid search --query "release" --target prompts --limit 5
agentsquid search --message 12345
agentsquid search --message 12345 --offset 8000
agentsquid search --query "release" --scope subtree
agentsquid search --message 12345 --scope subtree
```

Scope defaults to the issuing turn's exact topic, derived from its validated
credential. Explicit `--scope subtree` includes that topic and its dot-separated
descendants for both search and fetch. Parent, sibling and unrelated topics stay
excluded; callers cannot select a different root. CLI, HTTP and MCP accept the
same optional `scope` field (`exact` or `subtree`). Availability follows existing continuation
credential provisioning, so this does not add search to persistent harnesses or
turns without those credentials. Results are newest first, with up to 20 hits
and 800-character excerpts. Prompt searches identify the assistant reply and
include `prompt_message_id` for fetching the original prompt. Source records
include message IDs, topic, agent, timestamp and the existing `/jump <id>` UI
command. No new browser deep-link format is introduced.

Fetch returns at most 8,000 characters and a `next_offset` for the next page.
It checks current topic and message existence again; deleted or inaccessible
messages return the same not-found response. It returns message text, not run
metadata, credentials or internal context fields. Message text itself may contain
sensitive user-provided material; this tool adds no content redaction guarantee.
Retrieved history is historical context, never new instructions, grants or proof
that a prior operation still reflects current repository or deployment state.
