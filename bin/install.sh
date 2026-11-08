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
  raw=$("$cmd" "$flag" 2>/dev/null) || return 1
  [[ "$raw" =~ ^Python\ ([0-9]+)\.([0-9]+) ]] || return 1
  local vmaj=${BASH_REMATCH[1]} vmin=${BASH_REMATCH[2]}
  [[ "$vmaj" -gt "$maj" ]] || ( [[ "$vmaj" -eq "$maj" ]] && [[ "$vmin" -ge "$min" ]] )
}

# Only a terminal may authorize downloads. EOF is a decline, even though
# pressing Enter explicitly accepts the default.
confirm() {
  local answer
  [[ -t 0 ]] || return 1
  read -r -p "  $1 [Y/n] " answer || return 1
  case "$answer" in
    ""|y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

install_python() {
  local installer candidate
  if [[ -z "$UV" ]]; then
    confirm "Install uv and Python 3.13 for Squid?" || return 1
    installer=$(mktemp -t squid-uv-install.XXXXXX) || return 1
    # Download first so a failed transfer cannot execute a partial script.
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$installer"; then
      rm -f "$installer"
      return 1
    fi
    # Keep the executable location predictable and leave shell profiles alone.
    if ! env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"; then
      rm -f "$installer"
      return 1
    fi
    rm -f "$installer"
    [[ -x "$HOME/.local/bin/uv" ]] || return 1
    UV="$HOME/.local/bin/uv"
  else
    confirm "Install Python 3.13 with uv and continue?" || return 1
  fi
  "$UV" python install 3.13 || return 1
  candidate=$("$UV" python find --system --no-python-downloads 3.13) || return 1
  need_version "$candidate" 3 10 || return 1
  PYTHON=$candidate
  ok "Python ready: $PYTHON"
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
SQUID_HOME="$HOME/.squid"

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

if command -v pi &>/dev/null; then
  ok "pi $(pi --version 2>/dev/null || echo '')"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "pi not found — curl -fsSL https://pi.dev/install.sh | sh"
fi

if command -v agy &>/dev/null; then
  ok "agy"
  AGENTS_FOUND=$((AGENTS_FOUND + 1))
else
  warn "agy not found — curl -fsSL https://antigravity.google/cli/install.sh | bash"
fi

if [[ $AGENTS_FOUND -eq 0 ]]; then
  fail "No coding agents found. Install at least one before starting squid."
  mark_error
fi

# Ollama is optional and not itself a coding-agent harness (it's a local
# model provider Pi/OpenCode can point at, see ADR-0037) — its absence never
# counts toward AGENTS_FOUND or fails the install.
if command -v ollama &>/dev/null; then
  ok "ollama $(ollama --version 2>/dev/null || echo '')"
else
  warn "ollama not found — curl -fsSL https://ollama.com/install.sh | sh"
fi

# ── install squid ────────────────────────────────────────────────────────────
echo ""

PYTHON=""
for candidate in "$SQUID_DIR/.venv/bin/python" python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if need_version "$candidate" 3 10 "--version"; then
    PYTHON=$candidate
    break
  fi
done

# uv-managed interpreters need not be on PATH. Do not download anything during
# detection; downloads require confirmation below.
UV=""
if command -v uv &>/dev/null; then
  UV=$(command -v uv)
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV="$HOME/.local/bin/uv"
fi
if [[ -z "$PYTHON" && -n "$UV" ]]; then
  candidate=$("$UV" python find --system --no-python-downloads '>=3.10' 2>/dev/null || true)
  if [[ -n "$candidate" ]] && need_version "$candidate" 3 10; then
    PYTHON=$candidate
  fi
fi

if [[ -z "$PYTHON" ]]; then
  warn "Squid needs Python 3.10 or newer. No compatible Python was found."
  for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
      warn "Found $candidate at $(command -v "$candidate"): $("$candidate" --version 2>&1 || true)"
    fi
  done
  install_python || true
fi

if [[ -z "$PYTHON" ]]; then
  fail "A compatible Python is still needed to install Squid."
  echo ""
  echo "  Recommended: install Python 3.13 with uv (leaves system Python unchanged)."
  if [[ -z "$UV" ]]; then
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo '    "$HOME/.local/bin/uv" python install 3.13'
  else
    printf '    %q python install 3.13\n' "$UV"
  fi
  printf '  Then re-run: bash %q\n' "$SQUID_DIR/bin/install.sh"
  echo "  Python setup help: https://docs.astral.sh/uv/guides/install-python/"
  mark_error
else
  VENV_DIR="$SQUID_DIR/.venv"
  if [[ -d "$VENV_DIR" ]] && ! need_version "$VENV_DIR/bin/python" 3 10 "--version"; then
    warn "existing virtualenv has missing or incompatible Python — recreating it with $PYTHON"
    rm -rf "$VENV_DIR"
  fi
  [[ -x "$VENV_DIR/bin/python" ]] || run_quiet "create Python virtualenv" "$PYTHON" -m venv "$VENV_DIR"
  if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    run_quiet "bootstrap pip" "$VENV_DIR/bin/python" -m ensurepip --upgrade
  fi
  run_quiet "upgrade pip" "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
  run_quiet "install squid package" "$VENV_DIR/bin/python" -m pip install --quiet "$SQUID_DIR"
  mkdir -p "$SQUID_HOME/logs" "$SQUID_HOME/context"
  if [[ ! -f "$SQUID_HOME/squid.yaml" && -f "$SQUID_DIR/config/squid.yaml.example" ]]; then
    sed 's|/tmp/squid|/tmp/'"$(whoami)"'/squid|g' "$SQUID_DIR/config/squid.yaml.example" > "$SQUID_HOME/squid.yaml"
    ok "created ~/.squid/squid.yaml"
  fi
  if [[ -d "$SQUID_DIR/context" && -z "$(ls -A "$SQUID_HOME/context" 2>/dev/null)" ]]; then
    cp -r "$SQUID_DIR/context/." "$SQUID_HOME/context/"
    ok "seeded ~/.squid/context"
  fi
  ok "squid installed"
fi

# ── result ───────────────────────────────────────────────────────────────────
echo ""
if [[ $ERRORS -gt 0 ]]; then
  fail "${ERRORS} prerequisite(s) missing — fix the errors above, then re-run bin/install.sh"
  exit 1
else
  USER_CONFIG="$HOME/.squid/squid.yaml"
  PORT=$("$VENV_DIR/bin/python3" -c "import yaml; print(yaml.safe_load(open('$USER_CONFIG'))['server']['port'])" 2>/dev/null || echo "8000")
  # Configure tailscale serve now (not just on first start) so the URLs are
  # visible immediately after install, same block bin/start.sh runs on every
  # start — see that file for why the rules exist and how they're checked.
  PATH="$VENV_DIR/bin:$PATH" source "$SQUID_DIR/bin/tailscale-serve.sh"
  echo -e "  ${BOLD}Start:${RESET}  bin/start.sh"
  echo -e "  ${BOLD}Open:${RESET}   http://127.0.0.1:${PORT}"
  echo ""
fi
