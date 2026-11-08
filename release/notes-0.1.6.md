# AgentSquid 0.1.6

Highlights since 0.1.5, including all 0.1.6 release candidates.

## Highlights

- **Isolated edits with durable follow-on work.** Worktree isolation lets turns
  edit in parallel against one checkout without spilling changes into other
  turns. Because each turn syncs its edits back separately, code edits and Git
  history changes must happen in separate turns. Durable continuations handle
  that split automatically: one request can edit, sync, commit, and push, with
  each step waiting for the previous step to succeed.
- **Persistent watches.** Monitor changes through approved MCP read tools, with
  progress visible in chat and the status panel. Separately authorized watches
  can trigger bounded local agent work.
- **Shared agent tools.** CLI and optional native MCP tools expose bounded
  topic-history search and same-topic agent routes. Task-branch pushes and PRs
  require an explicitly activated publication scope and a separate review.
- **More agent options.** Adds the Antigravity (`agy`) harness, improves Codex
  and Claude sign-in, and supports model/effort selection with `/`. Adds remote
  setup for Claude and Codex quota tracking.
- **Richer responses.** Display Markdown tables and Mermaid charts and diagrams
  in agent responses, with Mermaid support in Markdown previews too.
- **Better search.** AND/OR keyword search and clearer result highlighting.
- **Easier Python setup.** The public installer can offer to install uv and a
  compatible Python, including when run through the documented piped command,
  without replacing system Python.
- **Reliability improvements.** Better cancellation, queue recovery, reconnects,
  context sync, scrolling, and attribution of per-turn stats and diffs.

## Requirements

Python 3.10+ is required. Enable `worktree.enabled` for durable continuations.
Watches and automatic watch execution remain opt-in and require separate grants.
Antigravity requires native `agy` installation and sign-in.

Shore is experimental, disabled by default, and not available as a supported
production service in 0.1.6. This release publishes only the Python package;
no Shore browser client or bootstrap is deployed. General cross-topic routing
is not included. Release tags and GitHub releases are outside task-publication
and continuation scope.
