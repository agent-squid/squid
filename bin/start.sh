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
#
# Publishes at the tailnet's default HTTPS port (443) for the shortest
# possible URL. `tailscale serve` supports multiple concurrent rules at
# different ports on the same node, so other local services on this machine
# (e.g. oMLX or Ollama on their own ports) can be exposed the same way,
# independently of this rule:
#   tailscale serve --bg --https=<their-port> 127.0.0.1:<their-port>
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
LOG_FILE="$SQUID_HOME/logs/server.log"
# agent/server.py owns LOG_FILE directly via a rotating handler (7-day
# retention). BOOT_LOG only catches crashes before that handler is set up
# (e.g. import errors) — truncated on every start, never accumulates.
BOOT_LOG="$SQUID_HOME/logs/boot.log"

FORCE=0
for arg in "$@"; do [[ "$arg" == "--force" || "$arg" == "--restart" ]] && FORCE=1; done

if [[ "$FORCE" == "1" ]]; then
  # Warn if there are active prompts running
  ACTIVE_COUNT=$(curl -sf "http://127.0.0.1:${PORT}/processes" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "0")
  if [[ "$ACTIVE_COUNT" -gt 0 ]]; then
    echo "⚠ warning: $ACTIVE_COUNT prompt(s) still running."
    read -r -p "Restart anyway and kill them? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
  fi
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
nohup "$VENV/bin/python" -m agent.server $RELOAD > "$BOOT_LOG" 2>&1 &
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
echo "warning: squid did not respond within 10 s — check $BOOT_LOG (early failures) or $LOG_FILE"
exit 1
