---
status: accepted
date: 2026-06-21
---
# ADR-0024: Drivers, Backends, Agents, and Routes

## Context

Squid historically used `backend` to mean both a CLI protocol adapter and a
specific provider configuration. DeepSeek support consequently inspected the
model name inside the Claude runner and injected provider-specific environment
variables in code. Adding another compatible endpoint required another code
branch, while backend names, colors, validation, and UI choices were duplicated
across the server, database, and browser.

The terminology also needs to distinguish topic routing from execution
configuration. A topic is the conversation/work thread. A named agent is the
execution profile addressed by `@agent`. A route is the user-facing binding
written as `#topic@agent`.

## Decision

Squid distinguishes these concepts:

- A **driver** is a coded coding-agent integration. Supported drivers are
  `claude`, `codex`, `cursor`, and `opencode`. Drivers own command
  construction, protocol support, stream parsing, tool normalization, session
  resume, and token semantics.
- A **backend** is a named YAML-configured instance of one driver. Backends own
  driver configuration: canonical provider, API key, base URL, gauge,
  additional arguments, UI color, default model, and protocol. Multiple
  backends may use the same driver with different endpoints, credentials, or
  protocols.
- A **named agent** is a user-defined execution identity: `(backend, model,
  cwd)`, plus optional timeout. `model` may be null, in which case the backend's
  default model is used. Agents are addressed with `@agent`.
- A **topic** is the conversation/work thread addressed with `#topic`.
- A **route** is `#topic@agent`: the topic plus the named agent that should
  handle a message.

Agents continue to store `(backend, model, cwd)` in SQLite. The backend ID is
resolved at execution time, the backend resolves to a driver plus driver
configuration and protocol, and the resulting driver protocol runs the request.
Routing is never inferred from the model name.

`api_key` may be literal or referenced from an environment variable with
`{env: NAME}` and is resolved only when the backend executes. Drivers translate
canonical `api_key` and `base_url` values into their native environment or
settings. Raw `env`, `settings`, and `args` remain escape hatches. Backend
metadata returned to the UI never contains credentials or raw settings.

Each backend selects a gauge adapter independently of its driver. For example,
`deepseek` and `deepopen` can both use the coded `deepseek` balance adapter while
retaining separate API keys. Static gauges represent local or provider-managed
service without inventing a numeric quota. Quota responses are normalized by
the server and selected only from backend configuration, never model names.

Codex driver settings, including settings generated from canonical connection
fields, are flattened to repeated `-c dotted.key=value` arguments.

The backend configuration fingerprint is stored with resumable sessions and
includes protocol. Squid starts a fresh session if execution-relevant backend
configuration changes.

## Consequences

- New endpoints and models supported by an existing driver require YAML only.
- Multiple backends may share one driver, such as `claude: claude` and
  `deepseek: claude`.
- Multiple backends may share one driver while using different protocols, such
  as `claude: oneshot-cli` and `claude-live: interactive-cli`.
- Multiple backends may share a gauge implementation without sharing keys.
- A genuinely different CLI protocol still requires a coded driver.
- Topic identity and execution identity stay separate: `#topic` owns the
  conversation, `@agent` owns `(backend, model, cwd)`, and `#topic@agent`
  selects the route.
- Backend IDs and colors are supplied dynamically to the UI.
- `agy` and `copilot` remain experimental runners and are not configurable
  drivers.
