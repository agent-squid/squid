#!/usr/bin/env bash
set -euo pipefail

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}!${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET}  $1"; }

# ── helpers ─────────────────────────────────────────────────────────────────
need_version() {
  local cmd=$1 maj=$2 min=$3 flag=${4:---version}
  if ! command -v "$cmd" &>/dev/null; then return 1; fi
  local raw
  raw=$("$cmd" $flag 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
  local vmaj vmin
  vmaj=$(echo "$raw" | cut -d. -f1)
  vmin=$(echo "$raw" | cut -d. -f2)
  [[ "$vmaj" -gt "$maj" ]] || ( [[ "$vmaj" -eq "$maj" ]] && [[ "$vmin" -ge "$min" ]] )
}

ERRORS=0
mark_error() { ERRORS=$((ERRORS + 1)); }

SQUID_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── banner ──────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}squid — install${RESET}\n"

# ── python + squid package ───────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  if need_version "$candidate" 3 9 "--version"; then
    PYTHON=$candidate
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  fail "Python >= 3.9 not found — brew install python@3.13"
  mark_error
  echo ""
  fail "${ERRORS} prerequisite(s) missing — fix the errors above, then re-run ./install.sh"
  exit 1
fi

VENV_DIR="$SQUID_DIR/.venv"
[[ -d "$VENV_DIR" ]] || "$PYTHON" -m venv "$VENV_DIR" &>/dev/null
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$SQUID_DIR"

# ── coding agents (detected via squid's own finder) ──────────────────────────
AGENTS_JSON=$(PYTHONPATH="$SQUID_DIR" "$VENV_DIR/bin/python3" -c "
import json, sys
from agent.config import find_cli, CLAUDE_CLI, CODEX_CLI, CURSOR_CLI
print(json.dumps({
    'claude':       find_cli(CLAUDE_CLI),
    'codex':        find_cli(CODEX_CLI),
    'cursor-agent': find_cli(CURSOR_CLI),
}))
")

get_path() { "$VENV_DIR/bin/python3" -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get(sys.argv[2]) or '')" "$AGENTS_JSON" "$1" 2>/dev/null || true; }

AGENTS_FOUND=0
for agent_name in claude codex cursor-agent; do
  path="$(get_path "$agent_name")"
  if [[ -n "$path" ]]; then
    AGENTS_FOUND=$((AGENTS_FOUND + 1))
    if [[ "$agent_name" == "claude" ]]; then
      ok "claude $("$path" --version 2>/dev/null || echo '')"
    else
      ok "$agent_name"
    fi
  else
    case "$agent_name" in
      claude)       warn "claude not found — npm install -g @anthropic-ai/claude-code" ;;
      codex)        warn "codex not found  — npm install -g @openai/codex" ;;
      cursor-agent) warn "cursor-agent not found — curl https://cursor.com/install -fsS | bash" ;;
    esac
  fi
done

echo ""
ok "squid installed"

if [[ $AGENTS_FOUND -eq 0 ]]; then
  echo ""
  fail "No coding agents found. Install at least one before starting squid."
  mark_error
fi

# ── result ───────────────────────────────────────────────────────────────────
echo ""
if [[ $ERRORS -gt 0 ]]; then
  fail "${ERRORS} prerequisite(s) missing — fix the errors above, then re-run ./install.sh"
  exit 1
else
  USER_CONFIG="$HOME/.squid/squid.yaml"
  PORT=$("$VENV_DIR/bin/python3" -c "import yaml; print(yaml.safe_load(open('$USER_CONFIG'))['server']['port'])" 2>/dev/null || echo "8000")
  echo -e "  ${BOLD}Start:${RESET}  bin/start.sh"
  echo -e "  ${BOLD}Open:${RESET}   http://127.0.0.1:${PORT}"
  echo ""
fi
