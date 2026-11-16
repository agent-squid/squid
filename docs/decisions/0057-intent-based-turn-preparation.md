---
status: proposed
date: 2026-11-15
---
# ADR-0057: Intent-Based Turn Preparation

## Context

[ADR-0025](0025-per-turn-worktree-isolation.md) captures current backing-repo
state and materializes an isolated writable directory before the agent runs.
With multiple large code roots, each turn pays preparation costs for repositories
it may never edit. Discussion turns pay these costs too. Concurrent routes
multiply filesystem pressure. Skipping promotion for unchanged turns does not
recover the work already spent preparing them.

The backing repository remains shared project state: newly started turns across
agents and subtopics must see completed syncs, including uncommitted changes.
Persistent divergent route workspaces and requiring commits for visibility would
change that behavior and are outside this proposal.

Private snapshot-index reuse was investigated and reverted after review found
that changed attributes could leave stale filtered contents in the cached tree.
Rather than add fragile cache invalidation, this proposal avoids preparation
entirely where the execution contract does not require writable isolation.

## Proposed decision

Start eligible agents with enforced read-only access to configured backing roots.
Let the executing LLM decide when editing is needed from its normal task context
and repository inspection. Squid injects instructions (a runtime skill) explaining
how to request writable isolation through a host-owned tool. There is no separate
pre-execution classifier or additional model invocation.

The skill guides behavior; the tool performs preparation; the sandbox enforces
access. These are distinct responsibilities. An editing workspace request does
not itself authorize edits or external effects.

### Compute constraints and Laya assessment

[Laya](https://github.com/NandhaKishorM/laya) is an existing Python System 1
decision engine, not a name for a new Squid component. It was considered for
mode and repository-scope classification. Its Python package brings a local
inference stack including PyTorch and Transformers. The project's advertised
33 ms encoder inference is measured on a T4 GPU; it is not a CPU-VM latency
measurement. See the upstream [package definition](https://github.com/NandhaKishorM/laya/blob/main/pyproject.toml)
and [quickstart](https://github.com/NandhaKishorM/laya#quickstart).

GPU acceleration is not a strict requirement for Laya, but CPU feasibility for
Squid's target VM has not been established. Squid must not require a GPU or add
a heavyweight resident model solely to decide whether to avoid a checkout.
Decision: defer Laya integration on compute and deployment grounds. Do not add
its dependencies or download model weights as part of this proposal.

A separate TF-IDF/linear classifier or inference service is also unnecessary for
the chosen approach. The executing agent already has conversational context and
can inspect dependencies before selecting roots. These alternatives remain
possible future experiments, not prerequisites for rollout.

### Injected skill and host tool

The injected instructions tell the agent to answer or inspect backing roots
read-only, then call the proposed tool before edits or other operations requiring
repository writes:

```text
prepare_edit_workspace(root_ids=["squid"], reason="Implement the requested fix")
```

The tool can be exposed through Squid's shared-tools interface, including MCP
where supported. MCP is a transport, not an enforcement mechanism. This is a
proposed runtime instruction and tool contract, not an installed user skill or
an existing implemented API.

The host validates turn identity, configured root IDs, current execution grants,
and request authorization. Arbitrary source or destination paths are rejected.
It captures the latest backing state and uses ADR-0025's existing preparation
pipeline for the selected roots. It returns the root-ID-to-isolated-path mapping
only after successful setup and registration for GitDiff tracking and sync.
The initial tracker baseline is captured before the agent can modify the copy.
Repeated requests for an already prepared root return the same turn-owned copy,
without resetting edits. Requests for additional roots prepare only those roots.
Partial failures must not expose an unregistered writable checkout.

| Execution state | Repository access and preparation |
| --- | --- |
| Discussion or investigation | Backing roots readable, writes blocked; no checkout, GitDiff snapshot, or sync |
| Selected roots prepared | Those isolated copies writable; backing roots and other repositories remain read-only |
| Stable snapshot requested | Prepare isolation even if no edits are planned |
| Unsupported sandbox or transition | Existing upfront isolation; no new direct-write fallback |

The normal agent context includes the original request, conversation and pending
task state, root identifiers, access mapping, and explicit constraints. Short
replies such as "go" and "revert" are interpreted through that context, not a
standalone classification call. Retrieved history remains context, not authority.
Tests, formatters, and package managers may require a writable copy even if they
leave no final diff. Forgetting the tool causes a denied write, never an automatic
grant against the backing repository.

Existing behavior when isolation is disabled is unchanged. Eligibility must be
explicitly capability-gated; this proposal does not silently introduce a new
fallback with direct backing writes.

### Enforcement and transitions

No repository preparation may be skipped solely because the prompt tells the
agent not to edit. The host needs an enforceable contract across each harness,
shell commands, file tools, and connected tools that can mutate repositories.
Read-only tools can still launch commands that create caches or build artifacts;
these writes must be blocked or redirected to disposable writable storage.
Backing paths, alternate paths, and symlink targets must not bypass restrictions.

The host's preparation and sync service runs outside the agent sandbox. Merely
changing file modes is insufficient when the agent can restore write access.
The restriction must also cover access through host-side tools and services;
a shell-only sandbox cannot protect backing files from an unrestricted file tool.
If the harness cannot enforce these restrictions, retain upfront isolation.

Two transition mechanisms are possible:

- **Same session:** a turn-private workspace area is writable from launch but
  initially contains no repository copies. The host populates requested roots
  there and returns their paths. Backing mounts remain read-only. Filesystem
  visibility and harness access policy must both support the new paths.
- **Host handoff:** where access cannot safely change mid-session, stop the
  read-only execution and launch an isolated editing step with findings and
  current authorization. Use a supported synchronous or durable continuation;
  never assume an exited CLI resumes itself. Prevent duplicate execution/effects
  on retries and do not report preparation as completed edits.

Both mechanisms update root mappings and require the agent to reread affected
files in the prepared copy: backing state may have changed since inspection.
Existing processes keep their backing paths read-only; no permission change makes
those paths writable. Root expansion repeats validation and preparation. The
agent cannot grant itself permissions or choose a different backing destination.

### Shared state and concurrent execution

Read-only inspection sees live backing state and may observe another turn's
sync between reads. This is acceptable for exploratory work that tolerates drift;
work requiring a stable view keeps snapshot isolation. Editing turns see backing
state captured during their preparation, then retain their isolated view while
other routes run. Their completed changes sync through ADR-0025 as before.

Per-root edit scope is an optimization, not a restriction on cross-repository
tasks. All configured roots can remain available for reading when authorized.
Dependent roots are isolated together when required by the request. Partial
preparation failures do not launch a turn with unintended write access.

### Authority and special execution modes

Explicit user constraints and recorded execution modes constrain workspace requests.
The tool cannot expand watch/Flow grants, grant task publication, or authorize push.
Git-only continuations retain their backing-repository rules and must not be
treated as ordinary read-only work merely because they do not edit files.
Task review/publication retains its separate recorded review and publication
requirements under [ADR-0054](0054-agent-callable-squid-capabilities-and-continuations.md).
The first rollout excludes these special modes rather than altering their
lifecycle. Workspace acquisition is separate from agent/topic routing.

## Rollout and evaluation

1. Prove backing-root write enforcement and host-owned workspace acquisition on
   the VM, covering shell and non-shell tools, child processes, and alternate paths.
2. Enable read-only starts and whole-root workspace acquisition for one supported
   harness. Inject the skill and verify both discussion-only completion and
   editing completion, including denied-write recovery and baseline capture.
3. Enable selected roots and scope expansion after cross-repository coverage.
4. Extend to other harnesses only after their sandbox and transition contracts
   pass the same checks. Unsupported harnesses retain upfront isolation.

Evaluate on the actual VM filesystem. Record filesystem/mount type, repository
file counts and sizes, Git version, and cache conditions. macOS warm-cache timings
are supplementary and cannot establish production benefit. Cover cold and warm
caches (document how cold conditions were obtained), clean and dirty repositories,
many small files, large files, multiple roots, and concurrent routes.

Measure end-to-end latency including workspace acquisition or handoff, snapshot/materialization/
integration/cleanup time, peak memory, filesystem work avoided, unnecessary
editing preparation, denied writes, unnecessary workspace requests, scope expansions,
and fallbacks.
Compare against current behavior on the same VM. Require demonstrated savings
and correct fallback before broad enablement; set rollout thresholds from those
measurements rather than inventing accuracy or latency targets here.

Correctness coverage must include contextual short replies, mixed discussion and
edit requests, tool failure, cancellation, retries/session clearing,
concurrent sync, external edits, cross-repo changes, symlink and shell bypass
attempts, write-producing tests, and recorded-grant boundaries. No rollout stage
is enabled merely by accepting this ADR.

## Consequences and alternatives

This can eliminate entire preparation stages for non-editing turns and untouched
roots while preserving shared uncommitted context. It adds a workspace-acquisition
tool, permission enforcement, and transition complexity. Enforcement is a prerequisite,
not a follow-up safety improvement. Record the decision and fallback in run
metadata so workspace requests, denied writes, handoffs, and preparation cost
are diagnosable.

Always isolating remains the conservative baseline. Prompt-only read-only mode
is rejected because instructions cannot prevent shared-file mutations. Manual execution
mode selection can provide a deterministic override, but is not required for
every message. Copy-on-write materialization, bounded display diffs, and avoiding
integration checkouts for clean merges are independent optimizations; they do
not replace this preparation decision or ADR-0025's correctness checks.
