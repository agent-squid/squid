# Reviewed task publication

`task_publication` is a host-executed continuation step. It operates only on a
saved `squid/task/<id>` branch in its persistent task worktree. `backing_git`
retains its existing current-backing-branch contract. Editing and review steps
still run in branchless directories; no agent shell receives permission to
commit, push or call GitHub for task publication.

Publication requires an authenticated human grant naming one repository, remote
and base branch, with `push: true` and `pull_request: true`. The saved scope binds
canonical code roots, configured agent, Git identity, fetch/push URLs, task branch
and pinned base. Both transports must identify the same repository. The original
human request digest and host-control audit are retained. Agent/issue text and
turn credentials cannot create or enlarge this grant.

For a direct human request, HTTP `/chat` accepts `task_workspace` or `task_id`,
`publication`, and `publication_expires_at` (Unix seconds, at most one day ahead).
Publication-bearing requests require the host-control bearer secret used by
human watch controls; a session or turn credential alone is insufficient. The
server prepares the task and records authority before starting the agent. The
response's `X-Squid-Task-Id` identifies the task. Realtime requests cannot grant
publication; use the authenticated HTTP path. An existing task/request can also
receive a grant through
`POST /task-workspaces/{task_id}/publication-grant` with
`request_id`, `publication`, and `expires_at`. Grants can be revoked through the
corresponding `/publication-revoke` endpoint.

```json
{
  "repository": "/absolute/configured/repository",
  "remote": "origin",
  "base_branch": "main",
  "push": true,
  "pull_request": true
}
```

Watch execution grants must additionally include `enable_publication: true`
through the authenticated human grant path. Omitting it records intent only.
Old intent-only grants migrate with activation off; enabling publication requires
a fresh human grant. Local-only grants retain their existing permissions.

The editing turn can declare this terminal sequence through `agentsquid continue`:

```json
{
  "request_key": "review-and-publish",
  "steps": [
    {"mode": "task_review", "prompt": "Review this exact fix without editing. Report success only if ready."},
    {"mode": "task_publication", "prompt": "Publish the reviewed task."}
  ]
}
```

`task_review` records the task's exact head and tree before a separate review
turn. The reviewer must report `{"success":true}` through `continue result`.
Edits, failed checks, changed authority, tree drift or failed sync invalidate the
review. Review edits remain quarantined in the isolated directory. Subsequent
edits require another review. A publication step must be last; in a Flow it can
only be declared by the final member after all other members complete.

The host creates a commit object from the reviewed tree, persists intent and its
SHA, then compares and updates only the saved task ref. It preserves the human
checkout and refuses unknown staged/index changes. It pushes one explicit task
ref without force or tags. The GitHub adapter supports same-repository PRs on
`github.com` via `gh api`; forks and other hosting services are unsupported.
Existing PRs are identified by the saved branch/base/task marker and retained PR
number. Branch pushes update their diffs while preserving human descriptions.
Closed, merged, missing or ambiguous PR identities block further publication.
No merge, release, deployment, branch switching or unrelated ref push is offered.

## Partial results and recovery

Intent records include task/destination identity, original owner, base, parent,
reviewed tree, intended commit, prior remote head, phase and PR number/URL. Intent
is durable before local ref promotion, push and PR creation. Commit success,
confirmed push, unknown push outcome and unknown PR creation are distinct;
uncertainty is not reported as rollback or success. Confirmed reconciliation
appends a success event while retaining earlier failure evidence; repeated
observations do not duplicate terminal events. A publication failure stops
the sequence and leaves the watch paused/partial, so the local fix is not rerun.

Startup recovery only observes remote refs and existing PRs. It records completed
effects when their exact identity is confirmed; it never repeats an uncertain
push or PR creation. Unexpected remote heads block. Concurrent publication and
reconciliation for the same task share the existing task/worktree lock.
Recovery also writes the observed partial outcome to the linked message when
publication remains incomplete, so a confirmed commit or push survives in the
transcript. An ambiguous continuation remains ambiguous; this report does not
authorize further writes or resume the sequence.

Host-authenticated `GET /task-publications/{id}` shows intent and outcome.
`POST /task-publications/{id}/reconcile` defaults to observing only. With
`resume: true`, it can perform a *known unperformed* phase under valid authority.
If the remote still matches the saved pre-push state, or a successful PR lookup
finds no matching PR, reconciliation marks that phase retryable. Only an explicit
`resume: true` under valid authority retries it; startup recovery never does.
Failed lookups and unexpected remote heads remain blocked. Transport failures
return HTTP 503 (`publication_transport_unavailable`) while preserving the saved
intent for a later explicit reconciliation request. `authority_parent_id`
can name a fresh human turn bound to the same task with a fresh publication grant;
this appends authority without changing the original destination or commit.
Reconciliation never revives an expired/cancelled watch grant or a later sequence
step. Unknown effects and unresolved artifacts remain available for inspection.

Tests use disposable local bare remotes, the normal worker/scheduler and a fake
GitHub service. No live-model or external GitHub publication is implied by those
checks; live proof still requires an explicitly authorized test repository.

Successful reconciliation refreshes the saved publication message for partial
outcomes as well as completion. Repeated read-only reconciliation does not
repeat external effects or duplicate an unchanged terminal event.
