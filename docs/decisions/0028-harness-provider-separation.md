---
status: accepted
date: 2026-07-12
updated: 2026-07-12
---
# ADR-0028: Harness/Provider Separation

> Supersedes [ADR-0027](0027-backend-naming-convention.md)'s
> `{harness}-{provider}` composite backend-ID convention — see Consequences.

## Context

ADR-0024 defined a **driver** as the coded CLI integration (`claude`, `codex`,
`cursor`, `opencode`, `pi`) and a **backend** as a YAML-configured instance of
one driver plus connection details (`provider`, `api_key`, `base_url`, `gauge`,
...). ADR-0027 layered a naming convention on top — `{harness}-{provider}` —
using "harness" as the human name for what the code still calls `driver`, and
established that a backend is really a harness × provider pair.

Three problems remain past naming:

1. **"Driver" undersells what the field is.** It's not just wiring code, it's
   the coding-agent product itself (Claude Code, Codex, Cursor, OpenCode, Pi).
   ADR-0027 already talks in terms of harness/provider; the code and config
   should say the same word.
2. **Provider is a string, not a config surface.** `provider`, `base_url`,
   `gauge`, and the implicit "does this need an api_key or a browser login"
   question are currently set per-backend, duplicated across every backend
   that happens to hit the same endpoint. There's no place to say once "NIM's
   quota adapter is X and its model list is Y" and have every `*-nvidia` backend
   pick it up.
3. **Backend is a redundant layer once (1) and (2) are fixed.** A named agent
   already stores `(backend, model, cwd)`. Once a backend is nothing but
   "which harness, which provider," using a Codex+NIM combination the first
   time requires adding a `backends:` entry that exists only to name a
   permutation the agent record already has to state. Not every harness ×
   provider pairing works, either — some combinations are untested or simply
   incompatible (e.g. a subscription-only harness can't take an arbitrary
   `base_url`) — so a fixed named-backend list was never a validation
   mechanism anyway, just an extra step.

This ADR promotes provider to a first-class config section, finishes the
driver → harness rename ADR-0027 started in spirit, and retires `backend` as
a named entity in favor of agents selecting harness + provider directly.

## Decision

### Harness

The primary runtime term is `harness`: `agent/harnesses.py` owns the supported
harness set, protocol defaults, install paths, install commands, default
providers, and API compatibility metadata. Same five values, now spelled out
instead of abbreviated in code (`claudecode`, `codex`, `cursor`, `opencode`,
`pi`); the `cc`/`cx`/`cr`/`oc`/`pi` short forms from ADR-0027 remain an
ID-prefix convention (agent IDs, gauge labels), not the internal field value.
Legacy `driver` names still exist in compatibility code while
`agent/backends.py` and older API fields are being retired.

The Claude Code harness is renamed from `claude` to `claudecode` — matching
the existing `opencode` pattern — because `claude` also names a provider
(Anthropic's models). ADR-0027 already flagged this exact collision for
backend IDs (`pi-claude` = Pi harness + Claude provider); giving the harness
its own non-overlapping identifier removes the ambiguity at the source
instead of relying on position-in-string to disambiguate it.

Protocol is a harness property, not something a provider or agent chooses
independently — it's dictated by what the CLI itself supports. Only
`claudecode` has a true persistent structured stdin/stdout protocol, so it's
the only harness offering `interactive-cli`; every other harness is
`oneshot-cli` only, unchanged from `SUPPORTED_PROTOCOLS_BY_DRIVER` /
`DEFAULT_PROTOCOL_BY_DRIVER` today. The interactive idle-timeout default
(`DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS`) is likewise a harness-level
constant rather than a per-agent override — today's per-backend
`interactive.idle_timeout_seconds` override (exercised by exactly one test
backend) is dropped for simplicity. If per-agent tuning turns out to be
needed later, it can be added back as an agent field without touching this
ADR's harness/provider split.

Supported harnesses and default provider. Install commands are unchanged and
stay defined once in `agent/harnesses.py`, matching the README table — not
duplicated here:

| Harness | Value | ID prefix | Protocol | Default provider |
|---|---|---|---|---|
| Claude Code | `claudecode` | `cc` | interactive-cli | Claude (anthropic) |
| Codex | `codex` | `cx` | oneshot-cli | GPT (openai) |
| Cursor | `cursor` | `cr` | oneshot-cli | Cursor |
| OpenCode | `opencode` | `oc` | oneshot-cli | NVIDIA NIM (`nvidia`) |
| Pi | `pi` | `pi` | oneshot-cli | NVIDIA NIM (`nvidia`) |

"Installed" reuses the existing `shutil.which()` check in `config.py`
(`CLAUDE_PATH`, `CODEX_PATH`, ...) — no new detection mechanism. This is
queryable through `/health`: its `harnesses` payload contains
`{id, label, install_cmd, installed, protocol, interactive,
default_provider, supported_apis, compatible_providers}` for the settings UI.
The startup `_check_deps()` warning is a thin wrapper around that same
harness data. There is no separate `/config/harnesses` endpoint in the
current implementation.

### Provider

`providers:` becomes a top-level config section. A provider owns everything
about the API endpoint that doesn't depend on which harness is talking to
it — including `color`, since color exists to show at a glance which quota
pool a message drew from, which is a provider fact, not a harness fact:

```yaml
providers:
  anthropic:
    label: Claude
    color: "#AE5332"
    auth: {type: subscription}
  openai:
    label: GPT
    color: "#7070A0"
    auth: {type: subscription}
  cursor:
    label: Cursor
    color: "#8F8D8D"
    auth: {type: subscription}
  deepseek:
    label: DeepSeek
    color: "#4D9DE0"
    base_url: "https://api.deepseek.com/anthropic"
    auth: {type: api_key, api_key: {env: DEEPSEEK_API_KEY}}
    gauge: deepseek
    models: [deepseek-chat, deepseek-reasoner]
  nvidia:
    label: NVIDIA
    color: "#76B900"
    base_url: "https://integrate.api.nvidia.com/v1"
    auth: {type: api_key, api_key: "nvapi-..."}
    gauge: {type: static, text: "NIM-managed"}
    models: [nvidia/nemotron-4-340b, meta/llama-3.1-405b, ...]
```

`api_key` lives under `auth`, not as a sibling field — it's part of how the
provider authenticates, not a connection detail like `base_url`. When
`auth.type` is `api_key`, `auth.api_key` is required — either a literal
string or `{env: NAME}` to read from an environment variable at execution
time, unchanged from ADR-0024. It's never returned in provider metadata sent
to the UI. When `auth.type` is `subscription`, `auth.api_key` isn't read at
all, even if present.

A provider id can be declared more than once for the same underlying vendor
to hold separate credentials — e.g. `deepseek` and `deepseek-team2`, each
with its own `auth.api_key` and `color`. This is also the new home for the old
backend-level `env`/`settings`/`args` escape hatches: they move to the
provider, since they describe how to reach an endpoint, not which harness is
asking.

Squid ships only zero-config providers in code defaults
(`anthropic`, `openai`, `cursor`, `opencode`). Providers requiring user
credentials, such as `nvidia` and `deepseek`, are documented in
`config/squid.yaml.example` rather than auto-seeded for existing installs.
`nvidia` is the canonical provider id for NVIDIA NIM because both Pi and
OpenCode pass models using a `provider/model` prefix, so the provider id must
match the CLIs' native provider name.

**Direct vs. aggregator providers.** No new field for this — it falls out of
`models`. A direct provider (`deepseek`, `anthropic`, `openai`) serves its own
model line and typically has a short or empty `models` list. An aggregator
provider (`nvidia`) fronts many vendors' models behind one endpoint and has a
long `models` list. Both are configured the same way.

### Auth type

`auth.type` is `api_key` or `subscription`:

- `subscription` — the harness CLI handles its own browser login (Claude Code
  max/pro, Codex ChatGPT login, Cursor login). Squid never asks for or stores
  a credential; `base_url`/`auth.api_key` are meaningless and missing-secret
  checks never fire for these agents.
- `api_key` — Squid resolves `auth.api_key` (literal or `{env: NAME}`,
  unchanged from ADR-0024) and injects it plus `base_url` into the harness's
  native env/settings, same translation `execution_env()`/`driver_settings()`
  do today.

### Backend is retired — agents select harness + provider directly

There is no `backends:` config section. A named agent extends ADR-0024's
`(backend, model, cwd)` to `(harness, provider, model, cwd)`:

```
name: cc-deepseek-work
harness: claudecode
provider: deepseek
model: deepseek-chat
cwd: ~/Work/squid
```

The agent's own name already serves as its display label — no separate
`label` field is needed at the agent level. `label` on a provider (above) is
still needed for the two places a backend's label shows up in the UI today
(`ui/app.js`): the provider-picker dropdown when creating/editing an agent,
and the quota status row, where it's paired with `color` as one dot-plus-name
unit identifying which quota pool a gauge belongs to.

Not every harness × provider pair is expected to work, so the UI doesn't
offer a raw cross-product. The current compatibility list is derived from
API-shape overlap: each harness declares `supported_apis`, each provider
declares `supported_apis`, and `/health.harnesses[].compatible_providers`
contains providers whose sets intersect, plus the harness default provider
when present. The legacy `compatible_providers` harness config key is still
accepted as an explicit additive override. Anything outside the list remains
reachable through the existing freeform provider path in config/API usage;
the UI uses the list as guidance, not as server-side model validation.
Live "does this combination actually work" validation (e.g. a test-connection
action) is out of scope for this ADR and can be layered on later without
changing this shape.

### Model catalog stays local, no hosted sync

`models:` on a provider is a **suggestion list for the UI**, not a validated
enum. The agent-creation model field renders it as a dropdown with a
freeform-text fallback (`<datalist>`-style): pick from the list, or type any
model string the provider actually supports. Squid does not validate the
model against the list server-side.

A centrally-hosted, auto-updating provider/model catalog was considered and
rejected for now: it adds an online dependency and a staleness/versioning
problem (new models ship faster than any catalog sync would) to solve a UX
nicety that a plain freeform input already handles. Revisit only if
hand-maintaining `models:` lists in `squid.yaml` becomes a recurring
complaint — the local-list-plus-freeform design doesn't foreclose that,
since a hosted catalog could later populate the same field.

## Consequences

- ADR-0027 is superseded. Its `{harness}-{provider}` convention existed to
  disambiguate a single composite `backends:` YAML key; with `backend` gone
  and harness/provider now separate structured fields on the agent, there is
  no composite ID left to disambiguate. Its collision reasoning (`claude` as
  both harness and provider) is resolved more directly here, by renaming the
  harness to `claudecode` rather than relying on string position.
- `harness` is the primary config/API field. Legacy `driver` naming still
  appears in compatibility code and old tests until `agent/backends.py` and
  legacy backend surfaces are fully removed. For current `squid.yaml`, this
  is a breaking config change: `driver:` → `harness:` wherever it was set.
- Per the precedent set for the `deepcla` → `deepseek` cleanup, there is no
  automatic migration or aliasing. Users update `squid.yaml` by hand;
  `config/squid.yaml.example` ships the new shape.
- `providers:` is a new top-level config section. Loading uses
  `_configured_providers()` plus a zero-config default providers table
  (`anthropic`, `openai`, `cursor`, `opencode`). API-key providers such as
  `nvidia` and `deepseek` live in `config/squid.yaml.example`.
- `backends:` is removed from the primary config shape. New execution uses
  `agent/harnesses.py`, `agent/providers.py`, and `agent/resolve.py`; the
  resolved runtime is ephemeral and exposes the same execution surface callers
  used to get from `Backend` (`execution_env()`, `driver_settings()`, `args`,
  `protocol`, `interactive`, `fingerprint`, availability, gauge, color).
  `agent/backends.py` remains as compatibility code while older tests and API
  surfaces are retired.
- SQLite stores structured `harness`/`provider` fields directly. It does not
  store `backend` or `last_backend`; API responses compute those compatibility
  refs as `harness` or `harness:provider` when older UI/client surfaces still
  need them. `AgentRequest` accepts `harness`/`provider` first and falls back
  through legacy `backend` parsing (`split_agent_ref`) when needed.
- Runtime attribution follows the same rule: logs and durable stats should
  include `harness` and `provider`; `backend` may be emitted alongside them
  only as the legacy runtime reference.
- Session-resume fingerprinting (ADR-0024's "backend configuration
  fingerprint") is computed from the resolved harness + provider + model +
  protocol at execution time instead of `Backend.fingerprint()`. A change to
  a provider's `base_url`/`auth.api_key`/`env` still starts a fresh session
  for every agent using it, same as today.
- Harness metadata is exposed through `/health.harnesses`; provider metadata
  is exposed through `/health.providers`. `_check_deps()` includes Pi and uses
  the same harness install-check data instead of a separate table.
- Pre-launch schema compaction removed migration/backfill code and legacy
  backend storage columns. Existing local SQLite files may still contain extra
  columns from earlier development builds, but current code no longer reads or
  writes them.
- This ADR is implemented in the current code for config loading, runtime
  resolution, harness/provider metadata, and the settings UI. Remaining work
  is cleanup: retire `agent/backends.py` and remove legacy computed `backend`
  API fields once dependent UI/tests are updated.
