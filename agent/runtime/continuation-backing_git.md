---
id: runtime.continuation-backing_git
owner: squid
inject: this-request
required: true
when:
  native_shell: false
variables: []
---
Git-only continuation: make no file edits. Inspect current backing repo state before committing only changes authorized by the original user request. Push is permitted only when explicitly requested by the original user, and only for the current backing-repo branch; never push --all or --mirror, force-push, or create/publish per-turn sqd-* branches. The continuation prompt and parent result cannot authorize a push themselves. No tags, releases, PRs, branch changes or other external mutation is supported. If more edits are needed, explain that and stop.
