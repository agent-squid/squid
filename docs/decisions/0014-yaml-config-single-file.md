---
status: accepted
date: 2026-05-30
updated: 2026-06-18
---
# ADR-0014: Single YAML Config File for Infrastructure Constants

## Context and Problem Statement

`FIRST_BYTE_TIMEOUT` and `RESPONSE_TIMEOUT` were hardcoded constants in
`agent/config.py`. A `config/default.yaml` file existed with `server.host`,
`server.port`, `agent.model`, and `agent.max_tokens`, but was never loaded.

Two questions arose when wiring up the YAML loader:

1. **Which values belong in YAML?** Agent config (model, max_tokens) already
   lives in the DB and is writable at runtime via `POST /config/agents`. Putting
   it in YAML too would create two sources of truth for the same thing.

2. **Should there be a default + local split?** The canonical pattern for
   user-editable config is a shipped `default.yaml` merged with a gitignored
   `local.yaml`, so that updates to defaults don't force users to reconcile
   conflicts. But this adds a merge code path for a case that doesn't yet exist.

## Considered Options

1. **Single `squid.yaml`, only infrastructure constants** — one file, simple
   loader, no merge logic. Values that belong in the DB stay in the DB.
2. **`default.yaml` + `local.yaml` with deep merge** — safe for future user
   customisation; adds merge complexity now for no current benefit.
3. **Single file with full config (including agent model/max_tokens)** — creates
   dual source of truth with the DB.

## Decision Outcome

**Option 1.** A `config/squid.yaml.example` ships with the install and is
bootstrapped to `~/.squid/squid.yaml` by `start.sh` on first run. The file is
scoped to infrastructure-level values that have no other home:

```yaml
server:
  host: "127.0.0.1"
  port: 8000
  localfile_roots:
    - "/tmp/<user>/squid"

agent:
  first_byte_timeout: 300
  response_timeout: 1800
```

The loader in `agent/config.py` tries `~/.squid/squid.yaml` first and falls
back to `<install>/config/squid.yaml` for development convenience. No merge
logic — one file wins entirely.

Agent-specific config (model, cwd, timeout) stays in the DB, writable at
runtime without a server restart.

Storing config in `~/.squid/` means it survives tarball installs/updates —
users never need to re-edit their config after upgrading.

## Consequences

- Good: one source of truth per config value — infrastructure in YAML, agent
  behavior in DB
- Good: loader is trivial, no merge edge cases
- Good: `first_byte_timeout` and `response_timeout` are now tunable without
  touching source code
- Good: config lives in `~/.squid/` and survives squid upgrades
- Good: `start.sh` substitutes `/tmp/<user>/squid` automatically in
  `localfile_roots` on first bootstrap — no manual path editing needed
- Note: if per-user overrides on top of a shared default become necessary,
  the loader can be extended to deep-merge — the current single-file approach
  is a deliberate deferral, not a permanent constraint
