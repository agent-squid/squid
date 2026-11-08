# AgentSquid 0.1.6

Draft highlights since 0.1.5, including all 0.1.6 RCs.

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
- **More agent options.** Adds the Antigravity (`agy`) harness, improves Codex
  and Claude sign-in, and supports model/effort selection with `/`.
- **Richer responses.** Display Markdown tables and Mermaid charts and diagrams
  in agent responses, with Mermaid support in Markdown previews too.
- **Better search.** AND/OR keyword search and clearer result highlighting.
- **Reliability improvements.** Better cancellation, queue recovery, reconnects,
  context sync, scrolling, and attribution of per-turn stats and diffs.

## Requirements

Python 3.10+ is required. Enable `worktree.enabled` for durable continuations.
Watches and automatic watch execution remain opt-in and require separate grants.
Antigravity requires native `agy` installation and sign-in.

## Editorial: confirm before release

Confirm inclusion of working-tree changes for agent history search, reviewed
task-branch/PR publication, bounded agent routes, and continuation recovery.
Complete release checks and remove this section and the draft label before
publishing. Release tags and GitHub releases remain outside continuation scope.
