#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"

# ── context staging dir ──────────────────────────────────────────────────────
# /tmp/squid must be a real directory (not a symlink) so Claude Code uses it
# as-is rather than resolving back to ~/Work/squid and loading the wrong
# CLAUDE.md scope. Initial rsync happens in agent/context_sync.py at startup.
mkdir -p /tmp/squid

# ── check venv ───────────────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/uvicorn" ]]; then
  echo "Dependencies not installed. Running install.sh first..."
  bash "$SCRIPT_DIR/install.sh"  # install.sh is in the same bin/ dir
fi

# ── tailscale serve ──────────────────────────────────────────────────────────
# Expose squid on the Tailscale network via a plain-HTTP serve on the same port.
# This is a one-time persistent config — Tailscale remembers it across reboots.
# squid itself always binds to 127.0.0.1; tailscale serve is the bridge.
if command -v tailscale &>/dev/null; then
  PORT=$("$VENV/bin/python3" -c \
    "import yaml; c=yaml.safe_load(open('$ROOT/config/squid.yaml')); print(c['server']['port'])" \
    2>/dev/null || echo "8000")
  if tailscale serve status 2>/dev/null | grep -q "http://127.0.0.1:${PORT}"; then
    echo "tailscale serve: already configured (port ${PORT})"
  else
    if tailscale serve --bg --http="${PORT}" "127.0.0.1:${PORT}" 2>/dev/null; then
      echo "tailscale serve: configured (port ${PORT})"
    else
      echo "warning: tailscale serve failed — squid will run locally only."
      echo "  To enable remote access, run:"
      echo "    tailscale serve --bg --http=${PORT} 127.0.0.1:${PORT}"
    fi
  fi
fi

# ── start ────────────────────────────────────────────────────────────────────
PID_FILE="$ROOT/.squid.pid"
LOG_FILE="/tmp/squid-server.log"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "squid is already running (PID $(cat "$PID_FILE"))"
  exit 0
fi

RELOAD=""
[[ "${DEV:-}" == "1" ]] && RELOAD="--reload"

cd "$ROOT"
nohup "$VENV/bin/python" -m agent.server $RELOAD >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "squid started (PID $!, log: $LOG_FILE)"
