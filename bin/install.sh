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

run_quiet() {
  local msg=$1
  shift
  local log_file
  log_file=$(mktemp -t squid-install.XXXXXX)
  "$@" >"$log_file" 2>&1 &
  local pid=$!
  local frames=("-" "\\" "|" "/")
  local i=0
  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  ${YELLOW}%s${RESET}  %s" "${frames[$((i % 4))]}" "$msg"
    i=$((i + 1))
    sleep 0.2
  done
  if wait "$pid"; then
    printf "\r\033[K"
    ok "$msg"
    rm -f "$log_file"
  else
    printf "\r\033[K"
    fail "$msg"
    sed -n '1,120p' "$log_file" >&2
    rm -f "$log_file"
    exit 1
  fi
}

SQUID_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── banner ──────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}squid — install${RESET}\n"

# ── coding agents ────────────────────────────────────────────────────────────
AGENTS_FOUND=0
if command -v claude &>/dev/null; then
  ok "claude $(claude --version 2>/dev/null || echo '')"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "claude not found — curl -fsSL https://claude.ai/install.sh | bash"
fi

if command -v codex &>/dev/null; then
  ok "codex"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "codex not found  — curl -fsSL https://chatgpt.com/codex/install.sh | sh"
fi

if command -v cursor-agent &>/dev/null; then
  ok "cursor-agent"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "cursor-agent not found — curl -fsS https://cursor.com/install | bash"
fi

if command -v opencode &>/dev/null; then
  ok "opencode"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "opencode not found — curl -fsSL https://opencode.ai/install | bash"
fi

if [[ $AGENTS_FOUND -eq 0 ]]; then
  fail "No coding agents found. Install at least one before starting squid."
  mark_error
fi

# ── install squid ────────────────────────────────────────────────────────────
echo ""

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
else
  VENV_DIR="$SQUID_DIR/.venv"
  [[ -d "$VENV_DIR" ]] || run_quiet "create Python virtualenv" "$PYTHON" -m venv "$VENV_DIR"
  run_quiet "upgrade pip" "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  run_quiet "install squid package" "$VENV_DIR/bin/pip" install --quiet "$SQUID_DIR"
  ok "squid installed"
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
