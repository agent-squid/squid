---
status: proposed
date: 2026-11-23
---
# ADR-0060: Topic Provider Preferences

## Context

The `squid*` topics should use personal accounts by default, with an
explicit exception when the user approves another provider. Personal and
work accounts can share the Codex harness. The resolved provider is the
relevant setting under [ADR-0028](0028-harness-provider-separation.md).

Topic-memory instructions reach the model after Squid has selected its
provider. Squid already has the information needed to make this decision
before invocation, without a model call or extra prompt context.

## Proposed decision

Configure topic glob rules in the instance configuration so a single rule
covers existing and future topics. Each rule can specify two provider
lists:

- `allow_providers` lists the providers that can run without approval.
  Other verified providers require one-time approval. Omitting the list
  lets any verified, non-denied provider run without approval.
- `deny_providers` lists providers that may never run under the rule.
  A deny takes precedence over an allow and cannot be overridden by
  one-time approval.

Use the first matching rule in configuration order. Configure `squid*`
with `allow_providers: [openai_pers]` for the personal default. A topic
that only needs exclusions can set `deny_providers` by itself. Topics
without a matching rule keep their existing routing behavior.

For automatic selection, filter candidates by provider before ranking or
calling a routing model, as described in
[ADR-0045](0045-local-embedding-based-agent-auto-routing.md). For ordinary
selection, keep an explicitly named agent as the requested target. For
implicit selection, prefer an eligible sticky agent, then an eligible
global default. If the remaining choice requires approval, surface it
before starting the turn.

Check the resolved provider at model dispatch for chat, routes, Watches,
and continuations, including durable work resolved after enqueue. Return
a provider-denied result for an excluded provider. If a rule has exclusions
and the provider cannot be verified, block until Squid can verify the
provider and check those exclusions. Otherwise, if the provider requires
approval, return an approval-required result with the topic, target agent,
and provider status. Contact the provider only after the check passes.

The UI or CLI can ask the user to approve the specific dispatch once.
Squid binds approval to the request, topic, agent, and resolved runtime,
and consumes it at dispatch. If the target or runtime changes, require a
fresh decision. For an unresolved provider, approval must explicitly
cover the unverified runtime. Automated callers surface the blocked
dispatch through their existing status and error paths. They cannot grant
approval or carry it into subsequent steps.

## Consequences

The preference applies before a turn can send context to its model
provider. A user can still intentionally run one turn outside the
allow list if the provider is eligible for approval. Automated work can
stop at a provider mismatch and must surface the reason. A human can
retry with an eligible provider or approve a dispatch that permits it.
