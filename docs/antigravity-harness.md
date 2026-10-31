# Antigravity harness

Squid's `antigravity` harness uses the `agy` CLI's native one-shot NDJSON
protocol. The implementation follows the official
[headless protocol](https://antigravity.google/docs/cli/headless/) and
[CLI flags](https://antigravity.google/docs/cli/reference/).

Install with `curl -fsSL https://antigravity.google/cli/install.sh | bash`,
put `agy` on the server's PATH, and complete native login in an interactive
`agy` session. Restart Squid to refresh CLI detection. Browser login-session
integration and isolated credential-home support are not implemented for agy.

The native provider and harness are available automatically, including with an
older explicit `providers:` section. An explicit `antigravity` entry overrides
the built-in provider. No installed user's configuration is rewritten. The
following entries are optional customization examples:

```yaml
harnesses:
  antigravity:
    protocol: {type: oneshot-cli}
    default_provider: antigravity
    supported_apis: [/native/antigravity]
providers:
  antigravity:
    label: agy
    color: "#4285F4"
    auth: {type: subscription}
    gauge: {type: static, text: Native, title: Managed by Antigravity CLI}
    supported_apis: [/native/antigravity]
```

Merge these entries with existing mappings. Select this harness and provider
when creating an agent. Model names are freeform; list available choices with
`agy models`, or leave the model empty to use agy's default. Fresh database
initialization seeds an `agy` agent when the CLI is detected, without
replacing an existing row. The display name is `agy`; internal harness/provider
IDs stay `antigravity` for configuration compatibility. Existing `@antigravity`
agents and history remain available; use `@agy` for the shorter route.

The runner forwards provider args and environment, uses an explicit conversation
ID for resume, and sends history only on fresh turns. It normalizes text and
completed tool calls, preserves conversation IDs, and reports native token
usage without inventing cost. A terminal failure, missing result, or nonzero
process exit fails the turn. Existing process registration, timeout, and
cancellation handling apply. The CLI print timeout follows Squid's response
timeout unless explicitly supplied through provider args.

Permissions follow agy's native policy; Squid does not automatically add a
permission-bypass flag. Configure the CLI's allowed tools if headless commands
are denied. Interactive stdin sessions, API-provider translation, and native
quota discovery are outside this one-shot integration.

Verification includes protocol fixtures, fresh-home catalog discovery, resume
command construction, idempotent database seeding, and real subprocess
cancellation/cleanup. A live CLI smoke remains
required: the implementation environment had no `agy` binary on PATH or in
common installation locations, so authenticated output, resumed continuity,
and a real tool call could not be verified here.

Checks: the full Python 3.11 suite passed 977 tests before two additional
login/cancellation checks; the resulting harness suite passed all 15 checks.
Python 3.10 focused harness/runner/database coverage passed 229 tests.
Installer shell syntax validation also passed.
