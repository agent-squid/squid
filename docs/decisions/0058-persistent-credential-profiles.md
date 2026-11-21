---
status: proposed
date: 2026-11-20
---
# ADR-0058: Dedicated Codex Subscription Providers

[ADR-0059](0059-dedicated-claude-subscription-providers.md) proposes applying
the provider-owned login model to Claude Code.

## Context

[ADR-0028](0028-harness-provider-separation.md) lets agents select a harness
and a provider. Providers already represent separate credentials and quota
pools for the same vendor. For native subscriptions, the harness handles
login. [ADR-0035](0035-cli-auth-sessions-via-scoped-pty.md) supplies that login
flow, which currently targets a harness in the host environment.

[ADR-0036](0036-sandboxed-home-per-agent.md) gives Blank Home agents temporary
homes under `/tmp` and reconciles their credentials with the host login.
A second subscription needs its own credential store that survives a restart
or temporary directory cleanup.

## Proposed decision

Let a Squid provider opt into a dedicated Codex subscription login. The
provider owns the login and any associated quota meter. Agents choose the
account through the existing provider picker and can share a login by
selecting the same provider. Squid shows the provider label and sign-in
status and names the target in the login panel.

| Agent | Harness | Squid provider | Credential store |
| --- | --- | --- | --- |
| `codex` | `codex` | `openai` (built-in) | Existing host Codex login |
| `cope` | `codex` | `cope` (dedicated) | `~/.squid/provider-homes/cope/.codex/auth.json` |

Both providers use the native OpenAI connection through the same Codex CLI.
`cope` is an example Squid provider ID users can configure. Initial support
is limited to native Codex ChatGPT sign-in. Validate this pairing and reject
custom endpoints, API-key auth, and settings or arguments that would redirect
authentication or state storage.

### Provider setting

Set `auth.dedicated_login` in `squid.yaml`. The default uses the host
login. The Providers view displays the configured login mode. For a
dedicated login it also shows the resolved **Codex home** and sign-in status.
Select the agent in chat and run `/login` to sign in. Squid derives
`CODEX_HOME` from the provider ID in v1. The agent's Home setting controls `$HOME`.

Proposed provider entry to add under the existing `providers:` mapping:

```yaml
cope:
  label: Codex Personal
  auth:
    type: subscription
    dedicated_login: true
  supported_apis: [/v1/responses]
```

A failed or stale YAML save leaves the previous provider configuration
active. A provider change that
would make an existing agent's harness or Home mode incompatible is rejected
with the affected agents listed. Validate compatibility again before launch,
including after edits made directly on the host.

### Durable Codex state

Resolve `~/.squid/provider-homes/<provider-id>/.codex/` from the service
user's real home and the validated provider ID. Keep the directory on the
host's persistent filesystem with access restricted to that user. Validate
that the resolved directory stays under the provider storage root and that
it neither aliases the host Codex store nor another provider's store. It
lives outside Squid's context sync tree and worktree lifecycle. Display label
changes, agent deletion, and toggling the setting off leave the provider's
files in place. The provider ID is the stable storage key.

For dedicated providers, Squid sets `CODEX_HOME` and `CODEX_SQLITE_HOME` to
this directory in the child environment. The child keeps its normal
`$HOME` and working directory. Codex stores and refreshes `auth.json` under
`CODEX_HOME`. Apply the provider's resolved storage settings
after environment merging and clear inherited auth overrides such as
`OPENAI_API_KEY`, `CODEX_API_KEY`, and `CODEX_ACCESS_TOKEN`. An unavailable
store, signed-out provider, or incompatible managed Codex policy produces a
provider-specific error. Honor managed requirements without falling back to
the host login. See the Codex
[environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables)
and [authentication](https://learn.chatgpt.com/docs/auth?surface=cli) docs.

The separate `CODEX_HOME` gives the provider its own Codex configuration and
session state. Keeping `$HOME` also keeps normal user tools and discovery,
including user skills in `$HOME/.agents/skills`. The processes run as the same
OS user and have the same filesystem permissions. A separate OS worker user
would be needed if a deployment requires filesystem permissions to differ
between accounts. See the Codex [skill discovery](https://learn.chatgpt.com/docs/build-skills)
and [instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
docs.

### Interaction with Blank Home

Existing agents retain User Home and Blank Home. Blank Home continues to
inherit the host login under ADR-0036. Dedicated Codex providers use the
normal `$HOME` and their own `CODEX_HOME`, so the first release makes Blank
Home unavailable for those providers and rejects the combination in the
server. This keeps credential linking and reconciliation out of dedicated
provider storage.

### Login, execution, and session history

Use one server-side provider resolution for sign-in status, the scoped
`codex login --device-auth` command, and turn execution. Extend both auth
transports to accept a provider ID for Codex login and bind the resolved
credential store to the login session. The server validates eligibility and
selects the path and command under the ADR-0035 allowlist. Existing
harness-only login requests retain host behavior. An explicit unknown or
ineligible provider returns an error. A dedicated turn's sign-in prompt
always supplies its provider ID. After dedicated sign-in, the user can
resubmit the turn. This gives them a chance to confirm the provider after
signing in or changing accounts.

Bind the provider, resolved store, and credential generation to each turn at
submission. Provider changes and config reloads that change the credential
store clear the affected agents' active session pointers. Running turns can
finish with their original store. Reject queued turns whose binding changed
and let the user resubmit them. Compare the binding when updating the active
session pointer so a late completion can update its own history safely.

Record the effective Codex store with each new session. Historical log
lookup uses that recorded store even after an agent switches providers or
the provider configuration changes. A provider ID by itself is insufficient
because toggling the setting changes the store for the same ID. Preserve
legacy lookup for older sessions created without this metadata.

Keep ordinary token refresh within the same credential generation. Serialize
explicit login replacement against active turns that share the store. If a
replacement changes the account or account continuity cannot be verified,
advance the credential generation and invalidate resumable session pointers
for agents using that provider. Watch execution grants, including watch
flows, are tied to the signed-in account. A different account requires a new
grant. Watches, flows, and continuations carry the binding captured at turn
preparation through dispatch. A continuation can automatically resume a
dedicated Codex session or reuse its sequence history only when the recorded
store and account match the new turn. Older dedicated turns without account
metadata require a fresh user request. Topic context injection follows
Squid's existing rules for memory, pins, and lookback.

### Quota

Reuse the Codex meter for dedicated Codex providers configured with the
`codex` gauge. The quota endpoint reads the selected provider's credential
store and never falls back to the host login or an imported token. The
provider ID identifies the dedicated store. A request checks the login
generation before returning usage, and the UI refreshes that provider's
snapshot after sign-in. A signed-out or expired profile shows its own auth
status.

The gauge's live snapshot is separate from per-turn quota deltas. Dedicated
turns continue to skip client sampling for deltas, and the server rejects
writes to both quota-delta endpoints for those turns. Quota samples remain
observational under [ADR-0023](0023-quota-deltas-are-observational.md).

## Verification

A local Codex CLI probe kept `HOME` fixed and changed only `CODEX_HOME`.
The default store reported signed in with a synthetic credential and the
empty dedicated store reported signed out. Returning to the default store
still reported signed in.

Before rollout, verify two live ChatGPT logins, interleaved turns, token
refresh, and session resume after restarting Squid and clearing its temporary
roots. Confirm that signing into one provider leaves the other provider and
the host login intact. Cover inherited credential overrides, unavailable
storage, agent provider changes with queued or running work, login replacement,
and historical log lookup after provider changes. Verify both auth transports,
stale config saves, disabling dedicated login with existing session logs, and
quota submissions from clients that still sample by provider.
