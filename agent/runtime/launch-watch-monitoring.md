---
id: runtime.launch-watch-monitoring
owner: squid
inject: this-request
required: true
when: {}
variables: []
---
Squid shared tools: `agentsquid tools --help`; `agentsquid watch --help`. Watch monitoring only; no automatic watch agent work or watch publication.

For requests to monitor across turns, discover the configured read tools, create a durable watch proposal, and report its ID and any operator grant or activation still needed. Squid handles polling after activation. End the setup turn once the watch status and next steps are known.
