# Internal watch policy and authorization

This slice implements an **observation-only internal service**, not user-facing
watch registration. Polling, HTTP/CLI/MCP operations, automatic turns, and
publication remain unwired. Execution-agent selection, event/deadline rules,
turn/retry budgets, and publication scopes are not supported by this service.
Unsupported policy fields and operations fail validation rather than implying
that those capabilities exist.

## Host policy

Trusted `squid.yaml` settings extend the existing registry:

```yaml
watches:
  enabled: false
  registry:
    servers: {}
    probes:
      snapshot:
        argv: [/absolute/path/to/read-snapshot]
  policy:
    operations: [create, list, show, activate, pause, resume, cancel]
    adapters: [snapshot]
    min_poll_seconds: 60
    max_lifetime_seconds: 86400
  topics:
    example:
      operations: [create, list, show, cancel]
      max_lifetime_seconds: 3600
```

Omitted policy permits the supported observation lifecycle operations and
registered adapters, with a 30-second polling minimum and 24-hour maximum
lifetime. The top-level enablement switch still defaults to false. Topic
policies intersect operation/adapter allowlists, increase polling minimums, and
reduce lifetime ceilings. They cannot enable globally disabled watches or
expand scope. Default expiry is the lesser of 24 hours and the effective maximum;
idempotent creation preserves the original absolute expiry. Existing watches
are not automatically reconciled when policy changes; polling remains disabled
until a future scheduler enforces current policy on every claim.

## Trusted control versus agent operations

`WatchService` derives topic and operations from a credential, never from a
caller-supplied topic or user assertion. It provides bounded list/show, idempotent
proposal creation, and versioned lifecycle operations using the existing store.
Activation and resume additionally require an unrevoked durable grant for the
exact watch, explicitly included in the credential's scope. Grant checks run
inside the lifecycle write transaction and before replaying keyed operations.
Pause/cancel require their respective token operations but no activation grant.
Responses omit internal polling lease tokens.

`WatchControl` is a **trusted Python host interface**, not an authenticated
transport and never an agent tool. Future authenticated human controls must
validate a structured grant before calling `record_grant`. The recorded
`user_action` is an audit reference, not proof that arbitrary referenced message
text authorizes monitoring. An agent cannot call the service to mint credentials,
record grants, or broaden permissions. No human HTTP/CLI control path is shipped
in this slice; therefore no production caller currently records grants.

Grants cover only the referenced watch's immutable target/adapter/topic and its
recorded observation lifetime; they authorize no agent execution or external
mutation. Revocation durably marks the grant and atomically pauses an active
watch, clearing its lease so a late provider result cannot be ingested. A new
grant and authorized resume are required to continue. Records survive restart.
Durable lifecycle notifications remain a separate integration task.

## Credential lifetime

Each service instance keeps only SHA-256 hashes of random opaque credentials.
Tokens bind a turn, topic, operation allowlist, grant allowlist, and expiry of at
most one hour. A fresh server lifespan creates a fresh service, invalidating
old tokens; shutdown closes the service and clears its credentials. Trusted
launch integration must call `revoke_turn` on success, failure, timeout, and
cancellation. That integration and instance-specific environment delivery are
**not implemented yet**; no token is injected into current agent processes.

No credential is persisted in the watch/grant database or included in service
responses. The credential registry uses no diagnostic token output. Transport
redaction and protected remote delivery still need verification when HTTP/CLI
is added. This API boundary does not sandbox an agent's filesystem or shell.
