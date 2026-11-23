---
status: accepted
date: 2026-11-23
---
# ADR-0060: Topic Provider Preferences

## Context

Topics such as `squid` should use personal provider accounts by default.
Personal and work accounts can share the Codex harness. The resolved
provider is the relevant setting under
[ADR-0028](0028-harness-provider-separation.md).

Topics can be created as users work. Their provider preferences belong
with the topic and should take effect without an instance restart.

## Decision

Set optional provider lists under `squid` in the topic's `memory.md`
front matter. Squid reads those fields before agent selection and
rechecks them before dispatch. The fields take effect even when the
user sends a turn without including topic memory in the prompt.

```yaml
---
squid:
  allow_providers: [anthropic_pers, openai_pers]
  deny_providers: [anthropic, openai]
---
```

- `allow_providers` lists providers that can run without approval.
  Other verified providers require one-time approval. Omitting the list
  lets any verified, non-denied provider run without approval.
- `deny_providers` lists providers that may never run under the rule.
  A deny takes precedence over an allow and cannot be overridden by
  one-time approval.

Dotted subtopics share their root topic's memory and provider preferences.
Topics without either field keep their existing routing behavior.
Malformed front matter or invalid provider lists block model dispatch
until corrected.

When [ADR-0045](0045-local-embedding-based-agent-auto-routing.md) adds
automatic selection, filter candidates by provider before ranking or
calling a routing model. For ordinary
selection, keep an explicitly named agent as the requested target. For
implicit selection, prefer an eligible sticky agent, then an eligible
global default. If the remaining choice requires approval, surface it
before starting the turn.

Check the resolved provider at model dispatch for chat, routes, Watches,
continuations, and journal generation, including durable work resolved
after enqueue. Return
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
