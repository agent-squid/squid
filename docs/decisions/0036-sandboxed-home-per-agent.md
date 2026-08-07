---
status: accepted
date: 2026-08-06
updated: 2026-08-06
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
rule out a global config as the cause of some behavior), without repeating
an OAuth/device-code flow just to get there. A fully-blank, logged-out
sandbox isn't a useful third state on its own: every harness requires login
before it does anything, so "blank and logged out" just forces the
auth-session flow from [ADR-0035](0035-cli-auth-sessions-via-scoped-pty.md)
before the sandbox is usable, with no advantage over "blank and already
logged in." Two states cover the real need.

## Decision Outcome

Add an opt-in **sandboxed `$HOME`** mode, scoped per **agent** — a property
of the agent preset itself, not per-topic, consistent with how
`harness`/`provider`/`model`/`cwd` are already defined once per agent
(`agents` table, keyed by `name`) and reused across every topic that
addresses it. Presented as a two-item selector next to the agent's other
setup fields in the Agents view (e.g. a home icon with a dropdown:
**User Home** / **Blank Home**), defaulting to **User Home**:

1. **User Home** (default). No `HOME` override — today's behavior, full
   inheritance of the user's real environment, plugins, skills, and
   credentials.
2. **Blank Home.** Squid creates an empty directory and points the harness's
   `HOME` at it. Every `$HOME`-relative path (plugins, skills, prompt
   history, MCP server config) starts empty except the credential file,
   which Squid symlinks in from the real `$HOME` (see below) so the agent is
   authenticated immediately. No separate "blank, logged out" state is
   offered — see Context above for why that third state isn't useful.

### This is a separate axis from ADR-0025

Per-turn worktree isolation ([ADR-0025](0025-per-turn-worktree-isolation.md))
isolates *code* — a fresh, disposable directory per turn, discarded after
sync. This ADR isolates *environment/identity* — a persistent directory per
agent, not per turn, because re-running OAuth or reinstalling plugins on
every single turn would defeat the point. The two compose freely: a
sandboxed-`HOME` agent still gets normal per-turn worktree isolation for its
code roots, in every topic it's used from.

### Credential linking: symlink, not copy

Squid symlinks the credential file from the real `$HOME` into the sandbox
`HOME` (`ln -s <real path> <sandbox path>`) rather than copying it, so a
token refresh on either side is picked up by the other automatically — no
explicit "resync" action needed while the symlink holds.

This has two consequences a plain copy wouldn't, both worth stating
explicitly rather than assuming symlink-in-place is free:

- **The link is fragile against atomic-write refreshes.** `rename(2)` does
  not follow a symlink at the destination path — it replaces the symlink
  itself. If a harness refreshes its credential by writing a temp file and
  `rename()`-ing it over the target (a common pattern to avoid partial
  writes), the first refresh — inside the sandbox *or* in the real
  environment — silently replaces the symlink with an ordinary file. From
  that point the two copies are permanently decoupled with no notification
  to Squid or the user. Whether each bundled harness refreshes in place
  (link survives) or via temp+rename (link silently breaks) is unverified —
  see Open items.
- **The link is bidirectional, not real→sandbox-only.** Until it breaks (per
  above), any in-place write inside the sandbox — e.g. an explicit logout
  that rewrites the file rather than replacing it — mutates the user's real
  credential too. This is an accepted tradeoff now that "blank, logged out"
  isn't offered as a state: the credential file specifically is not
  isolated by Blank Home, only the surrounding plugins/skills/settings/
  history are. Worth surfacing in the UI (e.g. a note on the Blank Home
  option) so "sandboxed" isn't read as "credential is isolated too."

The symlink target is a fixed table, mirroring the login-command allowlist
in ADR-0035, keyed by harness — never inferred from scanning `$HOME`:

```
claudecode -> ~/.claude/.credentials.json
codex      -> ~/.codex/auth.json
cursor     -> ~/.cursor/cli-config.json   (holds more than just auth; linking
              the whole file is the current plan pending confirmation of a
              narrower credential-only key — see Open items)
opencode   -> not yet located (see Open items)
pi         -> nothing to link; env-var auth is set directly per sandbox
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

Sandbox directories live under `/tmp/<user>/`, not under `~/.squid/` —
deliberately *not* parallel to the worktree path convention
(`_WORKTREES_HOME` = `~/.squid/worktrees/` in `agent/worktree.py`). A git
worktree is safe to nest under the real `$HOME` because it's itself a git
repo root; upward directory-tree discovery for project config stops at the
nearest `.git` boundary. A sandbox `HOME` is not a git repo — nothing stops
an upward walk, so a path like `~/.squid/sandbox-homes/<agent>/` would let
discovery keep walking `~/.squid/` → `~/`, landing on the real `~/CLAUDE.md`
and defeating the isolation. This is exactly the failure mode
[ADR-0012](0012-context-sync-tmp-squid.md) already hit and fixed by moving
its own tmp working directory off a path under the real `$HOME`; sandbox
`HOME` follows the same convention:

```
/tmp/<user>/squid-homes/<agent>/
```

(`SQUID_HOMES` in `agent/config.py`, per-user for the same reason
`SQUID_HOME` is — avoids permission conflicts when multiple OS users run
squid on the same machine.) A sibling of `SQUID_HOME`
(`/tmp/<user>/squid`), not nested inside it — `context_sync.py` rsyncs
`~/.squid/context/` into `SQUID_HOME` with `--delete` on every change, which
would silently wipe anything else placed inside that directory.

Nothing is seeded into a sandbox home today beyond the credential symlink —
it starts genuinely empty. If a future need calls for deliberately injecting
default content (e.g. a baseline `CLAUDE.md` or MCP config every Blank Home
agent should start with), the pattern to follow is the same one
`context_sync.py` already uses for `SQUID_HOME`: a user-owned source under
`~/.squid/` (e.g. `~/.squid/context/`) synced by Squid into the tmp
directory, never symlinked directly — a directory symlink back to something
under the real `$HOME` reintroduces the exact upward-discovery leak this
section exists to avoid.
One consequence worth stating explicitly: unlike `~/.squid/`, `/tmp` is not
guaranteed to survive a reboot (tmpfs on Linux, periodic cleanup on macOS).
A reboot can wipe an agent's Blank Home sandbox — its plugins/skills/
settings/history — the same way it already wipes `SQUID_HOME`, ADR-0012
accepted this for the same reason. Recovery is automatic but not free: the
sandbox directory and credential symlink are recreated lazily on next use
(`ensure_sandbox_home()`'s `mkdir(parents=True, exist_ok=True)` plus the
existing reconciliation logic), but any plugins/skills installed inside the
now-gone sandbox are not — the agent comes back up authenticated but with a
fresh empty environment again, same as first provisioning.

Persistent across turns and topics for the lifetime of the agent's sandbox
(module reboot aside) — not cleaned up per turn, and not scoped to any one
topic, since the same sandbox directory is reused for every topic that runs
this agent. The runner layer (`agent/runners.py`, wherever the harness
subprocess env is constructed) gains an optional `home_override` that, when
set, replaces `HOME` and clears the XDG variables above in the spawned
process's environment — everything else about turn execution (worktree
setup, stats parsing, protocol selection) is unaffected.

The credential symlink (see above) is created once at sandbox-creation
time, then reconciled at the start of every turn, before the harness
subprocess spawns:

1. Check whether the sandbox credential path is still a symlink pointing at
   the real path.
2. If it isn't (a temp+rename refresh severed it — see "Credential
   linking" above), the sandbox's file is the freshest token, since it was
   written most recently. Copy its contents back onto the real path first.
3. Delete the sandbox file and recreate the symlink.

This can't prevent the momentary fork a rename-based refresh causes within
a turn — that's kernel-level `rename(2)` behavior, not something a link
choice or Squid code can avoid — but it keeps there being one real source
of truth *between* turns, self-healing rather than silently drifting
forever. It also doubles as the mechanism for a manual "repair sandbox"
action, which just runs the same three steps on demand.

## Consequences

- Good: a working, low-effort sandbox boundary — no per-harness code
  changes required, since every bundled CLI already resolves its state from
  `$HOME` (or XDG vars falling back to it).
- Good: users can rule out global plugins/skills/settings as a source of
  unexpected agent behavior, or deliberately run an agent without them.
- Good: Blank Home avoids repeating an OAuth/device-code flow, and — while
  the symlink holds — stays authenticated across token refreshes
  automatically, with no explicit resync action needed.
- Good: composes with ADR-0025 (code isolation) and ADR-0035 (the auth
  session mechanism, no longer needed for Blank Home now that a logged-out
  state isn't offered, but still relevant for initial User Home login)
  without either needing to change.
- Neutral: sandbox `HOME` is persistent per agent, not per turn — a
  different lifecycle than worktrees, and a new directory tree Squid must
  track and eventually let users delete/reset. Also means Blank Home is
  all-or-nothing per agent across every topic that uses it — there's no
  per-topic override once an agent is set to Blank Home.
- Bad: the credential symlink table is per-harness, hand-maintained, and
  only partially verified (see Open items) — a wrong or stale path either
  fails to link a credential (Blank Home stays unauthenticated) or, worse,
  links a file that mixes in non-credential secrets alongside the token.
- Bad: the credential symlink is bidirectional and can silently break on
  the first atomic-write refresh (see "Credential linking" above) — Blank
  Home is not a full isolation guarantee for the credential file the way it
  is for plugins/skills/settings.
- Bad: sandbox homes live under `/tmp`, not `~/.squid/` (see "Storage and
  spawn mechanics" for why) — a reboot can wipe an agent's accumulated
  plugin/skill/settings state, same tradeoff ADR-0012 already accepted for
  `SQUID_HOME`. The credential symlink and directory are recreated
  automatically on next use; installed plugins/skills are not.
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
- Whether each bundled harness refreshes its credential file in place
  (symlink survives) or via temp-file + `rename()` (symlink silently
  breaks) is unverified for `claude`, `codex`, `cursor`, and `opencode` —
  this determines how durable Blank Home's "stays in sync" property
  actually is in practice.
- The two-state selector itself is now built: a `location_home`/
  `location_away` Material Symbol + `<select>` (User Home / Blank Home) per
  row in the Agents view (`ui/app.js` `loadAgents()`, `PUT
  /config/agents/{name}/home-mode`). Toggling it reuses the exact
  confirm-then-clear flow that already exists for changing an agent's
  harness/model/cwd (`confirmAgentSessionClear` + `clear_agent_sessions`) —
  changing `home_mode` moves where a resumable session's transcript would be
  looked up, orphaning every session stored for this agent across every
  topic the same way those other attributes already do, so it's treated as
  the same class of change. Still open: no visible indication that a
  per-turn symlink reconciliation happened (see "Storage and spawn
  mechanics"), and no way yet to delete a sandboxed agent's `HOME` directory
  (currently manual: `rm -rf` under `SQUID_HOMES`).
- The per-turn reconciliation step assumes the sandbox-side file is always
  the freshest when the link has broken (step 2 above copies sandbox →
  real). This holds for the common case — a refresh happened during the
  last turn — but hasn't been checked against every harness's refresh
  behavior; if a harness ever writes a stale or partial credential during
  the temp+rename window in a way that outlives the write, this step could
  propagate a bad token onto the real path.
