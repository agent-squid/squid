#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/.squid.pid"

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm -f "$PID_FILE"
    echo "squid stopped (PID $PID)"
    exit 0
  else
    rm -f "$PID_FILE"
  fi
fi

# Fallback: find by venv path
if pkill -f "$ROOT/.venv/bin/python" 2>/dev/null; then
  echo "squid stopped"
else
  echo "squid is not running"
  exit 1
fi
