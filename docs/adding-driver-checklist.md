# Adding a Coded Driver Checklist

Use this when adding a genuinely new CLI protocol adapter. If the target can
run through an existing driver with different endpoint, credentials, args, or
model, add a backend in `squid.yaml` instead.

## Recon

- Capture `CLI --help` and version output.
- Verify one-shot command shape, structured output mode, and resumed-session
  flags.
- Check how model/provider selection works. If the CLI supports `--provider`
  and `--model` flags, plan to map `backend.provider` and `backend.model` to
  those in the runner.
- Distinguish provider/model selection from gauge selection. Gauges measure
  product quotas (e.g. Claude.ai, Codex/ChatGPT), not API provider usage.
  Default to `type: static` unless the CLI exposes a native quota/balance API.
- Check where credentials come from: native login, environment variables, or
  command flags.
- Sample real output for a plain final answer, a resumed turn, and at least one
  harmless tool call.

## Runtime Wiring

- Add CLI executable/path constants in `agent/config.py`.
- Add driver support in `agent/backends.py`:
  - `SUPPORTED_DRIVERS`
  - `SUPPORTED_PROTOCOLS_BY_DRIVER`
  - `DEFAULT_PROTOCOL_BY_DRIVER`
  - `_DRIVER_PATHS`
  - `_DEFAULT_BACKENDS`
- Add driver-specific `execution_env()` or `driver_settings()` translation only
  if canonical `api_key`, `base_url`, or provider fields are supported.
- Implement the runner in `agent/runners.py`.
- Register it in `RUNNER_NAMES_BY_DRIVER` and
  `RUNNER_NAMES_BY_DRIVER_PROTOCOL`.

## Runner Contract

- Build fresh and resumed commands explicitly.
- Use `_build_prompt(prompt, history)` for fresh one-shot turns.
- For resumed turns, pass only the new prompt to the native resume/session flag.
- Pass `backend_args`, `model`, `backend_env`, and `prompt_preview`.
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

- Pick a brand-aligned color for the backend and set it in `agent/backends.py`
  (the `COLOR` or `_DEFAULT_BACKENDS` entry). Use the CLI's official logo color;
  avoid placeholder shades.
- Add the backend to `config/squid.yaml.example` so fresh installs see it. Add
  commented-out alternative examples if the CLI supports multiple
  providers/models (e.g. `pi-openai` with `provider: openai`).
- Confirm `stats_db.init_db()` seeds the default agent row when the backend is
  available. The generic seed row is:
  `(name=backend_id, backend=backend_id, model=backend.model, cwd=NULL)`.
- Add CLI detection and install guidance in `bin/install.sh`.
- Update README supported CLI/backend lists and examples.
- Update relevant ADR references if they enumerate supported drivers.

Existing `~/.squid/squid.yaml` files are not automatically rewritten. Users with
older configs need to add the backend block manually or regenerate their config.

## Tests

- Backend validation:
  - default protocol
  - unsupported protocols rejected
  - public backend fields
  - availability/missing requirements where relevant
- Runner selection:
  - `runner_for_driver`
  - `runner_for_backend`
  - adhoc behavior if the driver has interactive protocols
- Runner parsing:
  - fresh and resumed command shape
  - provider/model CLI flags passed through from backend config
  - final text
  - status/progress
  - tool events
  - stats and session ID
  - error events
- Seed behavior:
  - default agent row if the CLI is available
- UI e2e tests only if the UI behavior changes.

## Verification

- Run focused Python tests for backend and runner changes.
- Run the full Python suite when DB seeding or shared execution paths change.
- Run `bash -n` for touched shell scripts.
- Run a real CLI smoke:
  - final-answer no-tool turn
  - resumed-session continuity
  - harmless tool call
- Verify a fresh-home config exposes the backend through `public_backends()`.
