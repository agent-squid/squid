---
id: runtime.continuation-task_review
owner: squid
inject: this-request
required: true
when:
  native_shell: false
variables: []
---
Perform a read-only review of the saved task. Do not edit files, commit or mutate external services. Report success only if this exact code is ready for publication.
