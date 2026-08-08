---
status: accepted
date: 2026-08-07
updated: 2026-08-07
---
# ADR-0037: Local Models via Ollama — Provider-Scoped Queueing and Active Load/Unload Switching

## Context

Every harness Squid runs today (ADR-0028) wraps a real coding-agent CLI —
Claude Code, Codex, Cursor, OpenCode, Pi — each with its own subprocess,
tool-use loop, and provider-managed auth (`subscription` or `api_key`).
Ollama is a different shape: a local HTTP daemon that serves chat
completions for whichever open-weight checkpoints the user has pulled
(`qwen2.5`, `qwen3`, `gemma3`, ...), with no subprocess per turn and no
tool-use loop of its own.

An earlier draft of this ADR treated that difference as reason to build a
whole new `ollama` harness + runner for everything. That's no longer the
primary plan: `agent/resolve.py` already has a mechanism
(`sync_pi_models_store`/`sync_pi_provider`, lines 96-140 and 250-265) for
registering an arbitrary OpenAI-compatible endpoint as a **custom provider**
that an existing harness (Pi, and OpenCode via the equivalent opencode-side
path) talks to — built for things like self-hosted NIM-alternatives, and a
direct fit for Ollama's OpenAI-compatible surface
(`http://localhost:11434/v1/chat/completions`). Running a local checkpoint
*with* Pi's real tool-use loop (file edits, bash — "side effects on the
local filesystem," per the discussion that shaped this) was the actual
motivating goal, and that's Path A below, not a new execution mechanism.

The narrower, still-relevant chat-only harness from the original draft is
kept as an optional Path B, for cases where no tool loop is wanted at all.

What's genuinely new work in both cases, and what this ADR is really about,
is **queueing** plus **active load/unload management**, as two main
features:

1. Squid queues per `(topic, agent)`, parallel across topics
   (`agent/topic_queue.py:758`: `queue_key = f"{topic}@{agent}" if agent
   else topic`). That's correct for hosted APIs — Claude/Codex handle their
   own concurrency — but wrong for a shared local Ollama daemon: two
   different topics calling agents that both resolve to the same local
   checkpoint (or even different checkpoints on the same daemon — see
   below) get independent `TopicWorker`s dispatching in parallel, which for
   one GPU means Ollama thrashes between loading/evicting models instead of
   Squid queueing them in a visible, predictable order.
2. Once requests are serialized through one lane, switching which model
   that lane is talking to should be something Squid actively drives, not
   something it just waits out. Ollama's own idle-eviction (default ~5
   minutes) is passive — it works, but it means the previous model's VRAM
   isn't freed until a timer fires, which is slow and non-deterministic
   exactly where Squid now has enough visibility (one FIFO lane per
   physical resource) to do better. This was the original ask that started
   this ADR: switching agents should feel like "turn off the old model,
   turn on the new one," not "wait a few minutes and hope."

Explicitly out of scope, same as the original draft: automatic
`ollama pull`/model-download management, and model-comparison/eval/stats
tooling across checkpoints.

## Decision

### Two integration paths, not mutually exclusive

**Path A (primary): existing coding-agent harnesses via a custom
provider.** No new harness or runner. A named agent such as `qwen25-pi` is
just `(harness: pi, provider: ollama, model: qwen2.5:7b, cwd: ...)` — the
same `(harness, provider, model, cwd)` shape every agent already uses
(ADR-0028). Pi's existing custom-provider sync
(`agent/resolve.py:250-265`) writes the `baseUrl`/`api`/model entry into
`~/.pi/agent/models.json` the same way it would for any other non-standard
provider; nothing about that mechanism is Ollama-specific. This is the path
that actually runs a local checkpoint as Pi's coding brain.

**Path B (optional, secondary): a standalone chat-only `ollama` harness.**
Unchanged from the original draft — added to `agent/harnesses.py`'s fixed
set, protocol `oneshot-cli`, install-check replaced by a daemon-reachability
check (`GET {base_url}/api/tags`, since there's no CLI binary to
`shutil.which()`), plus a new `run_ollama` runner in `agent/runners.py`
shaped like `run_echo` (`agent/runners.py:1910` — no subprocess, plain async
generator) rather than the CLI-spawning runners: a streaming HTTP client
against `{base_url}/api/chat`, yielding text deltas then a `_stats` dict
from Ollama's own response fields. No tool-call handling. Path B is
independent of Path A and doesn't need to ship alongside it — everything
below (queuing groups, load visibility) applies equally to both, since it's
keyed by *provider*, not by which harness (or lack of one) is calling.

### Provider auth type: `none`

Both paths need an Ollama provider entry with no credential:

```yaml
providers:
  ollama:
    label: Ollama
    base_url: http://localhost:11434/v1
    auth: {type: none}
```

Reusing `auth.type: subscription` was considered — `sync_pi_provider`
forwards `base_url` unconditionally regardless of auth type, so
`subscription` would work mechanically for Path A. Rejected anyway for
correctness of intent: `subscription` means "the harness CLI handles its
own browser login" everywhere else it's used (ADR-0028); labeling a local,
credential-free daemon that way would mislead anyone reading `squid.yaml`
into expecting a login flow that doesn't exist. This ADR adds a third
`SUPPORTED_AUTH_TYPES` value, `none` (`agent/providers.py:20`).
`missing_secrets()` (`agent/providers.py:79`) already only fires for
`api_key`, so `none` needs no new logic there — just acceptance as a valid
enum value.

### Provider-scoped queueing, keyed by provider name

No new field on `Provider`. Instead, `topic_queue.py`'s key derivation
(`agent/topic_queue.py:758`) gains one conditional: when a resolved agent's
provider has `auth.type == "none"`, the queue key becomes
`f"provider:{provider_name}"` instead of `f"{topic}@{agent}"` — collapsing
*every* agent, topic, and harness that resolves to that provider into one
`TopicWorker`, i.e. one FIFO lane. Providers with any other auth type
(`subscription`, `api_key`) keep today's per-`(topic, agent)` parallel
behavior, unchanged. `auth.type: none` already means "local, credential-free
daemon" (see above) — serial dispatch is just what that implies mechanically
for a single-GPU box, so it doesn't need its own opt-in field; a second
config knob would only be saying the same thing the auth type already says.

This is scoped by **provider name**, not by harness or by model, for two
reasons that only became clear once Path A entered the picture:

1. **The same local daemon is reachable through more than one harness.** A
   Path-B chat-only `ollama` agent and a Path-A `pi`-harness agent using
   Ollama as a custom provider both resolve to the same provider entry
   (`ollama`), so they land in the same lane automatically. Keying by
   harness (the original draft's approach) would let them run concurrently
   against each other — exactly the contention this ADR exists to prevent.
2. **Different checkpoints on one daemon still contend for the same
   hardware.** The original draft keyed by *model* (`ollama:{model}`),
   letting `qwen25` and `qwen30` requests run "in parallel." That's usually
   wrong: most local setups can't hold two checkpoints resident at once, so
   concurrent requests to different models on the same daemon just make
   Ollama thrash between them rather than actually parallelizing. One lane
   per provider name (i.e. per physical resource) matches how the hardware
   behaves, and matches the "swap to the new model" mental model from the
   original discussion — switching checkpoints is an expected serialized
   load/unload, not a promise of concurrency.

A user running more than one local box (each with its own Ollama instance)
still gets real parallelism for free: each instance is just a separate
provider entry (distinct name, distinct `base_url`) — e.g. `ollama-gpu1` and
`ollama-gpu2` — and since the lane is keyed by provider name, they naturally
get independent `TopicWorker`s with no extra config. There's no way to
intentionally *merge* two differently-named local providers into one shared
lane under this scheme, but no real use case for that surfaced in the
original discussion — it would mean two separate daemons/processes
pretending to be one physical resource, which isn't how local setups are
actually built. The existing `/queue` endpoint and
`queue_depth()`/`position_of()` (`agent/topic_queue.py:212`,
`agent/server.py:1346`) need no changes — they already report position
within whatever worker owns a key; only the key derivation gains this
auth-type-conditional branch.

### Load-state visibility

Independent of which path dispatches the request, `topic_queue.py` can
check Ollama's `GET {base_url}/api/ps` (resident-models list) right before
handing a queued item to its runner. If the target model isn't resident,
emit an SSE event before dispatch — same channel/shape as the existing
`sse_event("queued", ...)` position event (`agent/server.py:995`), new
event type `"loading"` carrying the model name, so the UI can show "Loading
qwen25..." instead of an unexplained stall. This is gated on the provider's
`auth.type == "none"` — the same providers where cold-load latency and
hardware contention are real — rather than being harness-specific, so a
Path-A (Pi) call gets the same visibility as a Path-B (chat-only) call. No
polling loop is needed to detect "finished loading": the request itself
blocks through the load, and the first streamed byte (Path B) or Pi's first
protocol event (Path A) is the natural "now warm" signal. The `/api/ps`
check itself is Ollama-specific; if a different local-model server joins
this mechanism later under its own `auth: {type: none}` provider entry, it
would need its own load-check implementation, not a shared one — not a
problem this ADR needs to solve now with only one local backend in scope.

### Active load/unload on model switch

The main feature this ADR adds on top of queueing: don't just wait on
Ollama's idle timer, actively free the outgoing model when the lane is
about to switch to a different one.

Each `TopicWorker` serving a provider-scoped (`auth.type == "none"`) key
already processes its queue strictly in order (that's the queueing decision above), so it's the
one place that can track, in memory, "which model did this lane last
dispatch." When the next queued item targets a *different* model than the
last one, the worker sends an explicit unload for the outgoing model before
handing the new item to its runner — Ollama supports this directly via
`keep_alive: 0` on a request naming that model (`/api/chat` or
`/v1/chat/completions`). This is a small addition to
`TopicWorker._process` (`agent/topic_queue.py`), not a new component: the
same function that already decides whether to emit the `"loading"` event
(above) is the one that compares outgoing vs. incoming model and fires the
unload. The result is deterministic: the old model's memory starts freeing
before the new one starts loading, instead of sometime in the next five
minutes.

This reuses the `"loading"` SSE event rather than adding a second event
type: its payload carries both model names — `{"to": "qwen30", "from":
"qwen25"}` on a switch, `{"to": "qwen25"}` with no `"from"` on a cold start
where nothing was previously resident in that group — so the UI renders one
state ("Switching qwen25 → qwen30...") instead of two back-to-back,
seemingly-unrelated events.

No new agent-picker UI is needed to make this "easily used." `qwen25` and
`qwen30` are already ordinary named agents (ADR-0028), selectable through
Squid's existing agent switcher exactly like any Claude/Codex/Pi agent —
that picker is how a user actually triggers a switch, and it needs no
changes. What's new is purely what happens underneath that switch: an
active, visible handoff instead of a silent wait on Ollama's own timer.

## Consequences

- No new harness or runner is required to get a local checkpoint running
  *with* a tool-use loop — Path A reuses Pi's existing custom-provider
  mechanism unchanged. This is a smaller change than the original draft of
  this ADR assumed.
- New `auth.type: none` value in `agent/providers.py`'s
  `SUPPORTED_AUTH_TYPES` — needed for correctness of intent, though the
  existing Pi custom-provider code path doesn't technically require it
  (`base_url` flows through regardless of auth type there); it prevents a
  local, credential-free provider from being mislabeled as "subscription"
  (implying a login flow that doesn't exist).
- No new field on `Provider` — the one behavioral core of this ADR piggybacks
  on the existing `auth.type` instead. `agent/topic_queue.py`'s queue-key
  derivation gains an auth-type-conditional branch: agents on an
  `auth: {type: none}` provider share one FIFO lane keyed by provider name,
  regardless of topic, agent, or harness. Every other provider's queueing is
  unaffected. Multi-instance local setups (two GPUs, two daemons) still
  parallelize for free, since each is its own provider entry/name.
- New SSE event type (`"loading"`) alongside the existing `"queued"` event,
  gated on the provider's `auth.type == "none"`; the chat/status UI needs a
  handler for it. Additive to the event stream, no protocol break for
  existing consumers.
- `TopicWorker` gains a small piece of in-memory state per provider-scoped
  lane — the last model it dispatched — used to decide when to emit an
  explicit `keep_alive: 0` unload call for the outgoing model before
  switching. This is the active-swap behavior that was the original goal
  of this whole ADR; providers on other auth types are unaffected, and no
  separate scheduler/background process is introduced — it's driven
  entirely by the existing dispatch loop.
- The `"loading"` event's payload grows to carry both `to` and an optional
  `from` model name, so a switch renders as one state ("Switching qwen25 →
  qwen30...") instead of a separate unload notice plus a separate load
  notice.
- Path B (standalone `ollama` harness + `run_ollama` runner) remains
  optional and independent — can ship, be deferred, or be dropped entirely
  without affecting Path A or the provider-scoped queueing mechanism, since
  both are provider-scoped rather than harness-scoped.
- Explicitly out of scope, left for later ADRs if pursued: automatic
  `ollama pull`/model-download management, any new tool-use loop beyond
  what Pi (or another harness) already provides, and
  model-comparison/eval/stats-diffing across checkpoints.
- Nothing in this ADR is implemented yet — it records a design decision to
  guide a future change, not shipped code.
