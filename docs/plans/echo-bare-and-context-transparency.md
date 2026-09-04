# Plan: Echo fixes, the `bare` connector, and context-envelope transparency

Design-settling doc, distilled from an echo-agent brainstorm into ADR-0046
(amended) and ADR-0048 (new). This plan sequences the implementation; it does
not re-litigate the decisions those ADRs already record.

## Problem

1. **Echo is a poor stand-in for its own purpose.** `run_echo`
   (`agent/runners.py:2293`) sleeps a random 5–10s (non-deterministic,
   complicates timing-sensitive test assertions), never registers itself in
   the process table so it's invisible to the status blinker for its whole
   duration, and echoes back the fully-assembled `effective_message`
   (topic memory + pinned context + attachments), not the literal prompt —
   so chaining through it (`#topic@echo=5>@claude`) leaks envelope noise into
   the next hop instead of relaying the bare prompt.
2. **No way to verify what was actually sent.** `effective_message`
   (`agent/server.py:1092-1150`) is assembled ad hoc from Python string
   concatenation and handed straight to the runner; it is never persisted.
   The "thoughts" trace modal (`ui/app.js:15091` `openTraceModal`) only shows
   what the model *did* (tool calls, status stream), nothing about what it
   *received*.
3. **No cheap, tool-less, low-context connector exists.** Every real harness
   wraps a CLI subprocess and (for `cli`-class agents) receives the full
   context stack. There's no way to make a small, predictable, low-token
   real model call without either a CLI install or unwanted context/tooling
   overhead.

## Settled decisions (see ADRs for rationale)

- Echo stays a `SQUID_TEST_HARNESS`-gated, zero-network, zero-cost stub.
  Its fixes are small and don't need an ADR of their own.
- Context assembly becomes one authoritative assembler emitting typed,
  identified blocks across 5 layers (runtime, global, topic memory, request,
  user request) — ADR-0046.
- Harnesses/connectors belong to a class (`cli` or `bare`) that determines
  default layer eligibility. `echo` is `bare`-class (layer 5 only) but makes
  no network call; the new `bare` harness is `bare`-class and does — ADR-0048.
- Audit/trace surfacing never persists rendered layer text. User-owned layers
  link to live current content with a "changed since sent" flag on hash
  mismatch; runtime-layer blocks link to a viewer that re-renders
  `{template_id, variables}` on demand, flagged if `template_revision` has
  since changed — ADR-0046.

## Implementation sketch

### Phase 1 — Echo fixes (no ADR, ship independently)

1. `agent/runners.py:2314` — replace `asyncio.sleep(random.uniform(5, 10))`
   with a fixed `asyncio.sleep(5)`.
2. Wrap that sleep with `_register_proc(...)` / `_deregister_proc(...)`
   (synthetic pid) so echo is visible in `list_active_procs()` /
   the status blinker for its duration.
3. `agent/server.py:1023` — extend the existing
   `if native_backend_command or harness == "echo":` branch to also skip
   building `prefix_blocks` (topic memory, pinned "referenced context",
   attached files), so `effective_message` for echo is the raw `message`.

### Phase 2 — Runtime templates + single assembler (ADR-0046 core)

1. Add `runtime/global.md`, `runtime/oneshot.md`, `runtime/worktree.md` with
   validated frontmatter (`id`, `owner`, `inject`, `required`, `when`,
   `variables`) per ADR-0046.
2. Add the template loader/renderer/condition registry and the single
   server-side assembler that replaces today's ad hoc `prefix_blocks`
   construction (`agent/server.py:1092-1150`) and `_build_prompt`'s history
   wrapper (`agent/runners.py:942-958`).
3. Assembler takes connector class into account for layer eligibility
   (`cli` vs `bare`, including `echo`), resolved at harness-resolution time.
4. Persist per-turn block references only: `{id, source, revision/hash,
   inject cadence}` — no rendered text. Reuse the existing `mem`/
   `mem_revision` columns for the topic-memory layer; add equivalent small
   columns/fields for the other layers as needed.

### Phase 3 — `bare` connector harness (ADR-0048)

1. Register `bare` in `SUPPORTED_HARNESSES` / harness config
   (`agent/harnesses.py`), reusing `_compatible_providers_for`'s
   `supported_apis` intersection for provider matching.
2. Implement the direct-HTTP call path (no CLI subprocess, no tool schema).
3. Wire default context eligibility to `bare`-class (layers 1 + 5), opt-in
   config for layers 2–4.
4. YAML-only config; no UI work in this phase.

### Phase 4 — Trace/audit UI (consumes Phase 2's block references)

1. Extend `openTraceModal` (`ui/app.js:15091`) with a "Sent" section listing
   each block reference from Phase 2.
2. User-owned layers: reuse the existing live-link pattern (`memory` row →
   `openMemoryEditor`, pin preview → `ctx-pin-preview-${id}`) generalized to
   all blocks of that kind.
3. Runtime layers: new viewer that takes `{template_id, variables}`,
   re-renders server-side, and shows a "template wording has changed since
   this turn" flag when `template_revision` mismatches the currently shipped
   template.

## Verification

- Phase 1: existing dispatch-graph tests (`tests/test_runners.py`,
  `tests/test_realtime.py`) still pass with the fixed 5s delay; new
  assertion that echo appears in `list_active_procs()` during its run; new
  assertion that echo's output equals the literal `message`, not
  `effective_message`, when topic memory/pins/attachments are present.
- Phase 2: unit tests on the assembler's block output directly (no CLI/model
  needed, per this conversation's "real CLI run" clarification) — correct
  layer selection per connector class, correct cadence (`every-turn` vs
  `session-start` vs `this-request`), no duplicate injection.
- Phase 3: `bare` harness resolves compatible providers correctly; a turn
  through it carries no tool schema and only layers 1+5 by default; opt-in
  config surfaces layers 2–4 when configured.
- Phase 4: e2e (Playwright) — trace modal shows a "Sent" section matching
  what was actually assembled; editing topic memory after a turn shows
  "changed since sent" on that turn's trace without affecting other turns;
  runtime block viewer reflects current worktree/cwd variables correctly.
  Visual check of the modal on desktop and mobile widths.
