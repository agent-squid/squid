---
status: proposed
date: 2026-11-20
---
# ADR-0059: Dedicated Claude Subscription Providers

## Proposed decision

Extend the provider-owned subscription login from
[ADR-0058](0058-persistent-credential-profiles.md) to Claude Code. Keep
the existing `claudecode` agent on the `anthropic` provider and its host
login. A second agent, for example `clape`, uses the same harness with a
separately configured `clape` provider.

Use the existing `auth.dedicated_login` provider option for claude.ai
subscriptions. The Providers view shows the login status. Users select an
agent and run `/login` in chat to open the scoped
`claude auth login --claudeai` flow. Users pick the agent to choose the
account. Configure the native Claude provider with
`supported_apis: [/v1/messages]`. Reject dedicated providers that advertise
both native subscription API shapes so the provider can resolve exactly one
harness. Resolve dedicated Claude providers to the one-shot protocol before
queueing even if the global Claude protocol is interactive.

## Claude Code storage and execution

Set `CLAUDE_CONFIG_DIR` to the stable, absolute directory
`~/.squid/provider-homes/<provider-id>/.claude/` for login, status, and
turns. Resolve transcripts from that directory's `projects/` tree. Keep
the process's usual `HOME` and the existing working directory selection.
Claude Code owns its credentials. It selects a Keychain entry based on the
config directory on macOS and stores file-backed credentials under the
config directory on Linux and on macOS when Keychain writes fail.
[Claude Code authentication](https://code.claude.com/docs/en/authentication#credential-management)

Keep dedicated provider launches out of Blank Home credential reconciliation.
Clear inherited authentication and routing overrides that could select a
different credential or endpoint. For initial support, exclude user,
project, and local settings overlays with `--setting-sources ''` so they
cannot reintroduce credential helpers or routing variables. Keep managed
settings in effect. Direct Anthropic Console profile lookup to a separate
unused location under the provider directory to prevent expiry from falling
back to the host's Console profile. Disable native Claude auto-memory for
dedicated providers because the same directory can hold memory created under
an earlier login. Validate the config directory,
first-party provider, and claude.ai login before launching the turn, under
the same store lock used for sign-in.
Scope initial support to claude.ai subscriptions. Anthropic documents that
two keyless Claude Console logins use storage outside the config directory.
[Claude Code multi-account sign-in](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts)

Reuse the account binding from ADR-0058 for queued turns, session resume,
historical logs, watch grants, and continuations. Claude's native status
shows cached email and organization, which can survive a credential
replacement. Treat them as diagnostic fields. On Linux, bind reuse to a
digest of the private OAuth credential file's access and refresh tokens,
after confirming native status and checking that the credential stayed
stable during that probe. A token rotation may require a fresh session or
watch reapproval. On macOS, native status does not establish which Keychain
or fallback-file credential it used, so allow fresh manual turns and
historical log viewing only. Sign-in remains available when metadata is
incomplete and the provider directory is usable.
Use the same protected config directory across
restarts and preserve the host login throughout sign-in and renewal.
Start with the one-shot runner. Enable the persistent Claude runner after
its process cache and lifecycle incorporate the provider and account binding.
With `gauge: claude`, fetch usage using the provider's Claude Code OAuth
access token. Read the config-directory-scoped Keychain entry on macOS
or the provider's private credential file. Claude Code owns token renewal.

## Verification

The installed Claude Code 2.1.316 CLI reported the correct
`configDirectory` and signed-out status for two freshly created
directories with the same `HOME`. Synthetic credential fixtures supplied
both identity fields. Actual one-shot commands with signed-out and expired
synthetic stores failed without contacting the project's configured API
endpoint, even with a project API key present.

Before enabling the feature, test sign-in with two claude.ai accounts on
macOS and on the supported Linux host. Confirm that a second login and
renewal leave the first account intact. Verify interleaved turns, resume,
host restart, credential overrides, account replacement, and session-log
lookup. Test the native identity fields on each supported CLI version
before relying on them for watch authorization or resuming sessions.
