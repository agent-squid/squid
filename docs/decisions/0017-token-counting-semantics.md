---
status: accepted
date: 2026-06-09
---
# ADR-0017: Token Counting Semantics — Claude vs Codex

## Context

Squid displays per-turn token counts and aggregates them in the analytics panel.
The two primary backends — Claude (Anthropic API via Claude Code CLI) and Codex
(OpenAI via Codex CLI) — report token usage in fundamentally different ways. We
have revisited this more than once and incorrectly "fixed" working code. This
ADR records the verified semantics so future changes are made with full context.

## Claude (Anthropic API / Claude Code CLI)

The `result` event in `--output-format stream-json` contains a `usage` object
with three input buckets:

| Field                        | Meaning                                                  |
|------------------------------|----------------------------------------------------------|
| `input_tokens`               | Uncacheable residual — typically **2–4 tokens**. The user's message is **not** here. |
| `cache_creation_input_tokens`| Tokens written to the prompt cache this turn, **including the user's message**. |
| `cache_read_input_tokens`    | Tokens served from a cache entry created in a prior turn. |

**True total tokens processed = `input_tokens + cache_creation + cache_read`**

This is counter-intuitive. A 50-token user message will show `input_tokens: 2`
because the message itself was appended to a cache block and landed in
`cache_creation_input_tokens`. This is correct behaviour, not a bug.

We verified this empirically (June 2026) by running two consecutive turns and
confirming that the second turn's `cache_creation_input_tokens` grew by the
exact token count of the new user message, while `input_tokens` stayed at 2.
Cost cross-checked against the three-tier pricing formula:

```
cost = input_tokens       × $3.00/1M
     + cache_creation     × $3.75/1M
     + cache_read         × $0.30/1M
     + output_tokens      × $15.00/1M
```

**In `_stats` (runners.py → stats_db → UI):**

| `_stats` key         | Source API field               |
|----------------------|--------------------------------|
| `input_tokens`       | `input_tokens` (residual only) |
| `cache_write_tokens` | `cache_creation_input_tokens`  |
| `cache_read_tokens`  | `cache_read_input_tokens`      |

**Display formula (app.js `addStats`):**

```
isSplit = (cacheRead + cacheWrite) > 0 && input < (cacheRead + cacheWrite)
total   = input + cacheWrite + cacheRead   // when isSplit
new     = input + cacheWrite               // tokens new this turn
```

## Codex (OpenAI Codex CLI)

The `turn.completed` event's `usage` object works the opposite way:

| Field                      | Meaning                                                        |
|----------------------------|----------------------------------------------------------------|
| `input_tokens`             | **Full total**, cache already included.                        |
| `cached_input_tokens`      | Subset breakdown of `input_tokens` — not additive.            |
| `output_tokens`            | **Full output total**, reasoning already included.             |
| `reasoning_output_tokens`  | Subset of `output_tokens` consumed by internal chain-of-thought — not additive. |

**True total input = `input_tokens` (do not add `cached_input_tokens` on top).**

**True total output = `output_tokens` (do not add `reasoning_output_tokens` on top).**

Reasoning tokens are an internal detail of o-series models (o1, o3). They are
already billed and counted within `output_tokens`. Tracking them separately adds
no useful information and was removed (June 2026).

In `_stats`, Codex stores `input_tokens` = total and `cache_read_tokens` =
cached subset. The `isSplit` heuristic in app.js correctly identifies Codex
because `input >= cacheRead` (cache is always ≤ total).

## Decision

- Store all three Claude buckets separately in `_stats` and the DB.
- Display formula uses `input + cacheWrite + cacheRead` for Claude (isSplit path).
- Never treat `input_tokens` alone as the full count for Claude.
- Never add `cache_read_tokens` on top of `input_tokens` for Codex.
- Never add `reasoning_output_tokens` on top of `output_tokens` for Codex — it is already included.
- The `isSplit` heuristic (`input < cacheRead + cacheWrite`) is the runtime
  gate; it correctly routes Claude and Codex without a backend type flag.

## Consequences

- Correct total token counts and cost estimates for both backends.
- The stats bubble shows `↑ N (M new) · K cached  ↓ Y tokens` for Claude sessions,
  where N is the true total, M is new-this-turn (input + cacheWrite), and K is cached.
- Analytics aggregations in `stats_db.py` store raw fields; callers must apply
  the correct formula per backend when computing effective totals.
