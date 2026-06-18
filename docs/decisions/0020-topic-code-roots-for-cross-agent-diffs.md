---
status: accepted
date: 2026-06-11
---
# ADR-0020: Topic Code Roots for Cross-Agent Diffs

## Context and Problem Statement

Squid supports multiple coding agents and backends. A topic may run through
Claude, Codex, or another CLI, and those CLIs may launch from backend-specific
working directories. Squid still needs to show one coherent code diff after an
agent turn.

Using the agent process `cwd` as the diff root is unreliable:

- some agent working directories are intentionally synthetic or isolated, such
  as `/tmp/<user>/squid`, to control backend-specific instruction loading;
- different agents for the same topic may have different configured `cwd`
  values;
- the real codebase may live somewhere else, such as
  `/Users/haebin/Work/squid`;
- a user can explicitly decide that a topic should not use code diff tracking.

Squid therefore needs a topic-level, backend-independent source of truth for
which repositories should be snapshotted before a coding-agent run and diffed
after it completes.

## Considered Options

1. Diff the repository containing the backend CLI process `cwd`.
2. Diff every repository visible from the Squid server process.
3. Store code roots in each agent config.
4. Store topic-level code roots in topic memory frontmatter.

## Decision Outcome

**Option 4.** Topic memory may declare structured Squid frontmatter:

```yaml
squid:
  code_roots:
    - /absolute/path/to/repo
```

Squid treats these paths as the topic's primary codebase roots for code diff
generation across all coding agents:

- before an agent turn, Squid snapshots Git state for valid configured roots;
- after the turn, Squid computes and displays diffs from those same roots;
- the agent receives the real code-root paths in prompt context and runs from
  the first valid code root when one is configured;
- the behavior is tied to the topic, not to the selected backend or agent;
- if multiple roots are present, all valid roots are eligible for tracking;
- the first root may be used as the preferred codebase path for ordering,
  prompts, or UI defaults.

Topic memory may also declare:

```yaml
squid:
  code_roots_skipped: true
```

This records that the user declined to associate code roots with the topic.
When this flag is present and no valid `code_roots` are configured, Squid does
not repeatedly ask and does not infer a fallback diff root from the current
process working directory.

If both fields are present, valid `code_roots` win over
`code_roots_skipped`. This lets a later manual edit re-enable diff tracking
without requiring cleanup of the skipped flag.

## Runtime Behavior

Before a coding-agent turn, Squid reads topic memory frontmatter independently
from whether the user chose to inject the full memory markdown into model
context.

- If `squid.code_roots` contains valid paths, Squid uses those roots for Git
  snapshots and post-run diff generation.
- If `squid.code_roots_skipped: true` is present without valid roots, Squid
  disables topic-level code diff tracking for that turn.
- If neither value is present, Squid asks the user once for paths or an explicit
  skip decision, then writes only the necessary frontmatter.

Git-generated diffs are authoritative when they exist. If Squid does not have a
`GitDiff` for a turn because code roots were skipped, missing, invalid, or Git
tracking could not initialize, the UI may still present a normalized changed
files summary from agent-reported edit events when available. Examples include
Claude `Edit`, `Write`, and `MultiEdit` tool events, or equivalent modified-file
events from other backends. This fallback should use the same changed-files
presentation shape where practical, but it must be labeled as agent-reported
rather than post-run Git truth because it may omit shell-generated edits or
failed tool calls.

The generated memory file remains minimal. Squid does not add guide prose below
frontmatter by default; explanatory text belongs in the topic memory editor/help
UI.

## Relationship to Agent `cwd`

Topic code roots do not replace the selected agent's configured `cwd`.
The agent `cwd` continues to control subprocess launch context, model/backend
configuration discovery, and resumable session identity. Code roots are a
separate set of real version-controlled paths used for prompt context and Git
diff tracking.

In direct watch mode:

- new sessions launch from the agent or stored session `cwd`;
- existing sessions store and resume that same agent/session `cwd`;
- the prompt keeps the real code-root paths instead of remapping them to a
  sandbox or worktree;
- Git diffs compare direct snapshots of the real watched roots before and after
  the turn.

When no code roots are configured, Squid falls back to the selected agent or
stored session `cwd` and disables topic-level Git diff tracking unless the user
later configures roots.

## Consequences

- Good: code diffs are stable across different coding agents for the same
  topic.
- Good: Squid edits and diffs the real repository directly, avoiding worktree
  path remapping and multi-root sandbox coordination.
- Good: users can skip diff tracking once without being prompted repeatedly.
- Good: when Git tracking is unavailable, Squid can still show the agent's own
  accurate edit-file list instead of hiding changed-file context entirely.
- Good: topic memory stays the single durable place for topic-specific codebase
  metadata.
- Good: model context injection and runtime diff tracking are decoupled.
- Bad: Squid must preserve three states: no decision yet, configured roots, and
  explicitly skipped roots.
- Bad: direct watch diffs include any changes observed in those roots during
  the turn, including concurrent user or process edits.
- Bad: agent-reported edit lists are not a full filesystem diff and need honest
  labeling when used as a fallback.
