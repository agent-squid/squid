# Antigravity harness

Antigravity is a supported harness, selectable when creating an agent.
Squid's `antigravity` harness uses the `agy` CLI's native one-shot NDJSON
protocol. The implementation follows the official
[headless protocol](https://antigravity.google/docs/cli/headless/) and
[CLI flags](https://antigravity.google/docs/cli/reference/).

Install with `curl -fsSL https://antigravity.google/cli/install.sh | bash`,
put `agy` on the server's PATH, and complete native login in an interactive
`agy` session. Restart Squid to refresh CLI detection. Browser login-session
integration and isolated credential-home support are not implemented for agy.

The example configuration includes the native harness and its `google`
provider. Installations with an explicit provider catalog need a provider
supporting `/native/antigravity`. Add the entries below if the catalog lacks
one. Installations that already use an `antigravity` provider ID can keep
that ID and use it as the harness default.

```yaml
harnesses:
  antigravity:
    protocol: {type: oneshot-cli}
    default_provider: google
    supported_apis: [/native/antigravity]
providers:
  google:
    label: Google
    color: "#4285F4"
    auth: {type: subscription}
    gauge: antigravity
    supported_apis: [/native/antigravity]
```

Merge these entries with existing mappings. Select this harness and provider
when creating an agent. Model names are freeform; list available choices with
`agy models`, or leave the model empty to use agy's default. Fresh database
initialization seeds an `agy` agent when the CLI and a compatible default
provider are available, preserving existing rows. The harness display name is
`Antigravity` and its ID is `antigravity`. Existing `@antigravity`
agents and history remain available; use `@agy` for the shorter route.

The runner forwards provider args and environment, uses an explicit conversation
ID for resume, and sends history only on fresh turns. It normalizes text and
completed tool calls, preserves conversation IDs, and reports native token
usage without inventing cost. A terminal failure, missing result, or nonzero
process exit fails the turn. Existing process registration, timeout, and
cancellation handling apply. The CLI print timeout follows Squid's response
timeout unless explicitly supplied through provider args.

The runner adds `--dangerously-skip-permissions` for headless execution so
commands and file writes can proceed without interactive confirmation. Native
CLI authentication is still required. Interactive stdin sessions and
API-provider translation are outside this one-shot integration. Native quota
is supported through the `antigravity` gauge described below.

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

## Live quota

The `antigravity` gauge runs `agy --print /usage --output-format json` to
retrieve quota without a model turn. It uses the existing native login and the
shared gauge refresh flow, including a fresh read after each browser turn, plus opening/returning to Squid.
Recovered turns discovered from history or another device also refresh on completion.
Local completion callbacks retain ownership so their reads are not duplicated.
It does not read quota before submission or poll during an active agy turn.
Changes compare consecutive successful readings of the same model group/window
and reset period. First readings, bucket switches, resets, and decreases establish
a new baseline without recording a delta. Failed reads preserve the last successful baseline.
The donut shows percent used for the most consumed model-group/window bucket;
the tooltip lists all buckets and reset times. Recorded changes cover the interval since the last successful check, not exact
per-turn consumption:
shared account activity and backend reporting delays can affect them.
The former default Native badge automatically upgrades; custom gauges remain intact.

## Conversation logs

Squid captures submitted prompts (including injected history/context) and native
AGY stdout lines under `~/.squid/logs/agy/<agent-hash>/<conversation-id>.jsonl`
(or beneath `SQUID_LOG_DIR`). Each JSONL record carries a receipt timestamp;
`squid_agy_stdout` records preserve the native line in `raw`, and `squid_prompt`
records contain the submitted message and Squid message ID. Files are created
with owner-only permissions, appended across resumed turns, and flushed for each
event. Capture failures warn without failing the model turn.

The context popup's session link opens these captures in the common log viewer,
with search, event labels, and Squid turn dividers. Capture begins with this
version; earlier turns and conversations run outside Squid are not imported from
AGY's SQLite storage. Logs persist until removed manually.
