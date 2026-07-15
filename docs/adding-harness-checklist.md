# Adding a Coded Harness Checklist

Use this when adding a genuinely new CLI protocol adapter. If the target can
run through an existing harness with a different endpoint, credentials, args,
or model, add a provider in `squid.yaml` instead.

## Recon

- Capture `CLI --help` and version output.
- Verify one-shot command shape, structured output mode, and resumed-session
  flags.
- Check how model/provider selection works. If the CLI supports `--provider`
  and `--model` flags, plan to map resolved provider/model values to those in
  the runner.
- Distinguish provider/model selection from gauge selection. Gauges measure
  product quotas (e.g. Claude.ai, Codex/ChatGPT), not API provider usage.
  Default to `type: static` unless the CLI exposes a native quota/balance API.
- Check where credentials come from: native login, environment variables, or
  command flags.
- Sample real output for a plain final answer, a resumed turn, and at least one
  harmless tool call.

## Runtime Wiring

- Add CLI executable/path constants in `agent/config.py`.
- Add harness support in `agent/harnesses.py`:
  - `SUPPORTED_HARNESSES`
  - `SUPPORTED_PROTOCOLS_BY_HARNESS`
  - `_DEFAULT_PROTOCOL_BY_HARNESS`
  - `_HARNESS_PATHS`
  - `_DEFAULT_PROVIDER_BY_HARNESS`
- Add provider-to-harness translation in `agent/resolve.py` only if canonical
  `api_key`, `base_url`, or provider fields need harness-specific wiring.
- Implement the runner in `agent/runners.py`.
- Register it in `RUNNER_NAMES_BY_HARNESS` and
  `RUNNER_NAMES_BY_HARNESS_PROTOCOL`.

## Runner Contract

- Build fresh and resumed commands explicitly.
- Use `_build_prompt(prompt, history)` for fresh one-shot turns.
- For resumed turns, pass only the new prompt to the native resume/session flag.
- Pass `backend_args`, `model`, `backend_env`, and `prompt_preview` through the
  existing runner contract.
- Emit normalized chunks:
  - final response text as strings
  - progress as `{"_status": text}`
  - tools as `{"_tool": ...}` using existing Squid tool shapes where possible
  - one final `{"_stats": ...}` when reliable stats are available
- Preserve native `session_id` so topic sessions can resume.
- Do not invent token or cost stats.
- Raise `CLINotFoundError` with an actionable install command.
- Raise `CLIError` for structured CLI errors.

## Seeding and Docs

- Pick brand-aligned colors for any new providers in `config/squid.yaml.example`.
- Add provider examples if the CLI supports multiple providers/models.
- Confirm `stats_db.init_db()` seeds the default agent row when the harness is
  available. The generic seed row is:
  `(name=harness_id, harness=harness_id, provider=default_provider, model=default_model, cwd=NULL)`.
- Add CLI detection and install guidance in `bin/install.sh`.
- Update README supported CLI/provider lists and examples.
- Update relevant ADR references if they enumerate supported harnesses.

Existing `~/.squid/squid.yaml` files are not automatically rewritten. Users with
older configs need to add provider settings manually or regenerate their config.

## Tests

- Harness/provider validation:
  - default protocol
  - unsupported protocols rejected
  - public harness/provider fields
  - availability/missing requirements where relevant
- Runner selection:
  - `runner_for_harness`
  - `runner_for_agent`
  - adhoc behavior if the harness has interactive protocols
- Runner parsing:
  - fresh and resumed command shape
  - provider/model CLI flags passed through from resolved provider config
  - final text
  - status/progress
  - tool events
  - stats and session ID
  - error events
- Seed behavior:
  - default agent row if the CLI is available
- UI e2e tests only if the UI behavior changes.

## Verification

- Run focused Python tests for harness/provider and runner changes.
- Run the full Python suite when DB seeding or shared execution paths change.
- Run `bash -n` for touched shell scripts.
- Run a real CLI smoke:
  - final-answer no-tool turn
  - resumed-session continuity
  - harmless tool call
- Verify a fresh-home config exposes the harness through `list_harnesses()` and
  provider through `public_providers()`.
