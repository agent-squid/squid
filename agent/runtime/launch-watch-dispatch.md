---
id: runtime.launch-watch-dispatch
owner: squid
inject: this-request
required: true
when: {}
variables: []
---
Squid shared tools: `agentsquid tools --help`; `agentsquid watch --help`. Local-work watch turns require recorded execution grants; task publication additionally requires an explicitly activated publication scope and a separate task_review.

For requests to monitor across turns, discover the configured read tools, create a durable watch proposal, and report its ID and any operator grant or activation still needed. Squid polls the source and starts a fresh turn for actionable events when authorized. End the setup turn once the watch status and next steps are known.

If the user asks the watch to contact a reviewer or take another external action, include the exact action and destination in the human execution-grant instructions using `external_actions`. For PR re-review, identify the PR and reviewer and specify that the fixes must already be on the remote PR head. Report whether publication and re-review are actually configured.
