---
status: superseded
date: 2026-07-11
superseded: 2026-07-12
---
# ADR-0027: Backend Naming Convention

> Superseded by [ADR-0028](0028-harness-provider-separation.md). The
> `{harness}-{provider}` convention below disambiguated a single composite
> `backends:` YAML key. ADR-0028 removes `backend` as a named entity —
> harness and provider become separate structured fields on the agent — so
> there is no composite ID left to disambiguate. Kept here for the
> collision reasoning, which ADR-0028 builds on directly.

## Context

Squid supports N×N backend variance: multiple coding agents (harnesses) ×
multiple API providers, with model passed at runtime from the UI.
The backend ID is the config key in
the YAML that a user writes or selects. It must be unique, human-readable,
and composable enough to cover all permutations without ad-hoc names.

The problem is that a name like `deepseek` is ambiguous — it could mean "the
DeepSeek API" (provider), or "Claude Code running on DeepSeek's API"
(harness + provider), or something else entirely. In practice it means the
latter, but that knowledge is tribal.

We also have a naming collision: `claude` is both a harness (the Claude Code
CLI) and a model provider (Anthropic API). When a Pi user runs a Claude
model, calling that backend `pi-claude` is only unambiguous if the reader
knows `claude` in suffix position means the model, not the harness.

A convention is needed so every backend ID can be decoded on sight.

## Decision

The naming convention is:

```
{harness}          — default provider (e.g. Pi → nvidia)
{harness}-{provider} — explicit provider override
```

Where:

- **Harness** is the coding agent: `cc`, `cx`, `oc`, `pi`, `cr`
- **Provider** is the model API endpoint: `claude`, `gpt`, `deepseek`, ...

Bare harness = its native default provider. A `-{provider}` suffix overrides
the provider. The suffix position is always a model provider, never a harness.

### Harness abbreviations

| Harness | Abbrev | Default provider | Default model |
|---|---|---|---|
| Claude Code | `cc` | anthropic | claude sonnet |
| Codex | `cx` | openai | gpt-4o |
| OpenCode | `oc` | deepseek | deepseek flash free |
| Pi | `pi` | nvidia | nemotron |
| Cursor | `cr` | cursor | cursor auto |

### Provider suffixes

| Suffix | Model API |
|---|---|
| `-claude` | Anthropic (Claude) |
| `-gpt` | OpenAI (GPT) |
| `-deepseek` | DeepSeek |
| `-local` | Local / self-hosted |

### Examples

| Backend ID | Harness | Provider |
|---|---|---|
| `cc` | cc | anthropic (default) |
| `cc-deepseek` | cc | deepseek |
| `cx` | cx | openai (default) |
| `cx-local` | cx | local |
| `oc` | oc | deepseek (default) |
| `oc-gpt` | oc | openai |
| `pi` | pi | nvidia (default) |
| `pi-nvidia` | pi | nvidia (explicit) |
| `pi-claude` | pi | anthropic |
| `pi-gpt` | pi | openai |
| `cr` | cr | cursor (default) |

### Disambiguation

The suffix `-claude` means "use the Claude model API", not "run the Claude
Code harness." The harness is always the first segment. This is unambiguous
because:

- `cc-claude` = harness `cc` + model provider `claude` — Claude Code
  hitting the Anthropic API, which is the default, so this bare `cc`
  covers it (the explicit form exists for symmetry).
- `pi-claude` = harness Pi + provider claude — Pi hitting
  the Anthropic API.

The `-gpt` suffix is unambiguous for the same reason: it always means
OpenAI's GPT model API, not a harness named gpt.

### Migration of existing backends

Current backends that are already well-known short names keep working:

| Old ID | New ID | Notes |
|---|---|---|
| `claude` | `cc` | Renamed to disambiguate from provider |
| `codex` | `cx` | Abbreviation for consistency |
| `opencode` | `oc` | Abbreviation for consistency |
| `pi` | `pi` | Unchanged — already follows convention |
| `cursor` | `cr` | Abbreviation for consistency |
| `deepseek` | `cc-deepseek` | Now explicit about the harness |

The legacy `deepcla` → `deepseek` migration (from ADR-0024 era) is
superseded by the broader `deepseek` → `cc-deepseek` rename.

### Model is passed from the UI

The backend ID selects the harness + provider combination. The specific model
is not part of the backend config — it's passed from the UI at request time.
This is why the backend ID only encodes the harness-provider axis.
The model is a third, independent dimension passed at request time:

- `pi` = harness Pi + provider nvidia (default), model chosen per request
- `pi-claude` = harness Pi + provider anthropic, model chosen per request

A backend YAML config has no `model` field:

```yaml
backends:
  pi-claude:
    driver: pi
    provider: anthropic
    label: Pi + Claude
    gauge: claude
```

## Consequences

- Backend IDs are decodable on sight: the harness is always the first
  segment, the provider override is always the second segment after `-`.
- `-deepseek` in suffix position is clearly a provider, removing
  the ambiguity of the existing bare `deepseek` key.
- Existing users need to update their config to the new IDs. The codebase
  should include a migration pass (similar to `deepcla` → `deepseek`)
  that renames known old IDs to their new equivalents.
- Some IDs are longer (`cc-deepseek` vs `deepseek`), but the tradeoff is
  unambiguity and composability. Typeable abbreviations are short enough.
- The convention extends naturally to future harnesses (e.g. `ai` for
  aider, `gh` for GitHub Copilot) and future providers (e.g. `-gemini`
  for Google, `-llama` for Meta).
