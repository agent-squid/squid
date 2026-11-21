---
id: runtime.launch-watch-proposals
owner: squid
inject: this-request
required: true
when: {}
variables: []
---
Squid shared tools: `agentsquid tools --help`; `agentsquid watch --help`. Watch registration currently saves proposals only; persistent monitoring is not enabled.

For requests to monitor across turns, report that the Squid polling worker is unavailable and give the operator the proposal ID if one was saved. End the setup turn once the watch status and next steps are known.
