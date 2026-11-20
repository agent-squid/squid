---
id: runtime.worktree
owner: squid
inject: every-turn
required: true
when:
  worktree_isolated: true
  has_code_roots: true
  native_shell: false
variables:
- backing_repos
---
{{backing_repos}}Isolated Squid turn dir: edit files belonging to the backing repos only in their provided code roots; no `.git`. Do not edit backing repo files directly or create another checkout, worktree, or branch for those changes. Authorized changes to unrelated files outside the backing repos may be made directly. Squid syncs isolated edits back to the backing repos after the response completes. For turns with file edits, do not run/suggest git commit, push, pull, branch, tag, PR, publish, or checkout switching. For git-only turns with no file edits, run requested git status, commit, push, pull, branch, tag, or PR commands only in the backing repos. When pushing, commit backing-repo changes if needed, then push only the current branch; never use `git push --all` or `git push --mirror`, and never create or publish per-turn `sqd-*` branches.

Dependency directories may link to shared backing environments: run tools there, but schedule requested installs/syncs using host-executed `dependency_sync` after source sync (`agentsquid continue --help`). Edit manifests/lockfiles here; report maintenance as queued, and restart only after it succeeds.
