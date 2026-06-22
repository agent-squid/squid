---
status: accepted
date: 2026-06-21
---
# ADR-0024: Configurable Backends and Coded Drivers

## Context

Squid historically used `backend` to mean both a CLI protocol adapter and a
specific provider configuration. DeepSeek support consequently inspected the
model name inside the Claude runner and injected provider-specific environment
variables in code. Adding another compatible endpoint required another code
branch, while backend names, colors, validation, and UI choices were duplicated
across the server, database, and browser.

## Decision

Squid distinguishes two concepts:

- A **driver** is a coded CLI adapter. Supported drivers are `claude`, `codex`,
  `cursor`, and `opencode`. Drivers own command construction, stream parsing,
  tool normalization, session resume, and token semantics.
- A **backend** is a named YAML-configured instance of one driver and one
  billing identity. Backends own canonical provider, API key, base URL, gauge,
  additional arguments, and UI color.

Agents continue to store `(backend, model, cwd)` in SQLite. The backend ID is
resolved at execution time, and the resulting driver runs the request. Routing
is never inferred from the model name.

`api_key` may be literal or referenced from an environment variable with
`{env: NAME}` and is resolved only when the backend executes. Drivers translate
canonical `api_key` and `base_url` values into their native environment or
settings. Raw `env`, `settings`, and `args` remain escape hatches. Backend
metadata returned to the UI never contains credentials or raw settings.

Each backend selects a gauge adapter independently of its driver. For example,
`deepcla` and `deepopen` can both use the coded `deepseek` balance adapter while
retaining separate API keys. Static gauges represent local or provider-managed
service without inventing a numeric quota. Quota responses are normalized by
the server and selected only from backend configuration, never model names.

Codex driver settings, including settings generated from canonical connection
fields, are flattened to repeated `-c dotted.key=value` arguments.

The backend configuration fingerprint is stored with resumable sessions. Squid
starts a fresh session if execution-relevant backend configuration changes.

## Consequences

- New endpoints and models supported by an existing driver require YAML only.
- Multiple backends may share one driver, such as `claude: claude` and
  `deepcla: claude`.
- Multiple backends may share a gauge implementation without sharing keys.
- A genuinely different CLI protocol still requires a coded driver.
- Backend IDs and colors are supplied dynamically to the UI.
- `agy` and `copilot` remain experimental runners and are not configurable
  drivers.
