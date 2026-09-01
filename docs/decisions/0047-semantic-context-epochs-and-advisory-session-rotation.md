---
status: proposed
date: 2026-09-01
---
# ADR-0047: Semantic Context Epochs and Advisory Session Rotation

## Context and Problem Statement

ADR-0001 makes a native resumable CLI session the primary path for a
`(topic, agent)` lane. This preserves the agent's real conversation state and
lets the underlying CLI/provider reuse its normal session and prompt-cache
mechanisms. The trade-off is that context grows until the user explicitly runs
`/clear` or a backend-native compaction command. Over a long session, old turns
can become expensive, irrelevant, contradictory, or actively distracting.

The existing session advisory uses idle time and ten-turn buckets to ask whether
the user still needs the accumulated context. These signals identify pressure,
not relevance: a five-minute, thirty-turn debugging sequence may need all of its
state, while a one-hour-old session may still be exactly the task the user is
continuing. Conversely, a semantically similar prompt can explicitly invalidate
the prior approach ("redesign this from scratch"), and a lexically different
prompt ("now test it") can depend completely on it.

Topic names do not solve this alone. A durable topic such as `#squid` naturally
contains many coherent subtasks over time. Changing agents also creates a new
native session even when the underlying user task remains continuous. Squid
therefore needs a context lifecycle below the user-visible topic and above the
opaque native CLI session.

Two tempting extremes are both insufficient:

1. Keep every conversation resumable forever and leave all clearing to the
   user. This retains continuity but accumulates stale context and cost.
2. Make every turn adhoc, query SQLite/memory on every prompt, and reconstruct a
   synthetic context. This gives Squid complete control, but repeatedly pays
   retrieval and prefill costs, forfeits native working state, and makes every
   ordinary continuation depend on retrieval quality.

This ADR decides whether Squid should introduce semantic context epochs, how
they relate to native sessions, and how far automation may go before real usage
data demonstrates that automatic rotation is safe.

## Terminology

- **Transcript**: the immutable complete chat history stored by Squid.
- **Topic**: the user-visible durable workstream, such as `#squid`.
- **Lane**: a persistent `(topic, agent)` execution identity from ADR-0002.
- **Context epoch**: a topically coherent interval within a topic. An epoch may
  span agents, but each participating agent still owns a distinct native
  session.
- **Working context**: the effective context currently held by a native CLI
  session.
- **Epoch summary**: a mutable, compact description of the epoch's objective,
  current state, decisions, constraints, unresolved work, and evidence links.
- **Rotation**: ending use of one native session and starting a fresh one.
- **Reconstruction package**: selected durable context injected into the first
  turn of a fresh session.
- **Drift**: evidence that the new request is less coherent with the active
  epoch than with another or a new epoch. Drift is a signal, not a command.

## Decision Drivers

- Preserve native resumable sessions for short and coherent work.
- Reduce stale-context and repeated-input cost in long or drifting sessions.
- Carry relevant context across `/clear`, agent changes, and resumed old work.
- Keep the complete transcript as the source of truth; summaries and vectors
  are derived indexes that can be regenerated.
- Avoid silently losing context because of an incorrect classifier decision.
- Work locally and offline by default, with no hosted memory service required.
- Keep ordinary-turn latency negligible; do not invoke a generative model on
  every prompt by default.
- Make every recommendation and reconstruction auditable back to message IDs.
- Measure value against actual token/cache behavior before enabling automation.
- Respect ADR-0045's separation between agent routing and context lifecycle.
- Respect ADR-0046's single authoritative context assembler and audit model.

## Prior Art and Findings

### MemGPT / Letta: virtual context management

[MemGPT](https://arxiv.org/abs/2310.08560) treats the context window as scarce
working memory backed by larger recall and archival tiers. Modern
[Letta](https://github.com/letta-ai/letta) exposes core memory, recall memory,
archival memory, and explicit context accounting.

The transferable idea is hierarchical memory, not agent-controlled eviction.
Letta owns its model request and can edit the active context. Squid delegates
context ownership to opaque CLI agents and generally cannot delete or reorder
messages within an existing native session. Squid's portable boundary is the
whole session: preserve it, or rotate it and reconstruct a fresh request.

### LangGraph / LangMem: thread state versus cross-thread memory

[LangGraph persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx)
separates thread-scoped checkpoints from durable cross-thread stores.
[LangMem](https://github.com/langchain-ai/langmem) adds background extraction,
consolidation, update, and semantic retrieval.

This supports keeping a native session disposable while topic/epoch state is
durable. It also shows that memory cannot be append-only: decisions change,
tasks complete, and newer evidence supersedes older facts.

### Segment-level conversational memory

[SeCom](https://arxiv.org/abs/2502.05589) reports weaknesses in turn-level,
session-level, and summary-only memory units and instead constructs topically
coherent conversation segments. Dialogue segmentation research similarly finds
that pairwise coherence is stronger than surface similarity alone; see
[Xing and Carenini](https://aclanthology.org/2021.sigdial-1.18/) and the
summary/intent/topic-shift decomposition in
[Def-DTS](https://aclanthology.org/2025.findings-acl.1066/).

This motivates an epoch below `#topic`, rather than equating a topic, session,
or fixed number of turns with one coherent task.

### Retrieval granularity, time, and updates

[LongMemEval](https://arxiv.org/abs/2410.10813) evaluates extraction,
multi-session reasoning, temporal reasoning, knowledge updates, and abstention.
It finds value in session decomposition, fact-augmented keys, and time-aware
query expansion. [RAPTOR](https://arxiv.org/abs/2401.18059) retrieves across a
hierarchy of source chunks and recursive summaries.

For Squid, retrieval units should include exact exchange evidence as well as
epoch summaries. Every derived memory must retain time, topic, agent, session,
and message provenance. A retrieval system must be able to decline injection
rather than always return the least-bad match.

### Long context and compression

[Lost in the Middle](https://arxiv.org/abs/2307.03172) shows that models do not
use all positions in a long context equally well. More context is not
automatically better. [LLMLingua](https://arxiv.org/abs/2310.05736) demonstrates
prompt compression, but token-level compression is risky for code-agent inputs
where paths, commands, error text, and exact constraints matter.

Squid should first select fewer relevant segments. It may summarize prose, but
must preserve exact evidence separately when exactness matters.

### KV-cache eviction is a different layer

[StreamingLLM](https://arxiv.org/abs/2309.17453),
[H2O](https://arxiv.org/abs/2306.14048),
[SnapKV](https://arxiv.org/abs/2404.14469), and
[Infini-attention](https://arxiv.org/abs/2404.07143) manage attention or KV
state inside a compatible model runtime. These techniques require inference
control and optimize memory/throughput; they do not determine whether an old
decision remains authoritative or transfer task state between unrelated CLI
agents.

Semantic context management remains necessary even if a future local Squid
runtime supports true sliding-window attention or KV eviction.

## Considered Options

### Option A: Continue native sessions until the user manually clears

Good: no new inference, storage, retrieval, or policy machinery; maximum native
continuity; no false automatic rotations.

Bad: leaves the known growth problem in ADR-0001 unresolved; users must infer
context pressure themselves; no structured handoff across agents or after a
clear; stale instructions remain resident.

### Option B: Make every turn adhoc with automatic retrieval

For every request, query SQLite/topic memory, retrieve and summarize history,
then send a stateless synthetic prompt.

Good: one context mechanism across all harnesses; exact prompt budget control;
easy cross-agent and cross-topic retrieval; native session IDs become optional.

Bad: discards native session continuity and implicit working state; performs
retrieval and reconstruction on every ordinary continuation; repeatedly
prefills a changing synthetic prefix; may reduce provider cache reuse; adds
latency and new failure modes; makes answer quality depend on retrieval recall
even when the immediately preceding turn was sufficient.

### Option C: Rotate automatically when embedding similarity crosses a threshold

Good: cheap, local, and superficially simple.

Bad: similarity is not task dependency. Testing an implementation may look
different while depending on it; restarting the same design may look similar
while invalidating it. A single threshold is uncalibrated across topics,
languages, prompt lengths, and embedding models. False rotation is asymmetric:
carrying some stale context is usually recoverable, while silently dropping
needed native context may not be.

### Option D: Hybrid native sessions plus semantic epochs and advisory rotation

Continue the native session in the normal case. Maintain durable epoch metadata
and derived summaries in the background. Use deterministic pressure signals,
semantic/coherence features, and retrieval to recommend rotation or resumption.
When rotation is accepted, create a fresh native session and inject an audited,
budgeted reconstruction package.

Good: preserves the reliable and potentially cache-efficient common path;
improves long-session and cross-agent continuity; can be introduced in shadow
and advisory modes without risking context loss; derived data can be rebuilt
from the transcript.

Bad: requires segmentation, summarization, retrieval, UI, and evaluation
machinery; a poor reconstruction may still omit information; benefits depend
on real session lengths and provider cache economics.

## Decision Outcome

Adopt **Option D**, incrementally and with automation explicitly deferred.

Native resumable sessions remain the default execution mode. Squid does not
convert all chat to adhoc retrieval and does not run retrieval on every normal
continuation. Context epochs are a durable semantic index over transcript
history, not a replacement transcript and not a new user-visible route.

The initial product is **advisory**:

- detect pressure and possible boundaries;
- prepare an explainable recommendation;
- let the user continue, start fresh with selected context, or resume a related
  epoch;
- record what the user chose;
- do not automatically issue `/clear`.

Automatic rotation requires a later decision backed by measured false-rotation
rates and user acceptance data. This ADR does not authorize it.

## Context Model

Squid uses these conceptual tiers:

```text
L0  immutable transcript and exact artifacts
L1  coherent exchanges and context epochs
L2  mutable epoch summaries and state
L3  durable topic memory and accepted decisions
L4  native agent working session
```

L0 is authoritative. L1-L3 are derived or user-editable durable state. L4 is a
disposable projection optimized for the current work.

### Epoch identity and scope

An epoch belongs to one user-visible topic and may reference turns from
multiple agents. Agent-native sessions remain scoped to `(topic, agent)` as
required by ADR-0002. The association is many-to-many:

```text
#squid / epoch 17 "semantic context management"
  - codex session C1
  - claude session A4
  - adhoc review turn R9
```

Changing agents need not create a new epoch. Starting a materially different
task within the same `#topic` may create one. Epoch creation does not rename,
move, hide, or delete the user's topic.

### Epoch state

The logical schema contains at least:

```text
epoch_id
topic
title/summary
status: active | dormant | completed | superseded
started_at, last_at
first_message_id, last_message_id
objective
accepted decisions and constraints
unresolved work
entities/files/code roots
source message IDs and revisions
embedding model/revision (when indexed)
```

Exact schema and migration details are deferred to implementation planning.
Summaries are replaceable derived state and must never overwrite transcript
content.

## Boundary and Continuity Signals

The detector produces evidence for a context action; it does not directly
execute `/clear`. Candidate signals include:

- similarity of the new request to the active epoch summary;
- similarity to recent individual exchanges;
- whether another recent epoch is a substantially better match;
- adjacent-turn coherence;
- explicit continuation language ("that", "continue", "now test it");
- explicit reset or shift language ("separately", "from scratch", "new
  question", "ignore the previous approach");
- referenced files, repositories, code roots, messages, and artifacts;
- overlap with unresolved objectives;
- idle duration;
- live session turn count and available token/context utilization;
- agent, model, harness, or cwd changes;
- whether relevant information has already been consolidated into durable
  epoch state.

No single signal is sufficient. Time and turn count represent pressure;
embeddings represent semantic affinity; explicit language and unresolved task
state represent dependency.

The detector's action vocabulary is:

```text
CONTINUE
CONTINUE_WITH_RECALL
FRESH_SAME_EPOCH
NEW_EPOCH_SAME_TOPIC
RESUME_RELATED_EPOCH
SUGGEST_OTHER_TOPIC
UNCERTAIN
```

Agent selection remains governed by explicit routes and, if accepted and
implemented, ADR-0045. Context action and agent routing are separately logged.

## Local Inference and Retrieval

### Fast path

The fast path uses deterministic features plus local embeddings. A small
generative model is not required on every prompt. The initial implementation
may use:

- SQLite FTS5/BM25 over transcript and epoch summaries;
- a local sentence-embedding model;
- cosine similarity for dense recall;
- reciprocal-rank fusion or a similarly simple combination of dense and
  keyword results;
- deterministic recency, provenance, status, file, and topic reranking.

The embedding provider follows ADR-0043's local-provider abstractions where
practical. Vector storage should remain local and colocated with SQLite for the
initial corpus size; no external vector database is required by this ADR.

### Ambiguous path

A small local instruct model may classify only an uncertainty band after the
cheap signals run. Its output must use a validated finite schema containing an
action, confidence, relevant epoch IDs, and short reason. Invalid or timed-out
output becomes `UNCERTAIN`, never a rotation.

The local model is optional. The system must function as a rules/embedding
advisory without it.

### Indexing granularity

Index at more than one level:

- coherent exchange or small turn segment for exact evidence;
- epoch summary for task-level affinity;
- topic memory for durable user-owned constraints.

Do not embed arbitrary concatenations of the entire topic. Every result retains
message/epoch provenance and timestamp. Superseded or completed records remain
searchable but are reranked or labeled rather than silently erased.

## Reconstruction Package

On a user-approved rotation, Squid builds a budgeted package through the single
context assembler established by ADR-0046. It normally contains:

1. required runtime context;
2. global user context and selected topic memory;
3. active/recovered epoch objective and compact state;
4. accepted decisions, constraints, and unresolved work;
5. a small number of retrieved exact evidence segments;
6. the most recent exchanges needed for conversational continuity;
7. the current user request.

Stable context goes near the beginning, exact retrieved evidence is clearly
labeled, and the current task remains at the end. Every derived assertion links
to source message IDs. Exact paths, commands, errors, diffs, and user
instructions should be quoted or attached as evidence rather than compressed
into lossy prose.

The package is recorded as delivered context under ADR-0046's audit contract.
Injection tracking is tied to the new native `session_id`, consistent with
ADR-0007 and ADR-0009.

Rotation must not occur until the package is ready. If summarization, retrieval,
or assembly fails, Squid leaves the existing session intact and reports that a
fresh-context recommendation could not be prepared.

## User Experience and Safety Policy

The existing advisory evolves from a turn/time-only prompt into an explainable
context recommendation. Example:

```text
Fresh context recommended
28-turn session · 4-day gap · this request matches "context management"
more than the active "PWA cache" epoch

[Continue current] [Fresh + selected context] [Review context]
```

The UI must show:

- proposed action and destination epoch/topic;
- pressure and continuity reasons;
- what summary, memories, and messages will be injected;
- estimated context size when available;
- an option to continue without dismissing the underlying history permanently.

No system modal is used. The common themed inline advisory/context surface is
the interaction point.

Dismissals and choices are local behavioral evidence. They do not rewrite the
transcript or silently become training labels for a self-reinforcing model.

## Observability and Evaluation

Every detector evaluation should be loggable in shadow mode with:

```text
topic, agent, native session_id, active epoch_id
candidate action and candidate epoch IDs
feature/model revisions
component scores and final confidence
pressure metrics
recommendation shown/dismissed/accepted
actual subsequent user action
rotation and reconstruction IDs
input, cache-read, and output token observations when available
latency
```

Raw private message text need not be duplicated into an analytics table; IDs
link back to the authoritative transcript.

Evaluation uses time-ordered replay: each historical prompt may use only state
available before that prompt. Explicit topic routes, `/clear`, agent changes,
long gaps, replies, pins, and user corrections are weak labels, not automatic
ground truth.

Primary metrics are:

- **false-rotation rate**: fresh context recommended/started when continuation
  required omitted native state;
- boundary precision/recall on a manually labeled sample;
- related-epoch retrieval recall;
- evidence recall in reconstruction packages;
- recommendation acceptance and immediate undo/correction rate;
- latency added to ordinary turns;
- input and cache-read token deltas before versus after rotation;
- answer/task success proxies before versus after accepted rotation.

False rotation is the safety metric. Overall classification accuracy can hide
rare but damaging context loss.

## Feasible Milestones

### Milestone 0: Instrument the current system

No context behavior changes.

- Measure session length, idle gaps, `/clear` locations, agent switches,
  search/pin use, input/cache-read tokens by live turn index, and advisory
  dismissal/acceptance.
- Add a reproducible history replay/export fixture with private text retained
  locally.
- Establish whether long sessions and cache economics are material enough to
  justify later milestones.

Exit criterion: a baseline report can quantify where rotation might save cost
or quality and identify a representative local evaluation set.

### Milestone 1: Epoch records with manual boundaries

- Add durable epoch metadata without embeddings.
- Allow a user to start a new epoch inside the same visible topic or associate a
  turn with an existing epoch.
- Preserve all existing session and route semantics.
- Display epoch membership only in context/history details initially.

Exit criterion: epochs survive restart, are auditable to exact messages, and do
not change prompt execution by themselves.

### Milestone 2: Deterministic rolling epoch state

- Maintain objective, decisions, constraints, unresolved work, files/entities,
  and source IDs.
- Update at explicit boundaries or background checkpoints rather than blocking
  every send.
- Provide inspection and correction of derived state.
- Version summaries so stale embeddings and reconstructions can be invalidated.

Exit criterion: a manually rotated session can be reconstructed from epoch
state and exact selected evidence without semantic retrieval.

### Milestone 3: Local hybrid retrieval

- Add FTS5 plus local embeddings over exchange segments and epoch summaries.
- Retrieve within the current topic first, with separately authorized recent
  cross-topic suggestions.
- Add abstention/minimum-score behavior and provenance display.
- Benchmark latency and recall on the replay set.

Exit criterion: related-epoch and evidence retrieval meet predetermined recall
and latency targets; ordinary resumed turns still perform no retrieval unless a
feature requires it.

### Milestone 4: Shadow boundary detector

- Compute candidate context actions without changing UI or session state.
- Combine pressure, explicit language, semantic/coherence, task-state, and
  artifact-overlap features.
- Compare predictions to later user behavior and manual labels.
- Do not train on the detector's own predictions.

Exit criterion: measured false-rotation and uncertainty rates support exposing
recommendations; otherwise stop and retain manual epochs/retrieval only.

### Milestone 5: Advisory rotation and reconstruction preview

- Replace/extend the current generic advisory with reasons and candidate
  context.
- Implement `Fresh + selected context`, `Continue`, and context review.
- Build through ADR-0046's assembler, clear only after package readiness, and
  audit exactly what was delivered.
- Provide a direct way to recover the prior epoch/session history if the new
  context is insufficient.

Exit criterion: recommendations show meaningful acceptance, low immediate
reversal/correction, and measurable token or quality benefit.

### Milestone 6: Optional ambiguous-case local classifier

- Invoke a small local model only inside a calibrated uncertainty band.
- Require validated structured output, strict latency limits, and fail-closed
  behavior.
- Compare incremental benefit against the embedding/rules baseline.

Exit criterion: the classifier materially improves boundary precision or
related-epoch selection enough to justify model download, runtime, and support
cost. Otherwise remove it.

### Milestone 7: Reconsider automation in a separate ADR

Automatic session rotation is not an automatic consequence of completing the
earlier milestones. A new ADR must define opt-in scope, thresholds, rollback,
and acceptable false-rotation rate using observed data.

## Stop/Go Criteria

Continue beyond instrumentation only if at least one is demonstrated:

- a material share of sessions become long enough for stale context or input
  cost to matter;
- users regularly switch agents while retaining the same task;
- users frequently search, pin, or manually reconstruct earlier context;
- answer quality measurably degrades late in sessions and recovers after clear;
- accepted reconstruction reduces cost without increasing correction rate.

Stop after Milestone 0 or keep only manual tooling if most work is short,
explicit topics already segment it well, provider caching neutralizes cost, or
the detector cannot achieve a sufficiently low false-rotation rate.

## Consequences

- Good: preserves native CLI context and cache behavior in the common coherent
  case.
- Good: provides continuity across clears and agents without making every turn
  stateless RAG.
- Good: gives long-lived topics internal structure without forcing the user to
  create many tags.
- Good: transcript, provenance, and user-owned topic memory remain
  authoritative.
- Good: milestones can stop after any layer that proves useful; the design does
  not require committing to autonomous clearing.
- Good: local embeddings and SQLite are sufficient for the initial scale.
- Bad: adds derived state, indexing, summary revision, and UI complexity.
- Bad: summaries and retrieval can omit or distort critical information, so
  exact evidence and user review remain necessary.
- Bad: the value may be small for short sessions or providers with effective
  prompt caching.
- Bad: cross-agent epochs do not transfer hidden model reasoning or unrecorded
  CLI state; reconstruction can carry only observable durable context.
- Neutral: future inference-level KV-cache optimizations remain orthogonal.

## Non-Goals

- Replacing resumable sessions with all-adhoc execution.
- Editing or transferring opaque KV-cache entries.
- Treating one embedding-distance threshold as a topic-boundary oracle.
- Automatically issuing `/clear` under this ADR.
- Creating a universal ontology or full temporal knowledge graph in v1.
- Making generated summaries more authoritative than exact transcript records.
- Auto-routing agents; that remains ADR-0045's concern.
- Implementing provider-specific native compaction policies here.

## Related Decisions

- ADR-0001 keeps resumable native sessions primary and identifies context
  growth as an open problem.
- ADR-0002 scopes native session identity to `(topic, agent)`.
- ADR-0007 and ADR-0009 define session-scoped context injection tracking.
- ADR-0013 passes backend-native compaction through rather than treating it as
  a Squid-owned clear alias.
- ADR-0017 defines token counting semantics used for evaluation.
- ADR-0021 defines the existing keyword search model.
- ADR-0037 and ADR-0043 define local model/provider execution and lifecycle.
- ADR-0045 proposes local embedding-based agent resolution; its embedding
  infrastructure may be shared, but its decision output remains independent.
- ADR-0046 defines layered context assembly, delivery, and audit.
