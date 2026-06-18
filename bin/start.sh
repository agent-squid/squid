#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"

# ── bootstrap config ─────────────────────────────────────────────────────────
CONFIG="$ROOT/config/squid.yaml"
if [[ ! -f "$CONFIG" && -f "$ROOT/config/squid.yaml.example" ]]; then
  cp "$ROOT/config/squid.yaml.example" "$CONFIG"
  echo "Created config/squid.yaml from example — edit it to customise."
fi

# ── per-user tmp dir ─────────────────────────────────────────────────────────
# /tmp/<user>/squid must be a real directory (not a symlink) so Claude Code
# uses it as-is rather than resolving back to ~/Work/squid and loading the
# wrong CLAUDE.md scope. Initial rsync happens in agent/context_sync.py at startup.
mkdir -p "/tmp/$(whoami)/squid"

# ── user data dir ────────────────────────────────────────────────────────────
mkdir -p "$HOME/.squid"

# ── check venv ───────────────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/uvicorn" ]]; then
  echo "Dependencies not installed. Running install.sh first..."
  bash "$SCRIPT_DIR/install.sh"
fi

# ── read port from config ────────────────────────────────────────────────────
PORT=$("$VENV/bin/python3" -c \
  "import yaml; c=yaml.safe_load(open('$CONFIG')); print(c['server']['port'])" \
  2>/dev/null || echo "8000")

# ── tailscale serve ──────────────────────────────────────────────────────────
# Expose squid on the Tailscale network via HTTPS. This is a one-time persistent
# config — Tailscale remembers it across reboots.
if command -v tailscale &>/dev/null; then
  if tailscale serve status 2>/dev/null | grep -q "127.0.0.1:${PORT}"; then
    echo "tailscale serve: already configured → https://$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","<machine-name>").rstrip("."))' 2>/dev/null || echo '<machine-name>')/"
  else
    if tailscale serve --bg "127.0.0.1:${PORT}" 2>/dev/null; then
      echo "tailscale serve: configured → https://$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","<machine-name>").rstrip("."))' 2>/dev/null || echo '<machine-name>')/"
    else
      echo "warning: tailscale serve failed — squid will run locally only (127.0.0.1:${PORT})."
      echo "  To enable remote access later, run:"
      echo "    tailscale serve --bg 127.0.0.1:${PORT}"
    fi
  fi
fi

# ── start ────────────────────────────────────────────────────────────────────
PID_FILE="$ROOT/.squid.pid"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/server.log"
mkdir -p "$LOG_DIR"

FORCE=0
for arg in "$@"; do [[ "$arg" == "--force" || "$arg" == "--restart" ]] && FORCE=1; done

if [[ "$FORCE" == "1" ]]; then
  # Kill by PID file
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "stopping squid (PID $(cat "$PID_FILE"))..."
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 1
  fi
  # Also kill anything still holding the port (handles stale PID file)
  OLD_PID=$(lsof -ti tcp:"${PORT}" 2>/dev/null || true)
  if [[ -n "$OLD_PID" ]]; then
    echo "killing process on port ${PORT} (PID $OLD_PID)..."
    kill $OLD_PID 2>/dev/null || true
    sleep 1
  fi
elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "squid is already running (PID $(cat "$PID_FILE")). Use --restart to restart."
  exit 0
fi

RELOAD=""
[[ "${DEV:-}" == "1" ]] && RELOAD="--reload"

cd "$ROOT"
nohup "$VENV/bin/python" -m agent.server $RELOAD >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# ── health check ─────────────────────────────────────────────────────────────
echo -n "starting squid"
for i in {1..20}; do
  sleep 0.5
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo ""
    echo "squid is up → http://127.0.0.1:${PORT}  (log: $LOG_FILE)"
    exit 0
  fi
  echo -n "."
done
echo ""
echo "warning: squid did not respond within 10 s — check $LOG_FILE"
