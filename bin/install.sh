#!/usr/bin/env bash
set -euo pipefail

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}!${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET}  $1"; }
step() { echo -e "\n${BOLD}$1${RESET}"; }

# ── helpers ─────────────────────────────────────────────────────────────────
need_version() {
  # need_version <cmd> <min_major> <min_minor> <version_flag>
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

# ── banner ──────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}squid — install${RESET}"
echo    "  Sets up all runtime dependencies for the squid chat server."

# ════════════════════════════════════════════════════════════════════════════
step "1 / 2  Coding agents (at least one required)"
# ════════════════════════════════════════════════════════════════════════════

AGENTS_FOUND=0
if command -v claude &>/dev/null; then
  ok "claude $(claude --version 2>/dev/null || echo '(unknown version)')"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "claude not found — install with: npm install -g @anthropic-ai/claude-code"
fi

if command -v codex &>/dev/null; then
  ok "codex found"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "codex not found  — install with: npm install -g @openai/codex"
fi

if command -v cursor-agent &>/dev/null; then
  ok "cursor-agent found"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "cursor-agent not found — install with: curl https://cursor.com/install -fsS | bash"
fi

if [[ $AGENTS_FOUND -eq 0 ]]; then
  fail "No coding agents found. Install at least one (claude, codex, or cursor-agent) before starting squid."
  mark_error
fi

# ════════════════════════════════════════════════════════════════════════════
step "2 / 2  Python environment"
# ════════════════════════════════════════════════════════════════════════════

# Find a suitable python (3.11+)
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  if need_version "$candidate" 3 9 "--version"; then
    PYTHON=$candidate
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  fail "Python >= 3.9 not found."
  echo "     Install via https://python.org or:"
  echo "       brew install python@3.13"
  mark_error
else
  ok "$PYTHON ($($PYTHON --version))"

  VENV_DIR="$(cd "$(dirname "$0")/.." && pwd)/.venv"

  if [[ -d "$VENV_DIR" ]]; then
    ok "virtualenv already exists (.venv)"
  else
    echo "     Creating virtualenv …"
    "$PYTHON" -m venv "$VENV_DIR"
    ok "virtualenv created (.venv)"
  fi

  echo "     Installing Python dependencies …"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet fastapi "uvicorn[standard]" httpx pyyaml
  ok "Python dependencies installed"
fi

# ════════════════════════════════════════════════════════════════════════════
echo ""
if [[ $ERRORS -gt 0 ]]; then
  fail "${ERRORS} prerequisite(s) missing — fix the errors above, then re-run ./install.sh"
  exit 1
else
  ok "All dependencies ready."
  echo ""
  echo -e "  ${BOLD}Start squid:${RESET}"
  echo "    bin/start.sh"
  echo ""
  echo -e "  ${BOLD}Then open:${RESET}  http://127.0.0.1:8000"
  echo ""
fi
