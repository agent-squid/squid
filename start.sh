#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# ── context symlink ──────────────────────────────────────────────────────────
ln -sfn "$SCRIPT_DIR/context" /tmp/squid

# ── check venv ───────────────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/uvicorn" ]]; then
  echo "Dependencies not installed. Running install.sh first..."
  bash "$SCRIPT_DIR/install.sh"
fi

# ── start ────────────────────────────────────────────────────────────────────
echo "Starting squid on http://${HOST}:${PORT}"
RELOAD=""
[[ "${DEV:-}" == "1" ]] && RELOAD="--reload"

exec "$VENV/bin/uvicorn" agent.server:app \
  --host "$HOST" \
  --port "$PORT" \
  --app-dir "$SCRIPT_DIR" \
  $RELOAD
