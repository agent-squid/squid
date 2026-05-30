---
status: accepted
date: 2026-05-30
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

**Option 1.** `config/default.yaml` is renamed to `config/squid.yaml` and
scoped to four infrastructure-level values that have no other home:

```yaml
server:
  host: "127.0.0.1"
  port: 8899

agent:
  first_byte_timeout: 30
  response_timeout: 1800
```

The loader in `agent/config.py` is a single `yaml.safe_load` — no merge.
Agent-specific config (model, max_tokens, cwd, timeout) stays in the DB,
writable at runtime without a server restart.

The default+local split is deferred until there is an actual need to localize
a value. If that need arises, the loader can be extended to deep-merge a
`local.yaml` over `squid.yaml` without changing anything else.

## Consequences

- Good: one source of truth per config value — infrastructure in YAML, agent
  behavior in DB
- Good: loader is trivial, no merge edge cases
- Good: `first_byte_timeout` and `response_timeout` are now tunable without
  touching source code
- Bad: no mechanism for per-user overrides yet; requires editing `squid.yaml`
  directly (and handling merge on update)
- Note: if per-user overrides become necessary, add `config/local.yaml` with
  deep merge — the current single-file loader is a deliberate deferral, not a
  permanent constraint
