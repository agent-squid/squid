---
id: runtime.continuation-dependency_sync
owner: squid
inject: this-request
required: true
when:
  native_shell: false
variables: []
---
Host-executed dependency maintenance after successful parent completion and source sync. The host runs the selected fixed package-manager operation in the recorded backing repository and saves its exit status and bounded output. This step launches no model turn and authorizes no source edits, Git operations or restart. A failed or interrupted command stops advancement; startup never retries uncertain work.
