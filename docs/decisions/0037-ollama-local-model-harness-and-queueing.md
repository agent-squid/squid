---
status: accepted
date: 2026-08-07
updated: 2026-08-09
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
   (`agent/topic_queue.py`: `queue_key = f"{topic}@{agent}" if agent else
   topic`). That's correct for hosted APIs — Claude/Codex handle their own
   concurrency — but wrong for a shared local Ollama daemon: two different
   topics calling agents that both resolve to the same local checkpoint (or
   even different checkpoints on the same daemon — see below) get
   independent `TopicWorker`s dispatching in parallel, which for one GPU
   means Ollama thrashes between loading/evicting models instead of Squid
   queueing them in a visible, predictable order.
2. Once requests are serialized through one lane, switching which model
   that lane is talking to should be something Squid actively drives, not
   something it just waits out. Ollama's own idle-eviction (default ~5
   minutes) is passive — it works, but it means the previous model's VRAM
   isn't freed until a timer fires, which is slow and non-deterministic
   exactly where Squid now has enough visibility (one FIFO lane per
   physical resource) to do better. This was the original ask that started
   this ADR: switching agents should feel like "turn off the old model,
   turn on the new one," not "wait a few minutes and hope."

Explicitly out of scope, same as the original draft: *unattended* automatic
`ollama pull`/model-download management (see "Amendment: user-initiated
pull/remove" below for what's now in scope instead), and
model-comparison/eval/stats tooling across checkpoints.

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

**Path B (optional, secondary; removed 2026-08-09, see Consequences): a
standalone chat-only `ollama` harness.**
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
    parallel: false
```

Reusing `auth.type: subscription` was considered — `sync_pi_provider`
forwards `base_url` unconditionally regardless of auth type, so
`subscription` would work mechanically for Path A. Rejected anyway for
correctness of intent: `subscription` means "the harness CLI handles its
own browser login" everywhere else it's used (ADR-0028); labeling a local,
credential-free daemon that way would mislead anyone reading `squid.yaml`
into expecting a login flow that doesn't exist. This ADR adds a third
`SUPPORTED_AUTH_TYPES` value, `none` (`agent/providers.py`).
`missing_secrets()` already only fires for `api_key`, so `none` needs no new
logic there — just acceptance as a valid enum value.

`auth.type` is purely about credentials — it has no bearing on queueing.
See "Provider-scoped queueing" below for the (separate) `parallel` field
that actually drives serialization.

### Pi's custom-provider auth requires *some* key, even for `none`

Discovered while wiring Path A up against a real Ollama install: pi's
custom-provider auth composer (`@earendil-works/pi-coding-agent`'s
`provider-composer.js`) has no "no auth" concept. For any provider it
doesn't natively recognize, it throws `"Provider is not configured"`
unless it can resolve an `apiKey` or `oauth` — there's no third option.
Writing no `apiKey` field into `~/.pi/agent/models.json` for an
`auth.type: none` provider (the naively "correct" thing to do) means pi
refuses the request before it ever reaches Ollama, even though Ollama
itself never checks the `Authorization` header at all.

The fix, in `agent/resolve.py`: `sync_pi_models_store()` gained a
`placeholder_key` parameter, set from `sync_pi_provider()` via
`self.provider.auth_type == "none"`. When true, it still writes the
`$SQUID_PI_<PROVIDER>_API_KEY` reference into `models.json` even though
there's no real credential. `execution_env()`'s pi branch then sets that
env var to the literal string `"none"` whenever there's no resolved
`api_key` and `auth_type == "none"` — any non-empty placeholder satisfies
pi's auth composer, and Ollama ignores the header content entirely. This is
the one place `auth.type == "none"` still drives behavior beyond labeling;
everywhere else (queueing, load management) it's `parallel` that matters.

### Provider-scoped queueing, keyed by provider name, driven by `parallel`

A new `parallel: bool` field on `Provider` (`agent/providers.py`, default
`True`) drives this — deliberately **not** derived from `auth.type`. An
earlier version of this decision piggybacked serialization on
`auth.type == "none"`, reasoning that "local, credential-free daemon"
already implies "single physical resource." That conflated two independent
properties: how a provider authenticates says nothing about how much
concurrency its backend can actually serve. A multi-GPU Ollama box (or one
tuned with `OLLAMA_NUM_PARALLEL`) can genuinely handle concurrent requests
despite having `auth.type: none`; conversely a shared self-hosted endpoint
sitting behind an `api_key` could still be one physical resource that needs
serialization. `parallel` names the actual property instead of inferring it
from an unrelated one, and can be set independently of `auth.type` in
either direction.

`topic_queue.py`'s key derivation (`TopicDispatcher.dispatch`) gains one
conditional: when a resolved agent's provider has `parallel == False`, the
queue key becomes `f"provider:{provider_name}"` instead of
`f"{topic}@{agent}"` — collapsing *every* agent, topic, and harness that
resolves to that provider into one `TopicWorker`, i.e. one FIFO lane.
Providers with `parallel: true` (the default) keep today's per-`(topic,
agent)` parallel behavior, unchanged. Because Ollama's own default is a
single-GPU box that can't serve concurrent requests without thrashing, the
shipped `ollama` provider entry sets `parallel: false` explicitly in
`config/squid.yaml.example` — there's no implicit default tied to
`auth.type` anymore, so this has to be spelled out in config rather than
inferred.

**Adhoc dispatches are included, not exempted.** Adhoc prompts normally get
their own ephemeral `TopicWorker` and run fully in parallel
(`self._adhoc_counter`), which is correct for hosted APIs. But an adhoc
prompt against a `parallel: false` provider would otherwise bypass the
shared lane entirely and contend with whatever session traffic is already
queued on that same physical resource — exactly the thrashing this ADR
exists to prevent. So the `parallel`-driven provider-scoped check runs
*before* the adhoc branch in `dispatch()`: adhoc requests against a
`parallel: false` provider join the same `provider:{name}` lane as session
traffic; only providers with `parallel: true` still give adhoc its
always-parallel, never-queued ephemeral worker.

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
`queue_depth()`/`position_of()` (`agent/topic_queue.py`, `agent/server.py`)
need no changes — they already report position within whatever worker owns
a key; only the key derivation gains this `parallel`-conditional branch.

### Load-state visibility

Independent of which path dispatches the request, `topic_queue.py` can
check Ollama's `GET {base_url}/api/ps` (resident-models list) right before
handing a queued item to its runner. If the target model isn't resident,
emit an SSE event before dispatch — same channel/shape as the existing
`sse_event("queued", ...)` position event, new event type `"loading"`
carrying the model name, so the UI can show "Loading qwen25..." instead of
an unexplained stall. This is gated on the provider's `parallel == False` —
the same providers where cold-load latency and hardware contention are
real — rather than being harness-specific, so a Path-A (Pi) call gets the
same visibility as a Path-B (chat-only) call. No polling loop is needed to
detect "finished loading": the request itself blocks through the load, and
the first streamed byte (Path B) or Pi's first protocol event (Path A) is
the natural "now warm" signal. The `/api/ps` check itself is
Ollama-specific; if a different local-model server joins this mechanism
later under its own `parallel: false` provider entry, it would need its own
load-check implementation, not a shared one — not a problem this ADR needs
to solve now with only one local backend in scope.

### Active load/unload on model switch

The main feature this ADR adds on top of queueing: don't just wait on
Ollama's idle timer, actively free the outgoing model when the lane is
about to switch to a different one.

Each `TopicWorker` serving a provider-scoped (`parallel == False`) key
already processes its queue strictly in order (that's the queueing decision
above), so it's the one place that can track, in memory, "which model did
this lane last dispatch." When the next queued item targets a *different*
model than the last one, the worker sends an explicit unload for the
outgoing model before handing the new item to its runner — Ollama supports
this directly via `keep_alive: 0` on a request naming that model (`/api/chat`
or `/v1/chat/completions`). This is a small addition to
`TopicWorker._process`/`_sync_local_model` (`agent/topic_queue.py`), not a
new component: the same function that already decides whether to emit the
`"loading"` event (above) is the one that compares outgoing vs. incoming
model and fires the unload. The result is deterministic: the old model's
memory starts freeing before the new one starts loading, instead of
sometime in the next five minutes.

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

## Amendment (2026-08-09): default provider at install, user-initiated pull/remove

Two follow-ups, added the same day as Path B's removal above, once Ollama
moved from "supported if you hand-edit `squid.yaml`" to "offered by default."

**`bin/install.sh` gates pi/opencode's default provider on the `ollama`
binary.** `config/squid.yaml.example`'s `ollama:` provider entry ships
uncommented now (previously commented-out like the `local` example above
it) — always present in the shipped config, inert unless something resolves
to it, since presence alone doesn't select it as anyone's default. The
`harnesses.opencode.default_provider` / `harnesses.pi.default_provider`
lines still read `nvidia` in the example file itself (the safe zero-binary
default); `bin/install.sh` checks `command -v ollama` and, only when found,
rewrites those two lines to `ollama` in the copy it writes to
`~/.squid/squid.yaml` for a *fresh* install. An existing `~/.squid/squid.yaml`
is never touched, so this changes only what a brand-new install defaults to,
never a running user's config. `agent/providers.py` gained a small,
provider-id-keyed parallel to `harnesses.py`'s `_HARNESS_PATHS`/
`_HARNESS_INSTALL` — `_PROVIDER_BINARY_PATH`/`_PROVIDER_INSTALL` — so
`GET /health`'s `providers` payload carries `installed`/`install_cmd` for
`ollama` the same way it already does for harnesses, which is what the
settings catalog's Install button (below) reads.

**User-initiated pull/remove reuses the login PTY mechanism (ADR-0035),
narrowing rather than reversing the "no automatic pull" line above.** The
distinction that stays intact: *unattended* pulling (e.g. auto-pulling a
model at agent-creation time with no confirmation) is still out of scope;
a user clicking a button for one specific, pre-curated model is not the
same risk and is now in scope. `agent/auth_sessions.py`'s `create_session`
gained a `mode` parameter (`login` | `install` | `pull` | `remove`) alongside
the existing harness-login argv construction:
- `install` covers both harness install one-liners (`_HARNESS_INSTALL`,
  already existed for the settings catalog's copy-to-clipboard command) and
  the `ollama` provider's own install one-liner
  (`_PROVIDER_INSTALL["ollama"]`) — same `sh -c <fixed string>` shape,
  same "never built from user input" invariant the module's docstring
  already commits to for login.
- `pull`/`remove` run `ollama pull <model>` / `ollama rm <model>` as a plain
  argv list (`[OLLAMA_PATH, action, model]`, no shell involved), where
  `model` is checked server-side against the `ollama` provider's configured
  `models:` list before the PTY ever spawns — a client can only ever trigger
  one of the names an admin already put in `squid.yaml`, never an arbitrary
  string, so this can't become an unbounded-download or delete-anything
  vector regardless of what a request body claims.

The settings UI's harness and provider catalogs (`renderHarnessesCatalog`/
`renderProvidersCatalog`, `ui/app.js`) both gained an Install button next to
the existing copy-to-clipboard command for anything reporting
`installed: false`; the `ollama` provider row additionally lists its
configured models with Pull/Remove buttons when the binary is present. All
three reuse `openAuthPanel`'s existing xterm.js/SSE plumbing unchanged
(ADR-0035) — only `mode`/`model` are new — including `ollama pull`'s native
layered progress output, which renders directly in the panel instead of a
generic spinner. There is deliberately no free-text model field anywhere in
this UI, matching the curated-list-only design above. Not done in this pass:
live per-model on-disk/resident state (`GET /api/tags` / `GET /api/ps`,
referenced above for the `"loading"` SSE event) — the Pull/Remove buttons
work whether or not a model is already present, but the catalog doesn't yet
show which models already are; left for a follow-up rather than blocking
this amendment on it.

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
  (implying a login flow that doesn't exist). It also still gates one
  narrow behavior: whether `sync_pi_provider`/`execution_env()` write a
  placeholder API-key value, needed only because pi's own custom-provider
  auth composer has no "no auth" concept (see "Pi's custom-provider auth
  requires *some* key" above).
- New `parallel: bool` field on `Provider` (default `True`), independent of
  `auth.type`. This *replaces* an earlier version of this ADR that
  piggybacked serialization on `auth.type == "none"` with no new field —
  that conflated "how does this provider authenticate" with "can this
  provider's backend serve concurrent requests," two properties that don't
  actually move together (a multi-GPU local box wants `parallel: true`
  despite `auth.type: none`; a shared api_key-auth endpoint might want
  `parallel: false`). `agent/topic_queue.py`'s queue-key derivation keys off
  `parallel == False`: those providers share one FIFO lane by provider name,
  regardless of topic, agent, harness, or adhoc-ness. Providers with
  `parallel: true` (the default) are unaffected — session traffic keeps
  per-`(topic, agent)` parallel lanes, adhoc keeps its always-parallel
  ephemeral workers. Multi-instance local setups (two GPUs, two daemons)
  still parallelize for free, since each is its own provider entry/name.
  Because there's no implicit default derived from `auth.type` anymore,
  `config/squid.yaml.example`'s `ollama` entry sets `parallel: false`
  explicitly.
- Adhoc dispatches against a `parallel: false` provider now join the shared
  provider-scoped lane instead of getting an always-parallel ephemeral
  worker — closing a gap where adhoc traffic could otherwise thrash a local
  daemon that session traffic was correctly being serialized against.
- New SSE event type (`"loading"`) alongside the existing `"queued"` event,
  gated on the provider's `parallel == False`; the chat/status UI needs a
  handler for it. Additive to the event stream, no protocol break for
  existing consumers.
- `TopicWorker` gains a small piece of in-memory state per provider-scoped
  lane — the last model it dispatched — used to decide when to emit an
  explicit `keep_alive: 0` unload call for the outgoing model before
  switching. This is the active-swap behavior that was the original goal
  of this whole ADR; providers with `parallel: true` are unaffected, and no
  separate scheduler/background process is introduced — it's driven
  entirely by the existing dispatch loop.
- The `"loading"` event's payload grows to carry both `to` and an optional
  `from` model name, so a switch renders as one state ("Switching qwen25 →
  qwen30...") instead of a separate unload notice plus a separate load
  notice.
- Path B (standalone `ollama` harness + `run_ollama` runner) shipped
  alongside Path A but was removed on 2026-08-09: Ollama is a provider
  (an API endpoint + auth shape), not an execution mechanism, and every
  real use case reaches it through an existing harness's tool-use loop
  (Path A) rather than a bespoke chat-only runner. Removing it is exactly
  the "can be dropped entirely without affecting Path A" case anticipated
  above — `agent/harnesses.py`'s fixed harness set no longer has an
  `ollama` entry, `run_ollama` is gone from `agent/runners.py`, and the
  `ollama` provider's `supported_apis` in `config/squid.yaml.example`
  dropped `/native/ollama` (that API tag existed solely to make the
  provider "compatible with" the now-removed harness).
- Explicitly out of scope, left for later ADRs if pursued: *unattended*
  automatic `ollama pull`/model-download management (user-initiated
  pull/remove via a button is in scope — see "Amendment" above), any new
  tool-use loop beyond what Pi (or another harness) already provides, and
  model-comparison/eval/stats-diffing across checkpoints.
- Amendment (2026-08-09): `bin/install.sh` now points pi/opencode's
  `default_provider` at `ollama` for a fresh install when the `ollama`
  binary is detected (never for an existing `~/.squid/squid.yaml`), and the
  settings catalogs gained Install/Pull/Remove buttons reusing ADR-0035's
  login PTY mechanism, gated by a curated `models:` allowlist server-side —
  see "Amendment: default provider at install, user-initiated pull/remove"
  above for the full reasoning.
- Implemented: `auth.type: none`, the `parallel` field and its queueing/
  load-visibility/active-unload behavior, adhoc inclusion in the
  provider-scoped lane, and Path A's placeholder-apiKey handling for pi are
  all shipped in `agent/providers.py`, `agent/resolve.py`, and
  `agent/topic_queue.py`, with test coverage in `tests/test_topic_queue.py`.
  Path B (the standalone `ollama` harness/runner) shipped and was later
  removed — see the note above. Only Path A is live today.
