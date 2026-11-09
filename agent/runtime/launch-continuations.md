---
id: runtime.launch-continuations
owner: squid
inject: this-request
required: true
when: {}
variables: []
---
Squid supports one durable sequence of up to eight follow-on steps after successful completion and sync. Use `agentsquid continue --help` to schedule requested follow-on work. Split edit/commit/edit requests at Git boundaries using the ordered steps input; each step runs only after successful completion and sync. For direct human edit-and-commit/push requests, schedule a backing_git step for after sync; push requires an explicit request from the original user and is limited to the current branch. Use task_review then terminal task_publication for recorded task-publication grants; the host performs those Git/PR effects. Watch and Flow sequences cannot use backing_git. Report edits as complete and requested Git steps as queued, not completed.
