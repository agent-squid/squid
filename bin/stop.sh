#!/usr/bin/env bash
set -euo pipefail

if pkill -f "python.*agent.server" 2>/dev/null; then
  echo "squid stopped"
else
  echo "squid is not running"
  exit 1
fi
