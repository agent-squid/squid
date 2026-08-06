---
status: accepted
date: 2026-08-06
---
# ADR-0036: Sandboxed `$HOME` Per Agent

## Context and Problem Statement

Every bundled harness resolves its config, plugins/skills, prompt history,
and credentials from `$HOME`-relative paths — confirmed by directly probing
each installed CLI under a throwaway `HOME`:

```
claudecode -> ~/.claude/.credentials.json, ~/.claude/settings.json
codex      -> ~/.codex/auth.json, ~/.codex/config.toml, ~/.codex/plugins, ~/.codex/skills
cursor     -> ~/.cursor/cli-config.json
opencode   -> ~/.config/opencode/, ~/.local/share/opencode/  (XDG defaults, not confirmed
              to hold the credential specifically — see Open items)
pi         -> ~/.pi   (no login command; auth is env-var only, ANTHROPIC_API_KEY /
              ANTHROPIC_OAUTH_TOKEN — nothing to copy)
```

Running `codex login status` / `cursor-agent status` / `claude auth status`
under a fake `HOME` reports fully logged-out with no stored state, and none
of the three showed any credential source outside `$HOME` (no macOS Keychain
string references found in the `cursor-agent` binary, despite `agent/creds.py`
separately reading a cursor token from Keychain — that appears to be a
squid-owned read, not something `cursor-agent` itself depends on).

This means overriding `$HOME` for a subprocess is a real, working sandbox
boundary today, with no code changes needed to any harness — it isolates
credentials *and* globally-installed plugins/skills/MCP servers/settings
together, since they all live under the same directory tree.

The problem: those two things — credentials and global customization — are
not always wanted together. A user may want to run an agent with none of
their global plugins/skills influencing behavior (for reproducibility, or to
rule out a global config as the cause of some behavior), while still being
logged in without repeating an OAuth/device-code flow. A single "swap
`$HOME`" toggle can't express that distinction on its own.

## Decision Outcome

Add an opt-in **sandboxed `$HOME`** mode, scoped per `(topic, agent)`, with
two user-selectable starting states:

1. **Fully blank.** Squid creates an empty directory and points the
   harness's `HOME` at it. The harness starts with no plugins, no skills, no
   settings, and no credentials — it must be logged in from scratch, via the
   auth-session flow from [ADR-0035](0035-cli-auth-sessions-via-scoped-pty.md)
   run with its own `HOME` env pointed at this same sandbox directory (so the
   login lands in the sandbox, not the user's real `$HOME`).
2. **Blank, but authenticated.** Same empty directory, but Squid performs a
   one-time, explicit copy of a fixed, harness-specific credential-file
   allowlist from the real `$HOME` into the sandbox before first use. Every
   other `$HOME`-relative path (plugins, skills, prompt history, MCP server
   config) starts empty, only the credential is carried over.

Both states are opt-in per `(topic, agent)`; the default remains today's
behavior — no `HOME` override, full inheritance of the user's real
environment.

### This is a separate axis from ADR-0025

Per-turn worktree isolation ([ADR-0025](0025-per-turn-worktree-isolation.md))
isolates *code* — a fresh, disposable directory per turn, discarded after
sync. This ADR isolates *environment/identity* — a persistent directory per
`(topic, agent)`, not per turn, because re-running OAuth or reinstalling
plugins on every single turn would defeat the point. The two compose freely:
a sandboxed-`HOME` agent still gets normal per-turn worktree isolation for
its code roots.

### Credential copy: explicit, one-directional, harness-specific

Copying is a deliberate user action (e.g. a "provision credentials" button
at sandbox-creation time, or a later explicit "resync credentials" action),
never an automatic background sync. Two reasons:

- **Real → sandbox only.** A sandbox is meant to isolate; silently syncing
  the other direction would let something run inside the sandbox mutate the
  user's real, non-sandboxed credential.
- **No continuous sync.** Copying happens once (or on explicit request), not
  on every turn — otherwise a sandbox's `HOME` is never actually stable,
  which breaks the "reproducible, unaffected by global config" motivation
  as much as full inheritance would.

The allowlist is a fixed table, mirroring the login-command allowlist in
ADR-0035, keyed by harness — never inferred from scanning `$HOME`:

```
claudecode -> ~/.claude/.credentials.json
codex      -> ~/.codex/auth.json
cursor     -> ~/.cursor/cli-config.json   (holds more than just auth; copying
              the whole file is the current plan pending confirmation of a
              narrower credential-only key — see Open items)
opencode   -> not yet located (see Open items)
pi         -> nothing to copy; env-var auth is set directly per sandbox
```

### `$HOME` is not the only variable that needs overriding

`opencode`'s config/data resolve through XDG defaults
(`~/.config/opencode`, `~/.local/share/opencode`), which fall back to
`$HOME`-relative paths but can be independently overridden by
`XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` / `XDG_CACHE_HOME` in
the user's real environment. A sandbox spawn must clear or override those
explicitly, not just `HOME` — otherwise a user with any of those set
globally would leak real config into an intended-blank sandbox for that one
harness even though `HOME` itself was overridden correctly.

### Storage and spawn mechanics

Sandbox directories live under Squid's own state directory, parallel to the
existing worktree path convention (`_WORKTREES_HOME` in `agent/worktree.py`):

```
~/.squid/sandbox-homes/<topic_hash>/<agent>/
```

Persistent for the lifetime of the `(topic, agent)` sandbox, not cleaned up
per turn. The runner layer (`agent/runners.py`, wherever the harness
subprocess env is constructed) gains an optional `home_override` that, when
set, replaces `HOME` and clears the XDG variables above in the spawned
process's environment — everything else about turn execution (worktree
setup, stats parsing, protocol selection) is unaffected.

## Consequences

- Good: a working, low-effort sandbox boundary — no per-harness code
  changes required, since every bundled CLI already resolves its state from
  `$HOME` (or XDG vars falling back to it).
- Good: users can rule out global plugins/skills/settings as a source of
  unexpected agent behavior, or deliberately run an agent without them.
- Good: "blank but authenticated" avoids repeating an OAuth/device-code flow
  just to get a clean environment.
- Good: composes with ADR-0025 (code isolation) and ADR-0035 (the auth
  session mechanism used to log in inside a fully-blank sandbox) without
  either needing to change.
- Neutral: sandbox `HOME` is persistent per `(topic, agent)`, not per turn —
  a different lifecycle than worktrees, and a new directory tree Squid must
  track and eventually let users delete/reset.
- Bad: the credential allowlist is per-harness, hand-maintained, and only
  partially verified (see Open items) — a wrong or stale path either fails
  to copy a credential (blank sandbox stays unauthenticated) or, worse,
  copies more than intended if a file turns out to hold non-credential
  secrets alongside the token.
- Bad: any harness that resolves state through a mechanism other than
  `HOME`/XDG (undiscovered Keychain use, an absolute path baked into a
  installer, etc.) would silently leak into every sandbox for that harness.
  Only `cursor-agent`'s binary was checked for Keychain string references;
  the others were not.

## Open items before implementation

- `cursor`'s `~/.cursor/cli-config.json` has not been inspected for whether
  it mixes credentials with non-credential state that shouldn't be copied
  into a sandbox wholesale.
- `opencode`'s actual credential file has not been located in this pass —
  `~/.config/opencode/` and `~/.local/share/opencode/` were confirmed as
  XDG-default paths in use, but not which file inside them (if any, versus
  e.g. `opencode.db`) holds the provider credential.
- Only `cursor-agent`'s binary was checked for hardcoded Keychain access;
  `claude`, `codex`, and `opencode` were not, so a non-`HOME` credential
  source for those is unverified, not ruled out.
- No decision yet on sandbox lifecycle UI: how a user creates, resyncs
  credentials into, or deletes a sandbox `(topic, agent)` `HOME`.
