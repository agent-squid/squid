---
status: proposed
date: 2026-08-15
updated: 2026-08-28
---
# ADR-0043: Local Model Providers Beyond Ollama — Reuse the Provider Pattern, Don't Build a Model-Management UI

## Context

ADR-0037 established two things for Ollama: (1) local model servers integrate
as **providers** (ADR-0028's provider/harness split) — `base_url` +
`auth.type` + `parallel`, consumed by an existing harness's (Pi/OpenCode)
custom-provider path, no new harness or runner — and (2) Squid *does* own a
thin slice of model lifecycle for Ollama specifically: provider-scoped
queueing, active load/unload on switch, and a user-initiated pull/remove UI
riding the login-PTY mechanism (ADR-0035), because Ollama exposes exactly the
primitives (`/api/ps`, `/api/tags`, `ollama pull`, `keep_alive: 0`) that make
those safe and well-defined to build against.

Requests have come up to extend local-model support to vllm, vllm-mlx,
llama.cpp, LM Studio, and oMLX. These are not one shape:

- **vllm** and **llama.cpp** (`llama-server`) are single-model-per-process
  servers with an OpenAI-compatible `/v1/chat/completions` surface and no
  persistent daemon or built-in model registry. Switching models means
  restarting the process with a different `--model`/`-m` flag; there's no
  `ollama pull`-equivalent subcommand and no `/api/ps`-equivalent load-state
  introspection.
- **vllm-mlx** is the same shape as vllm/llama.cpp, on MLX weights instead of
  safetensors/GGUF.
- **LM Studio** is a real daemon with its own model manager, dynamic
  load/unload, and a full GUI — closer to Ollama's shape than to
  vllm/llama.cpp's, but the model management already lives in LM Studio's own
  app, not behind any CLI Squid could drive.
- **oMLX** (github.com/jundot/omlx) is also a real daemon, for MLX
  specifically: continuous batching, a two-tier (memory + SSD) paged KV
  cache, an OpenAI-compatible endpoint, and its own admin UI (`/admin`) for
  model management, chat, and benchmarking. It is not "just a UI over
  vllm-mlx" — it's a separate inference server with engine-level features
  (batching, SSD KV caching) that raw vllm-mlx/mlx-lm don't provide on their
  own; the admin UI is one part of it, not the whole thing.

Building Ollama-equivalent pull/remove/active-switch UI for each of these
would mean reimplementing, per backend, what LM Studio and oMLX already ship
as GUIs, and what tools like llama-swap already ship as a generic
reverse-proxy that adds dynamic model switching in front of any single-model
server (llama.cpp, vllm, vllm-mlx, mlx-lm) by watching a config and
restarting the right process on request. None of that is a gap in the
*provider* abstraction — it's model-lifecycle tooling that already exists
outside Squid.

## Decision

**Add vllm, vllm-mlx, and llama.cpp as documented provider presets, the same
shape as the existing `local`/`ollama` examples in
`config/squid.yaml.example`.** These *generalize* the placeholder `local:`
entry already in the example file, whose own comment describes it as a
vllm-mlx server — the new `vllm-mlx` preset supersedes and renames that
`local:` stub rather than duplicating it, so the file ends up with one preset
per named backend instead of a generic `local` plus overlapping copies. Each
preset carries `base_url`, `supported_apis`, `auth.type` (`none` or `api_key`
depending on whether the server was launched with a key), and `parallel` set
per backend's real concurrency model (`true` for vllm's continuous batching,
`false` for a single Apple Silicon box running vllm-mlx or llama.cpp).

`supported_apis` is set per the **harness** consuming the provider, not per
the engine: Pi/OpenCode talk `/v1/chat/completions`, which is what vllm /
vllm-mlx / llama.cpp expose natively, so those presets ship
`supported_apis: [/v1/chat/completions]`. A codex-fronted local server is a
different value — the existing `local:` example targets codex and therefore
uses `supported_apis: [/v1/responses]`; a preset written for codex keeps that
value. No new harness, no new runner — Pi/OpenCode's existing custom-provider
path (ADR-0037's Path A) is all that's needed. As with `local`/`ollama`
today, the served model name(s) go in the provider's `models:` list (codex
cannot auto-discover them from `/v1/models`); for a raw single-model engine
that list is effectively one fixed model.

**Add LM Studio and oMLX as provider presets under the same shape**, since
both expose an OpenAI-compatible endpoint (`localhost:1234/v1` and
`localhost:8000/v1` respectively) and can set `parallel` based on their own
daemon's concurrency behavior. Both already run dynamic multi-model
management themselves *and* load models on demand by the request's `model`
field (LM Studio's just-in-time loading; oMLX's own model manager), so a
preset can list several entries in `models:` and each request pulls up the
right one with no Squid-side pull/remove UI at all — the user installs models
in LM Studio's or oMLX's own GUI, and the requested model is what gets served.
This is a stronger position than "whatever happens to be loaded": switching
between installed models needs no Squid machinery *and* no manual GUI step
per switch.

**Explicitly do not extend Ollama's active load/unload, provider-scoped-queue
load-visibility (`"loading"` SSE event), or pull/remove PTY UI (ADR-0037's
queueing/Amendment sections) to any of these five.** That machinery was built
against Ollama-specific primitives (`/api/ps`, `/api/tags`, `keep_alive: 0`,
`ollama pull`/`ollama rm` as real, narrowly-scoped subcommands) that don't
have equivalents in vllm/llama.cpp/vllm-mlx, and are redundant for LM
Studio/oMLX, which already solve it themselves. For the three
single-model-per-process engines, the documented recommendation is to run a
companion process-swap proxy (llama-swap or equivalent) in front of them and
point Squid's `base_url` at that proxy instead of at the raw engine — Squid
still only ever calls an OpenAI-compatible endpoint, and the switching
complexity stays in a tool purpose-built for it. Behind such a proxy the
provider's `models:` list is no longer one fixed model: llama-swap routes on
the request's `model` field and restarts the right process, so the list
enumerates the swap-config's model IDs and switching happens by model name,
exactly as it does for the daemon-backed providers above.

**Squid does not become a general local-model-management UI.** LM Studio and
oMLX already are one, each covering a real, current need (GGUF+MLX with a
full GUI; MLX with continuous batching and SSD KV caching, respectively).
Duplicating that inside Squid would be a worse rebuild of an already-solved
problem, and would cut against the provider/harness split (ADR-0028) that
keeps Squid's own surface to "talk to an OpenAI-compatible endpoint," not "be
one."

## Consequences

- `config/squid.yaml.example` gains commented presets for `vllm`,
  `vllm-mlx`, `llama.cpp`, `lmstudio`, and `omlx`; the existing placeholder
  `local:` entry is folded into the `vllm-mlx` preset (same server, named
  concretely) so there's one preset per backend and no generic/duplicate
  overlap. Documentation only — no code changes to `agent/providers.py` or
  `agent/harnesses.py`, since all five fit the existing `Provider` dataclass
  and custom-provider sync path unchanged.
- None of Ollama's queueing/load-visibility/active-unload/pull-remove
  machinery (ADR-0037) is generalized to these providers. A user wanting
  Ollama-equivalent dynamic switching for vllm/llama.cpp/vllm-mlx is pointed
  at a companion tool (llama-swap or equivalent) in documentation, not at new
  Squid code.
- Switching via a process-swap proxy trades Ollama's `keep_alive`-style warm
  switch for a cold start: the first request after a model change blocks on
  process spawn + weight load (seconds to tens of seconds). Squid does nothing
  to mask this — it surfaces to the harness as a slow first request, and can
  approach request timeouts and the backpressure/heartbeat handling of
  ADR-0040 on large weights. The daemon-backed providers (LM Studio, oMLX)
  have the same cold-start cost on a JIT load, just without a process restart.
- If real demand surfaces later for Squid-driven process supervision of a
  single-model engine (spawn/health-check/kill on switch), that's a new ADR,
  not an extension of this one — it's a different risk class (Squid owning a
  GPU-bound subprocess lifecycle) than anything the provider abstraction
  covers today.
- Model-file sharing across backends (Ollama's GGUF blob store, llama.cpp's
  own HF-repo cache, vllm's safetensors HF cache, vllm-mlx/oMLX's MLX-format
  HF cache) is left as user-managed: Ollama can import an already-downloaded
  local GGUF via a Modelfile `FROM <path>` without a second download, which
  is worth a documentation callout, but no Squid-side dedup logic is built,
  since vllm-mlx/oMLX (MLX) and vllm (safetensors, with only experimental
  single-file GGUF support) don't share weight formats with the GGUF pair in
  any general way.

## Implementation note (2026-08-28)

`omlx` is the first of the five presets actually added to
`config/squid.yaml.example`, commented out like the other opt-in providers
(`deepseek`, `kimi`, `fireworks`, `baseten`); `vllm`, `vllm-mlx`,
`llama.cpp`, and `lmstudio` remain undone, and the `local:` placeholder
entry has not yet been folded into a `vllm-mlx` preset. Status stays
`proposed` until the rest lands. See also ADR-0037's 2026-08-28 amendment,
which drops Ollama's install-time `default_provider` auto-promotion for the
same reason this ADR treats every local backend as an equally-weighted,
opt-in provider preset — no backend gets special ordering over another.
