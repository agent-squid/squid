---
status: accepted
date: 2026-05-27
updated: 2026-06-18
---
# ADR-0012: Context Sync to /tmp/<user>/squid Instead of Symlink

## Context and Problem Statement

Subprocess CLIs (claude, codex) need a clean working directory that does not
load any CLAUDE.md or MCP configuration from the user's personal scope. The
path is `/tmp/<user>/squid` (where `<user>` is the OS username), populated
from `~/.squid/context/`.

The original approach created `/tmp/squid` as a symlink to `squid/context/`
via `start.sh`. This broke because Claude Code resolves symlinks to their real
path before computing the project hash and loading config files. A symlink
pointing to `~/Work/squid/context/` caused the CLI to derive its project root
from `~/Work/squid/`, which loads `~/CLAUDE.md` (the clawdbot personal agent
config) and triggers Gmail/Calendar MCP connections — exactly the contamination
the isolation was meant to prevent.

The path was later changed from the shared `/tmp/squid` to per-user
`/tmp/<user>/squid` to avoid permission conflicts when multiple OS users run
squid on the same machine (e.g. a test user account alongside the main account).

The context source was moved from `<install>/context/` to `~/.squid/context/`
so user-curated context persists across tarball installs/updates.

## Considered Options

1. **Symlink** — simple, always up-to-date; broken by real-path resolution
2. **Sync on daemon restart only** — cheap but stale if context/ changes mid-run
3. **Sync on every prompt** — always fresh; spawn cost (~20ms) on every request
4. **Sync on restart + stat-check per prompt** — no-op when nothing changed;
   only pays rsync cost when content actually changes

## Decision Outcome

**Option 4.** `/tmp/<user>/squid` is a real directory. Two sync points:

- **Daemon startup** (`sync_now()` in `context_sync.py`): blocking rsync via
  `subprocess.run`. Runs once during module init so the directory is ready
  before the first request. Also handles `/tmp` being wiped on reboot.

- **Per-prompt** (`maybe_sync()` in `context_sync.py`): async stat-check that
  walks `~/.squid/context/` to find the max mtime across all files (including
  nested `.claude/` plugin dirs). If unchanged since last sync, returns
  immediately (~1ms). If changed, runs an async rsync with `--delete` so
  removals propagate.

`start.sh` creates `/tmp/<user>/squid` with `mkdir -p` as a lightweight guard
in case the server is restarted without running `start.sh`.

On first run, `start.sh` seeds `~/.squid/context/` from the install dir's
`context/` so users have a starting point. Subsequent edits to
`~/.squid/context/` are user-owned and survive tarball updates.

## Consequences

- Good: Claude Code never sees a symlink; uses the tmp path literally; no
  CLAUDE.md bleed from the personal scope
- Good: context changes (new CLAUDE.md, `.claude/` plugins, etc.) are picked
  up within one prompt with no daemon restart required
- Good: zero overhead on prompts when context is unchanged
- Good: per-user tmp path avoids cross-user permission conflicts on shared machines
- Good: `~/.squid/context/` persists across squid installs/updates
- Bad: `_tree_mtime` walks the full context tree on every prompt; acceptable
  for small context directories but would slow down if context/ grows large
- Bad: rsync must be available on the host (standard on macOS/Linux)
- Note: the mtime check uses wall-clock time; clock skew (e.g. from volume
  mounts) could cause missed syncs — a known edge case, not addressed here
