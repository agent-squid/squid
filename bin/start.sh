#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"

# ── ~/.squid user home ───────────────────────────────────────────────────────
SQUID_HOME="$HOME/.squid"
mkdir -p "$SQUID_HOME/logs" "$SQUID_HOME/context"

# config: bootstrap from example on first run, substituting the per-user tmp path
CONFIG="$SQUID_HOME/squid.yaml"
if [[ ! -f "$CONFIG" && -f "$ROOT/config/squid.yaml.example" ]]; then
  sed "s|/tmp/squid|/tmp/$(whoami)/squid|g" "$ROOT/config/squid.yaml.example" > "$CONFIG"
  echo "Created ~/.squid/squid.yaml from example — edit it to customise."
fi

# context: seed from install dir on first run (user can then edit ~/.squid/context/)
if [[ -d "$ROOT/context" && -z "$(ls -A "$SQUID_HOME/context" 2>/dev/null)" ]]; then
  cp -r "$ROOT/context/." "$SQUID_HOME/context/"
  echo "Seeded ~/.squid/context/ from install dir."
fi

# ── per-user tmp dir ─────────────────────────────────────────────────────────
# Must be a real directory (not a symlink) so Claude Code uses it as-is rather
# than resolving back to ~/Work/squid and loading the wrong CLAUDE.md scope.
mkdir -p "/tmp/$(whoami)/squid"

# ── check venv ───────────────────────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python" || ! -f "$VENV/bin/uvicorn" ]]; then
  echo "Dependencies not installed. Running install.sh first..."
  bash "$SCRIPT_DIR/install.sh"
fi

# A restored/moved virtualenv can retain launchers pointing at a deleted turn.
# Invoke Python directly so broken console-script shebangs cannot block repair.
"$VENV/bin/python" "$SCRIPT_DIR/repair-venv.py" "$VENV"

# ── read port from config ────────────────────────────────────────────────────
PORT=$("$VENV/bin/python3" -c \
  "import yaml; c=yaml.safe_load(open('$CONFIG')); print(c['server']['port'])" \
  2>/dev/null || echo "8000")

# ── tailscale serve ──────────────────────────────────────────────────────────
# Shared with install.sh, which runs the same block right after a fresh
# install so the URLs are visible immediately, not just on first start.
source "$SCRIPT_DIR/tailscale-serve.sh"

# ── start ────────────────────────────────────────────────────────────────────
PID_FILE="$ROOT/.squid.pid"
LOG_FILE="$SQUID_HOME/logs/server.log"
# agent/server.py owns LOG_FILE directly via a rotating handler (7-day
# retention). BOOT_LOG only catches crashes before that handler is set up
# (e.g. import errors) — truncated on every start, never accumulates.
BOOT_LOG="$SQUID_HOME/logs/boot.log"

print_pairing_help() {
  echo "If your browser asks to pair, run this on the Squid host:"
  printf '  (cd %q && %q -m agent.server pair-browser)\n' "$ROOT" "$VENV/bin/python"
  echo "Open the printed link within 5 minutes. Use /remote in the paired browser to add a Tailscale device."
}

active_process_count() (
  cd "$ROOT"
  "$VENV/bin/python" - "$PORT" <<'PYTHON'
import sys

import httpx

from agent.instance_auth import host_token

try:
    port = int(sys.argv[1])
    token = host_token(create=True)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
        result = client.get("/instance/active-process-count",
                            headers={"Authorization": f"Bearer {token}"})
        if result.status_code == 404:
            # The previous server version exposes only this endpoint.
            legacy = client.get("/processes")
            legacy.raise_for_status()
            processes = legacy.json()
            if not isinstance(processes, list):
                raise ValueError("unexpected process list")
            count = len(processes)
        else:
            result.raise_for_status()
            count = result.json()["count"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid process count")
        print(count)
except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
    sys.exit(1)
PYTHON
)

FORCE=0
for arg in "$@"; do [[ "$arg" == "--force" || "$arg" == "--restart" ]] && FORCE=1; done

if [[ "$FORCE" == "1" ]]; then
  # An unavailable count can never be treated as zero for a running instance.
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null ||
     lsof -ti tcp:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    CONFIRM_RESTART=0
    if ACTIVE_COUNT=$(active_process_count); then
      if [[ "$ACTIVE_COUNT" -gt 0 ]]; then
        echo "⚠ warning: $ACTIVE_COUNT prompt(s) still running."
        CONFIRM_RESTART=1
      fi
    else
      echo "⚠ warning: Could not determine whether prompts are running."
      CONFIRM_RESTART=1
    fi
    if [[ "$CONFIRM_RESTART" == "1" ]]; then
      read -r -p "Restart and interrupt any active prompts? [y/N] " CONFIRM || CONFIRM=
      [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
    fi
  fi
  # Kill by PID file
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "stopping squid (PID $(cat "$PID_FILE"))..."
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 1
  fi
  # Also kill anything still holding the port (handles stale PID file)
  OLD_PID=$(lsof -ti tcp:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "$OLD_PID" ]]; then
    echo "killing process on port ${PORT} (PID $OLD_PID)..."
    kill $OLD_PID 2>/dev/null || true
    sleep 1
  fi
elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "squid is already running (PID $(cat "$PID_FILE")). Use --restart to restart."
  print_pairing_help
  exit 0
fi

RELOAD=""
[[ "${DEV:-}" == "1" ]] && RELOAD="--reload"

cd "$ROOT"
PATH="$VENV/bin:$PATH" nohup "$VENV/bin/python" -m agent.server $RELOAD > "$BOOT_LOG" 2>&1 &
echo $! > "$PID_FILE"

# ── health check ─────────────────────────────────────────────────────────────
echo -n "starting squid"
for i in {1..20}; do
  sleep 0.5
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo ""
    echo "squid is up → http://127.0.0.1:${PORT}  (log: $LOG_FILE)"
    print_pairing_help
    exit 0
  fi
  echo -n "."
done
echo ""
echo "warning: squid did not respond within 10 s — check $BOOT_LOG (early failures) or $LOG_FILE"
exit 1
