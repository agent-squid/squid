# Persistent dependency maintenance

Isolated turns share installed dependency directories with their backing repos.
Installing through those symlinks can embed an ephemeral turn path in generated
launchers or editable installations. Run tools using the existing environment;
edit manifests and lockfiles in the isolated source tree.

For requested persistent environment updates, schedule `dependency_sync` using
`agentsquid continue create --input -`:

```json
{
  "request_key": "update-environment",
  "steps": [
    {
      "mode": "dependency_sync",
      "prompt": "{\"repository\":\"/absolute/backing/repo\",\"operation\":\"uv_sync\"}"
    }
  ]
}
```

The host runs only after successful parent completion and source sync. Supported
operations are `uv_sync` (`uv sync --locked --reinstall`), `npm_ci` (`npm ci`),
and `pnpm_install` (`pnpm install --frozen-lockfile`). Lockfiles must already be
updated in the source turn. UV reinstalls regenerate installed launchers and
editable paths against the persistent backing repository. No shell commands or
arbitrary arguments are accepted. Inherited turn credentials and Python/UV/npm
configuration environment overrides are removed from the command environment.

The target must be a canonical backing code root recorded on the originating
human turn (or its configured working directory when there are no code roots).
Watch, Flow, routed and task-workspace sequences cannot schedule this operation.
Existing sequence size, provenance, scope, cancellation and sync gates apply.

The host waits cancellably for the repository integration lock (up to 10 minutes), rechecks authority before launch
and while running, and kills the command process group on cancellation or a
10-minute timeout. Both sequence cancellation and the normal chat Stop action
stop the command; saved outcomes preserve an already cancelled message. Remaining child scripts are also stopped when their package
manager exits. Output above 1 MiB stops a running command; the saved assistant
message contains the exit status and last 16,000 bytes of output. Only exit zero
with unchanged authority releases the next step. A requested restart must be a
later step, so a failed maintenance command cannot release it. This mode itself
does not restart the server.

Report requested maintenance as queued until execution records its outcome.
Failed commands may leave a partially updated environment. Interrupted claims
become ambiguous on startup and are never automatically replayed; inspect the
saved outcome and request fresh maintenance if needed.

This adds host routing, not OS write protection. Shared directories remain
writable by same-user processes, and package-manager lifecycle scripts retain
their usual host privileges. No private per-turn environments are introduced.
