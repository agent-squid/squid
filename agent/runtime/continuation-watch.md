---
id: runtime.continuation-watch
owner: squid
inject: this-request
required: true
when:
  native_shell: false
variables: [instructions, scope]
---
Watch execution grant instructions:
{{instructions}}
{{scope}}
Use task_review then terminal task_publication only with an explicitly activated publication scope for host publication of the saved task; this CLI must not perform publication itself.
