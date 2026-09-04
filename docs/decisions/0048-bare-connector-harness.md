---
status: proposed
date: 2026-09-04
---
# ADR-0048: Bare Connector Harness

## Context and Problem Statement

Every harness Squid supports today (`claudecode`, `codex`, `cursor`,
`opencode`, `pi`) works the same way: shell out to a CLI binary and let that
binary talk to the provider. The one exception, `echo`, is a test-only stub
gated behind `SQUID_TEST_HARNESS` — it makes no network call at all and
exists purely to exercise Flow's dispatch topology (fan-out/fan-in,
attribution, race timing) cheaply and deterministically.

There is recurring interest in a third shape: a real model call, made
directly against a provider's API, with no CLI subprocess, no tool schema,
and (per ADR-0046's connector-class model) no optional context layers by
default — for cheap, fast, read-only-safe requests where a full coding-agent
CLI's tooling and context overhead is unwanted cost, not a feature.

The natural-seeming shortcut — let `echo` make a real call when a `model` is
configured on it — was considered and rejected during design. `echo` is
labeled and rendered in the UI as a test harness; conditioning real network
calls and real cost on a field's presence, inside a component whose identity
signals "no-op," is a hidden behavior fork that a user can trip into
unknowingly. ADR-0046 lists "deterministic, auditable" behavior as a decision
driver for exactly this reason. The two use cases — zero-cost dispatch-graph
testing, and a cheap real model call — must stay on separate, explicitly
named harnesses.

## Decision Drivers

- A harness's identity/label must not lie about whether it makes real,
  billable network calls.
- Reuse the existing provider registry and harness/protocol machinery
  (ADR-0024, ADR-0028) rather than inventing a parallel config surface.
- Ship the smallest useful version first: no UI work, no new persistence.

## Considered Options

### Option A: Conditional behavior inside `echo`

Add a `model` field; when set, relay the prompt to that model's provider
instead of echoing it locally. Rejected — see Context above: overloads a
test-only identity with real-cost behavior, and there is no clean way to
signal the mode switch to a user who only sees the agent's name.

### Option B: New `bare` harness

A distinct entry in `SUPPORTED_HARNESSES` that performs a direct provider API
call (HTTP, not a CLI subprocess), carries no tool schema, and defaults to
ADR-0046's `bare` connector-class context eligibility (runtime + user request
layers only). Chosen.

## Decision Outcome

Adopt **Option B**.

- **Execution model:** `bare` is the first harness that does not shell out to
  a CLI. It performs a direct request against a provider's existing
  documented API surface (matched via the current `supported_apis`
  intersection used by `_compatible_providers_for`), the same way
  `opencode`/`pi`'s underlying CLIs already do internally — Squid just does
  it itself instead of delegating to a subprocess.
- **No tool schema:** `bare` never advertises or accepts tool calls. This is
  both the token-savings mechanism and a safety property — a connector with
  no tools cannot take side-effecting actions, matching a "readonly" use
  case.
- **Context eligibility:** per ADR-0046's connector-class model, `bare`
  defaults to layers 1 and 5 (runtime + user request) only. Layers 2–4
  (global context, topic memory, request context) are opt-in per agent
  config, decided at harness-resolution time, not per turn.
- **Config surface:** YAML-only for v1 (`squid.yaml` agent + provider
  entries), following the existing precedent that advanced harness settings
  (e.g. interactive idle timeout, `harnesses.py:196-215`) ship without a UI
  control first. No new UI surface is in scope for this ADR.
- **Relationship to `echo`:** unchanged by this decision. `echo` stays the
  zero-cost, `SQUID_TEST_HARNESS`-gated dispatch-topology stub; `bare` is an
  always-available (not test-gated) real connector. Both are `bare`-class
  under ADR-0046 for context-eligibility purposes, but only one of them
  makes a network call.

## Consequences

- Good: a cheap, tool-less, low-context connector becomes available without
  compromising `echo`'s test-only identity or requiring a full CLI install.
- Good: reuses the existing provider/harness resolution machinery; no new
  config format.
- Good: no tool schema is also a safety property, not just a cost one.
- Bad: Squid now owns a second execution path (direct HTTP) alongside
  "shell out to a CLI," which is new maintenance surface (auth, retries,
  streaming) that the CLI wrappers previously got for free from the CLI
  itself.
- Neutral: v1 ships YAML-only; a UI affordance to create/select a `bare`
  agent is a follow-up, not blocked by this decision.

## Related Decisions

- ADR-0022 defines the supported execution protocols that `bare` adds a new
  member alongside (it does not fit `oneshot-cli`/`interactive-cli`/
  `interactive-pty` as defined, since there is no CLI subprocess at all).
- ADR-0024 defines configurable backends and coded drivers; `bare` follows
  the same harness/provider separation.
- ADR-0028 defines harness/provider separation, whose `supported_apis`
  intersection mechanism `bare` reuses for provider compatibility.
- ADR-0046 defines the connector-class layer-eligibility model `bare` is the
  first real (non-test) member of.
