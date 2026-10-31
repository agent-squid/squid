# Internal watch policy and authorization

This slice exposes an **observation-only service** through authenticated HTTP
and `agentsquid watch`. Polling, native MCP tools, automatic turns, and
publication remain unwired. Creation returns a proposal. HTTP activation/resume
returns `409 watch_monitoring_unavailable` until the production loop is wired;
a proposal is never described as active monitoring. Execution-agent selection, event/deadline rules,
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
transport and never an agent tool. Authenticated human controls validate a structured observation grant before
calling `record_grant`. The recorded
`user_action` is an audit reference, not proof that arbitrary referenced message
text authorizes monitoring. An agent cannot call the service to mint credentials,
record grants, or broaden permissions. The human HTTP/CLI control path described below records grants, without
activating a poller.

Grants cover only the referenced watch's immutable target/adapter/topic and its
recorded observation lifetime; they authorize no agent execution or external
mutation. Revocation durably marks the grant. If no other unrevoked grant
covers the watch through its expiry, it atomically pauses active observation
and clears its lease so a late provider result cannot be ingested. Another
full-lifetime grant preserves active observation. Once paused, a new grant and
authorized resume are required to continue. Records survive restart.
Durable lifecycle notifications remain a separate integration task.

## Credential lifetime

Each service instance keeps only SHA-256 hashes of random opaque credentials.
Tokens bind a turn, topic, operation allowlist, grant allowlist, and expiry of at
most one hour. A fresh server lifespan creates a fresh service, invalidating
old tokens; shutdown closes the service and clears its credentials. One-shot launch integration revokes credentials on success, failure, timeout,
cancellation, and generator closure. The topic queue supplies instance-specific
`SQUID_API_URL` and `SQUID_TURN_TOKEN` through the child environment, with a
one-hour maximum token lifetime. Persistent interactive harnesses receive no
watch credential pending a supported refresh mechanism. Native shell turns
receive the scoped environment too. Inherited control/turn credentials are
removed from child environments.

No credential is persisted in the watch/grant database or included in service
responses. The credential registry uses no diagnostic token output. HTTP errors and CLI diagnostics use fixed sanitized errors. The CLI accepts
HTTPS or loopback HTTP only (including loopback aliases such as `127.0.0.2`),
using the same URL validator as turn launch. It follows no redirects and ignores proxy environment
settings. The server rejects credentials over non-loopback plaintext requests. This API boundary does not sandbox an agent's filesystem or shell.


## HTTP and CLI contract

The console entry point dispatches `watch`/`tools` before importing server or
home-directory configuration. Agent calls require explicit `SQUID_API_URL` and
`SQUID_TURN_TOKEN`; there is no fallback server discovery. `agentsquid tools`
returns effective topic operations/adapters and explicitly reports monitoring,
dispatch, and publication as disabled. `agentsquid tools --help` and
`agentsquid watch --help` describe the client. Enabled one-shot model launches
receive a minimal discovery hint and the executable directory on PATH. Hints
are omitted for resumed sessions and slash commands, detected from the original
request supplied by the topic queue before runtime/context assembly. Plaintext listeners bound
to a specific non-loopback address disable turn tool delivery with a safe
warning, rather than issuing unusable credentials. Configure a loopback or
wildcard listener for the current local client transport.

| Agent endpoint | Behavior |
| --- | --- |
| `GET /tools` | Effective observation capability and availability |
| `POST /watches` | Idempotent proposal; adapter, target, request_key, optional poll_interval/expires_at |
| `GET /watches` | Topic-bound list; limit 1–100 and nonnegative offset |
| `GET /watches/{id}` | Topic-bound public row; no poll lease credential |
| `POST /watches/{id}/{action}` | Version-checked pause/cancel; activate/resume unavailable until polling ships |

Mutation bodies reject unknown fields and exceed neither 65,536 bytes nor the
underlying service limits. Lifecycle JSON includes `version` and optional
`operation_key`/`grant_id`. Conflicts return 409, denied credentials return 403,
and malformed/unsupported requests return 400 without echoing request data.

For human controls, provision a random secret of at least 32 characters in
`SQUID_WATCH_CONTROL_TOKEN` in the server's launch environment. No control
credential is generated or printed. Lifespan removes the plaintext from the
process environment and stores its digest; absence disables human controls.
Built-in self-restart hands only the verifier to the new process through a
reserved startup variable, consumed before child launches; it never restores
the plaintext secret. A fresh service-manager start still needs the original
secret supplied by that manager. Human CLI invocations use
that same secret in their own environment, plus explicit `SQUID_API_URL`, and
select `--human-topic TOPIC`. Never put credentials in command arguments or
agent prompts. This authenticates the operator, not natural-language claims.

Human endpoints live under `/watch-control/{topic}/watches`, supporting the
same create/list/show/lifecycle paths. Additional `/{id}/grant` accepts only
`{"expires_at": UTC_UNIX_SECONDS}` and records an exact-watch observation grant
with a server-generated authenticated action ID, host-control key fingerprint,
operation, topic, watch ID, and granted expiry. This identifies the shared
operator credential, not an individually authenticated person. `/{id}/revoke` accepts only
`{"grant_id": ID}` and verifies both topic and watch binding. Human credentials
cannot access agent endpoints and turn credentials cannot access human controls.
These endpoints are not added to Shore capabilities in this slice.

Examples (credentials already supplied securely through the environment):

```sh
agentsquid watch create --input proposal.json
agentsquid watch list
agentsquid watch show WATCH_ID
agentsquid watch grant WATCH_ID --human-topic TOPIC --input grant.json
agentsquid watch cancel WATCH_ID --input cancel.json
```

JSON mutation files may be replaced with `--input -` for stdin. All successful
commands emit JSON; failures return a nonzero exit code and sanitized stderr.
Execution-agent selection, execution/deadline rules, grants for external
mutations, and shared native MCP delivery remain later slices; unsupported
fields are rejected explicitly rather than suggesting execution is available.


Blocking watch service and control operations run in worker threads, including
launch mint/revoke and shutdown. Cancellation waits for an outstanding worker
before credential cleanup, preventing a delayed mint from surviving revocation.
Grant IDs are validated before SQL; malformed identifiers return a sanitized
client error. Foreign keys are enabled by the existing shared database connector,
so deleting a watch cascades to its grant records.
