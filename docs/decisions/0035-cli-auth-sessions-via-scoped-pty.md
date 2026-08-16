---
status: accepted
date: 2026-08-06
updated: 2026-08-15
---
# ADR-0035: CLI Auth Sessions via Scoped PTY over SSE

## Context and Problem Statement

Every bundled harness (`claudecode`, `codex`, `cursor`, `opencode`, `pi`)
shells out through `oneshot-cli` (and `claudecode` also through
`interactive-cli`) with `stdin=DEVNULL` or a structured stdin/stdout
protocol — never a real terminal. When a harness isn't logged in, Squid can
only detect and report the failure, not fix it: today that detection exists
for exactly one harness, a string match on `"Not logged in"` in
`_ClaudeStreamParser.feed_line` (`agent/runners.py:370`), which raises a
`CLIError` telling the user to run `claude login` themselves, outside Squid.

An API-key/env-var shortcut does not solve this for most users. Subscription
tiers (Claude Pro/Max, Cursor, ChatGPT Plus) authenticate via OAuth or
device-code flows tied to the account, not an API key — API keys are a
separate pay-per-use billing surface. So for the common case, the CLI's own
interactive login command is the only path to auth, and that command expects
a real terminal (it prints a URL/device code and polls, or opens a browser
and waits, or runs an interactive provider-picker wizard).

Squid needs a way to run that login command and let the user complete it
without leaving the app.

## Decision Outcome

Add a narrow, allowlisted **auth session** feature, separate from normal
turn execution and separate from the `interactive-pty` *protocol* defined in
[ADR-0022](0022-multi-protocol-agent-execution.md). ADR-0022's
`interactive-pty` is for running full agent turns through a PTY when a
harness's real interactive behavior only exists in a terminal; this feature
uses the same PTY primitive but only ever runs one of a fixed, allowlisted
set of login subcommands, never a turn and never arbitrary input. The two
should not be conflated even though both spawn PTYs.

### Transport: SSE output + POST input, not WebSocket

Squid has no WebSocket infrastructure today — every streaming surface is
`StreamingResponse` over `text/event-stream` (`agent/server.py:1111`,
`:1587`, `:2218`), paired with discrete POSTs for input actions. Chat itself
stays on SSE; this ADR does not change chat's transport. Login flows
involve a handful of discrete inputs per session (open a link, copy/paste a
code, press Enter once) — not continuous keystroke-rate interaction — so
POST-per-input is sufficient and avoids introducing a second realtime
transport stack for one feature. WebSocket is deferred until a real
general-purpose interactive shell is in scope, which this feature
deliberately is not.

Endpoints:

```
POST /auth/session                 create + spawn an allowlisted login command
GET  /auth/session/{id}/events     SSE stream of PTY output bytes
POST /auth/session/{id}/input      forward input bytes to the process
POST /auth/session/{id}/resize     terminal resize (cols, rows)
POST /auth/session/{id}/cancel     kill the process group, close the session
```

### Allowlisted commands only

The command run by an auth session is chosen server-side from a fixed table
keyed by harness — never constructed from user input:

```
claudecode -> claude auth login --claudeai
codex      -> codex login --device-auth
cursor     -> cursor-agent login   (NO_OPEN_BROWSER=1 optional)
opencode   -> opencode auth login
pi         -> no login command; manual instructions to set
              ANTHROPIC_API_KEY / ANTHROPIC_OAUTH_TOKEN
```

Input bytes sent to `/input` are forwarded to this fixed process only — a
user cannot start a different command through this endpoint. There is no
general shell.

### Detection

`CLIAuthRequired` is raised per harness, not shared. Only `claudecode` has a
detector today (`runners.py:370`); `codex`, `cursor`, and `opencode` need
their own stderr/exit-code detectors added, since each fails differently on
missing auth. This is in scope for the feature, not follow-up work.

### Credentials and process environment

Login processes run in the same host environment normal turns run in, not
inside per-turn worktree isolation ([ADR-0025](0025-per-turn-worktree-isolation.md)
scopes cwd/workspace, not credential storage). Each harness writes its
credentials to the same fixed, non-isolated locations it reads at normal
turn time (`~/.claude`, `~/.codex/auth.json`, OS keychain for cursor, etc.),
so a completed login is immediately visible to the next real turn with no
extra plumbing.

### UI

Only the composer (`#view-chat > form#form` / `#input-area`) swaps to an
embedded xterm.js panel; the chat transcript above it is untouched and the
page never becomes a full-screen terminal. xterm.js is the renderer only —
it does not require WebSocket and needs just the SSE byte stream plus
`/input`/`/resize`. Squid's existing in-app panel/modal styling is used, not
a system modal.

Lifecycle:

- Exit 0: close the panel, restore the composer, retry the original prompt
  if the session was triggered by a failed turn.
- Exit nonzero: keep terminal output visible with Retry/Cancel.
- Cancel or SSE disconnect: kill the process group, restore the composer.

### Process lifecycle

Standard `pty.openpty()` spawn with process-group isolation, consistent with
[ADR-0018](0018-cli-process-group-isolation.md), registered in the normal
process registry so existing stop/timeout controls reach it. An idle timeout
closes orphaned sessions (e.g. a closed browser tab) rather than leaking
processes indefinitely.

## Amendment (2026-08-15): macOS keychain-unlock remediation

`cursor-agent login` fails immediately, before it ever reaches an
interactive prompt, when the macOS login keychain is locked:

```
Error: Your macOS login keychain is locked.
Run security unlock-keychain and try again.
```

Cursor's keychain precheck is fire-and-done — it cannot be satisfied from
inside the login flow itself. Previously the only workaround was to unlock
the keychain from a separate terminal and reboot the whole Squid server,
because the server is launched detached
(`nohup python -m agent.server &`, `bin/start.sh`) and an unlock done in a
*different* macOS security/audit session is invisible to it.

**Investigated first: does `cursor-agent` have a keychain-free auth path at
all, the way `pi` uses `ANTHROPIC_API_KEY`?** It does define `CURSOR_API_KEY`
/`CURSOR_AUTH_TOKEN` env vars as alternatives to `cursor-agent login` — but
this doesn't help here: the keychain precheck runs unconditionally at CLI
startup, before argument/env parsing picks an auth source. Confirmed
empirically — `CURSOR_API_KEY=<value> cursor-agent --version` still fails
with the same locked-keychain error, with no subcommand ever reached. There
is no way to avoid the keychain for this CLI, so the in-session-unlock
remediation below is necessary rather than a documentation-only fix.

**In-session unlock, not out-of-band + reboot.** `pty.fork()` calls
`setsid()` but does not create a new audit session, so every PTY child Squid
spawns (including auth sessions) inherits the server process's
security/audit session — the same one a subsequently-spawned `cursor-agent
login` inherits. An interactive `security unlock-keychain` run inside a
Squid PTY session therefore unlocks the keychain for the server's own
session, and the next `cursor-agent login` sees it unlocked. No reboot
required. This adds a fourth `create_session` mode, `unlock`, alongside
`login`/`install`/`pull`/`remove` (ADR-0037's amendment): fixed argv
`["security", "unlock-keychain"]` (no path, so it prompts interactively for
the default/login keychain), macOS-only (`AuthSessionError` on any other
`sys.platform`), never built from user input — same invariant as every
other mode here. The password itself never enters Squid's storage: `security`
disables terminal echo during the prompt, so it never appears in the ring
buffer or an `auth.output` frame; only the raw `auth.input` bytes cross the
wire, which were already unpersisted for every auth-session mode.

**Loopback-only by default, fail-closed.** Unlocking is typing a macOS
login-keychain password — treated with the same caution as the password
itself. Squid is reachable beyond literally-local already (`tailscale serve`
onto the tailnet), so a naive "the server binds to loopback" argument is not
sufficient. `agent/server.py`'s `_keychain_unlock_allowed` gates
`mode="unlock"` on the request's raw TCP peer resolving to loopback
(`127.0.0.1`/`::1`), separately at both the WebSocket (`auth.start`) and
HTTP (`POST /auth/session`) entry points, and rejects a non-loopback caller
with `unlock_requires_local` instead of spawning.

The raw TCP peer address is not trusted alone as proof of *localness*:
`tailscale serve` reverse-proxies to this server over loopback, so a tailnet
client's connection also arrives with a loopback peer address at the ASGI
layer. It is trustworthy as proof of remoteness being absent, though, since
the server binds only `127.0.0.1` and a remote client cannot forge a
loopback source address against that bind. The gate does not attempt to
recover "the real client IP" from `X-Forwarded-For` — that header is
client-supplied, not server-verified, and a remote caller can set it to
`127.0.0.1` itself to impersonate a local client (an earlier version of this
gate did exactly this and was caught in review before it shipped: it fell
back to trusting the *first* comma-separated value in `X-Forwarded-For`,
which a spoofed header defeats outright, and which some reverse proxies
*append to* rather than replace, defeating it even for a well-intentioned
proxy). Instead the gate is fail-closed on the header's mere presence: any
`X-Forwarded-For`, `X-Real-IP`, RFC 7239 `Forwarded`, or
`Tailscale-User-Login` header denies the request immediately, regardless of
value, since a direct, unproxied local connection never sets any of them —
only a request with none of these headers and a genuinely loopback raw peer
passes. An explicit opt-in,
`auth.allow_remote_keychain_unlock` in `squid.yaml` (default `false`), lets a
user accept typing their Mac keychain password from another tailnet device
if they choose to — it bypasses the whole check, not just the header logic.
This opt-in is independent of, and should remain more restrictive than,
whatever principal model a future ADR-0039 Shore relay connection uses — a
Shore-relayed request is never treated as loopback by this gate, regardless
of the opt-in flag.

**UI.** `ui/app.js` watches decoded output from a streaming `cursor` login
session for the locked-keychain signature (`keychain is locked`,
case-insensitive, matched against accumulated decoded text so it is robust
to ANSI codes and to the message landing across multiple PTY reads). On a
match it shows an "Unlock keychain & retry" affordance in the same login
panel — never auto-run, since the user must consent to typing their own
password. Clicking it opens a `mode="unlock"` session in that same panel,
reusing `openAuthPanel`'s existing WS/SSE transport plumbing unchanged; a
clean exit (0) automatically re-issues the original `cursor` login, a
nonzero exit leaves the failure and a Retry button, same as any other auth
session. If the server refuses with `unlock_requires_local` (a non-loopback
client without the opt-in), that message is surfaced in place of a password
prompt — the server's gate is the actual authority, the client-side
affordance is only ever an offer.

## Consequences

- Good: users can complete OAuth/device-code login for any harness without
  leaving Squid or opening a separate terminal.
- Good (2026-08-15 amendment): a locked macOS login keychain no longer
  requires an out-of-band unlock plus a full server reboot to unblock
  `cursor-agent login` — the fix runs in-session, gated to a loopback client
  by default with an explicit opt-in for tailnet use.
- Good: no new realtime transport — chat keeps its existing SSE model, and
  the auth session reuses the same mental model (SSE out, POST in) instead
  of adding WebSocket infrastructure the rest of the app doesn't have.
- Good: scoped to a fixed command allowlist, so this is not a general
  in-app shell and doesn't expand Squid's security surface the way one
  would.
- Neutral: xterm.js renders whatever the CLI prints (spinners, ANSI, prompts)
  correctly with no per-harness output parsing, at the cost of adding a
  frontend terminal-emulator dependency.
- Bad: per-harness auth-failure detection has to be built individually for
  `codex`, `cursor`, and `opencode` — there's no shared detector to reuse
  beyond `claudecode`'s.
- Bad: `pi` has no CLI login command at all, so it falls back to manual
  instructions rather than a driven auth session.

## Deferred / open before implementation

- `codex login --device-auth` and `cursor-agent login` with
  `NO_OPEN_BROWSER=1` are believed to be non-interactive-friendly
  (print-and-poll) based on `--help` output, but neither has been run
  end-to-end to confirm they don't require an additional keypress after the
  browser/device step completes.
- Idle timeout value for auth sessions is not yet chosen (may reuse
  `DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS` from `agent/harnesses.py` or
  use a shorter, dedicated value).
- xterm.js is not yet a UI dependency and needs to be added.
