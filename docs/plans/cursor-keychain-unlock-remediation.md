# Plan: In-session keychain-unlock remediation for `cursor-agent login`

Follow-on to `adr-0040-cli-auth-ws-migration.md` (that migration is complete).
Execute the steps in order; each is self-contained with objective, files,
actions, and an acceptance check.

## Problem

`cursor-agent login` fails immediately when the macOS login keychain is locked:

```
Error: Your macOS login keychain is locked.
Run security unlock-keychain and try again.
```

Cursor's keychain precheck is fire-and-done, so it can't be fixed from inside
the login flow. Today the only workaround is to unlock the keychain from a
separate terminal and **reboot the whole squid server** — because the server is
launched detached (`bin/start.sh:100`, `nohup python -m agent.server &`), and an
unlock done in a *different* macOS security/audit session is invisible to it.

## Key insight (this is what makes the fix work)

`pty.fork()` calls `setsid()` but does **not** create a new audit session, so
every PTY child squid spawns inherits the server process's security/audit
session — the same one a subsequently-spawned `cursor-agent login` inherits.
Therefore an interactive `security unlock-keychain` run **inside a squid PTY
session** unlocks the keychain for the server's own session, and the next
`cursor-agent login` sees it. **No reboot.** The auth PTY is already interactive
(`write_input`), so the user can type their keychain password at the prompt.

## Design decisions

- **In-session unlock, not out-of-band + reboot.** Add an allowlisted
  `security unlock-keychain` PTY mode and run it in the auth panel.
- **Allowlist-safe.** Fixed argv `["security", "unlock-keychain"]` (no path →
  unlocks the default/login keychain, prompts interactively). Never built from
  user input — same invariant as `_login_argv`.
- **Password stays transient.** `security` disables terminal echo during the
  prompt, so the password never enters the ring buffer or any `auth.output`
  frame; only the raw `auth.input` bytes cross the wire, and those are already
  unpersisted. Do NOT change that.
- **Loopback-only by default (SECURITY — decide before building).** The server
  is exposed on the tailnet via `tailscale serve`, so "local" is not guaranteed
  even today. Gate the unlock mode to a loopback client address
  (`127.0.0.1`/`::1`) by default, with an explicit config opt-in
  (`auth.allow_remote_keychain_unlock`, default `false`) for users who accept
  typing their Mac keychain password from another tailnet device. Hard-block it
  over any future ADR-0039 Shore relay principal regardless of that flag.
- **macOS-only.** Unlock mode is valid only when `sys.platform == "darwin"`.

---

## Step 0 — Investigate a keychain-free path first (may obviate the rest)

Check whether `cursor-agent` supports auth without the keychain — a token file
or env var (the way `pi` uses `ANTHROPIC_API_KEY`). If it does, document that
instead and stop; it beats the unlock flow entirely.

**Acceptance:** confirmed cursor-agent has no keychain-free auth path (else
pivot to documenting it).

---

## Step 1 — Allowlisted unlock mode (auth_sessions.py)

**File:** `agent/auth_sessions.py`

- Add `mode="unlock"` handling in `create_session`. It ignores `target_id` for
  argv purposes; use a fixed `argv = ["security", "unlock-keychain"]` and
  `env = os.environ.copy()`.
- Guard: raise `AuthSessionError` if `sys.platform != "darwin"`.
- `display_command` → `security unlock-keychain` (shown in the panel before the
  password prompt). Registry prompt e.g. `unlock: keychain`.
- Everything else (PTY spawn, idle reaper, ring buffer, process registry) is the
  existing machinery unchanged.

**Acceptance:** `create_session(..., mode="unlock")` on darwin spawns the
prompt; non-darwin raises; argv is the fixed two-element command.

---

## Step 2 — Wire unlock over WS + HTTP, with the loopback gate (server.py)

**File:** `agent/server.py`

- Accept `mode="unlock"` in `_realtime_auth_start`'s validation and in the HTTP
  `AuthSessionRequest`/`auth_session_create` path (parity). No `model`; harness/
  target is irrelevant to argv but keep the field for display.
- **Loopback gate:** before spawning an unlock session, resolve the client
  address (`websocket.client.host` for WS; request client host for HTTP). If it
  is not loopback and `auth.allow_remote_keychain_unlock` is not enabled, reject
  with `{"ok": false, "error": "unlock_requires_local", "detail": ...}` (WS) /
  400 (HTTP). This applies to `mode="unlock"` only — other modes are unchanged.
- Add the `auth.allow_remote_keychain_unlock` config key (default `false`) to
  the YAML config surface.

**Acceptance:** unlock from a loopback client spawns; unlock from a non-loopback
client is refused unless the opt-in flag is set; other modes unaffected.

---

## Step 3 — Detection + retry chain (ui/app.js)

**File:** `ui/app.js`

- While a `cursor` login session is streaming, scan decoded terminal output for
  the locked-keychain signature (match `keychain is locked`, case-insensitive,
  on the accumulated text — robust to ANSI).
- On match, surface an **"Unlock keychain & retry"** affordance in the auth
  panel (do not auto-run — the user must consent to typing their password).
- On click: open an `mode="unlock"` auth session in the same panel (reuse
  `openAuthPanel`'s WS/SSE transport branch). User types the keychain password
  at the prompt.
- On unlock `auth.done` with `returncode === 0`: automatically re-run the
  original `cursor` login (`openAuthPanel('cursor', originalOnSuccessRetry, …)`).
  On non-zero: show the failure and leave the retry button.
- Keep this generic-but-gated: offer unlock whenever the signature appears, but
  the server's loopback gate is the real authority — if the server refuses
  (`unlock_requires_local`), surface that message instead of prompting.

**Acceptance:** a locked-keychain cursor login shows the unlock affordance;
completing the unlock (exit 0) auto-retries and the cursor login proceeds; a
remote client sees the refusal message, not a password prompt. Bump the PWA
version marker.

---

## Step 4 — Tests

- `tests/test_realtime.py`: unlock mode spawns the fixed argv on darwin (monkey-
  patch `_spawn_pty`); the loopback gate refuses a simulated non-loopback client
  and allows loopback; non-darwin raises. Assert the unlock session's input is
  not persisted (same fire-and-forget guarantees as other auth input).
- `tests/e2e/chat.spec.js`: mock a cursor `auth.output` carrying the locked-
  keychain signature → assert the unlock affordance appears; mock unlock
  `auth.done` `returncode: 0` → assert the cursor login is re-issued; mock the
  server `unlock_requires_local` refusal → assert the message shows and no
  password prompt is presented.

**Acceptance:** new tests pass; existing auth tests stay green.

---

## Step 5 — Docs

- `docs/realtime-protocol-v1.md`: document `mode="unlock"` and the loopback gate
  / `unlock_requires_local` error.
- `docs/decisions/0035-cli-auth-sessions-via-scoped-pty.md`: amend to cover the
  unlock remediation, the in-session-unlock rationale (no reboot), and the
  loopback-only default with the `auth.allow_remote_keychain_unlock` opt-in.

**Acceptance:** protocol doc and ADR-0035 reflect the new mode and its security
posture.

---

## Verification gate

Human check on a real Mac with a locked login keychain: trigger a `cursor-agent
login`, confirm the unlock affordance appears, type the keychain password in the
panel, and confirm cursor login then completes **without restarting the server**.
Confirm a non-loopback (tailnet) client is refused by default.

## Out of scope

Unlocking non-login keychains; storing/caching the keychain password; any
keychain interaction for harnesses other than the cursor remediation trigger.
