# ~/.squid/context — what this directory is for

Everything under here is context you control. It exists to make context inheritance
explicit instead of accidental: if a file is in this tree, it's because you put it
here (or it shipped as a seed default), and it will show up for agents. If it isn't
here, it won't.

## How it reaches an agent

This directory is **not** read directly by harness CLIs (Claude Code, Codex, etc.).
It's mirrored one-way into a per-user runtime directory, `/tmp/<user>/squid`
(e.g. `/tmp/haebin/squid`), which is the actual `cwd` squid launches agent
processes in:

```
~/.squid/context/  --rsync (one-way, mirror)-->  /tmp/<user>/squid/
```

- The sync runs once at squid server startup, and again before every prompt is
  dispatched (a cheap mtime check — it only re-syncs if something under
  `~/.squid/context/` actually changed).
- It's a real rsync copy, not a symlink. Claude Code in particular resolves
  symlinks to their real path before computing its project identity and loading
  personal `~/CLAUDE.md`/MCP scope — a symlink back into this repo's checkout
  would leak that scope into every squid session. A separate, synced directory
  keeps squid sessions on their own project identity.
- Because it's a mirror, **edit files here, not in `/tmp/<user>/squid/`** — that
  side gets overwritten (with `--delete`) on the next sync.

See `docs/decisions/0012-context-sync-tmp-squid.md` for the full history/rationale.

## What this does *not* control

Squid only mirrors what's in this directory — it does not, and structurally
cannot, strip a harness's own user-scope personalization. Each CLI still applies
its own personal layer on top, independent of anything here:

- Claude Code still reads its own `~/.claude/` (skills, personal memory, MCP
  server config, etc.) as installed on the machine.
- Any other harness's user-level install/config behaves the same way.

In other words: this directory gives you deliberate, explicit control over
squid/topic/role-scoped context. It is layered *underneath* whatever the harness
itself injects at the user-account level — squid doesn't (and can't, without
breaking the harness) suppress that layer.

## Layout

```
context/
├── topics/<topic>/memory.md     per-topic memory, seeded empty, then agent-owned
└── roles/<role-name>/           optional shared personas, see below
    ├── AGENTS.md                harness-agnostic persona content
    └── <harness>/<FILE>.md      thin per-harness shim that imports it
```

### `roles/` — shared personas across harnesses

A `role` is just a persona you want available to more than one harness without
copy-pasting it. Convention:

- `roles/<role-name>/AGENTS.md` holds the actual persona/instructions, written
  once, harness-agnostic.
- `roles/<role-name>/<harness>/<entry-file>` is a minimal shim for that harness's
  expected filename, which `@`-imports the shared file. Example
  (`roles/review/claude/CLAUDE.md`):

  ```markdown
  # CLAUDE.md — Reviewer Persona

  @../AGENTS.md
  ```

Add a new role by creating `roles/<name>/AGENTS.md` plus one shim subdirectory
per harness that should see it. There's no registry and nothing in squid's code
knows the word "role" — this is purely a content/naming convention, kept
intentionally dumb. (Squid's routing engine deliberately has no concept of
agent roles/profiles — see `docs/decisions/0032-route-chains-with-cwd-profile-agents.md`,
which is about routing syntax, not this convention; the two aren't in tension,
this just isn't wired into squid code at all.)

## Seeding on install

`bin/install.sh` and `bin/start.sh` copy this repo's `context/` tree into
`~/.squid/context/` on first run, **only if `~/.squid/context/` is empty**.
After that, this directory is entirely yours — tarball updates never touch it
again. Anything added to the repo's `context/` (this file, `roles/review/`,
etc.) becomes the default a fresh install starts with; anything you add locally
under `~/.squid/context/` stays local unless you also add it to the repo.
