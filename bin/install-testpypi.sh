#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <version>" >&2
  echo "example: $0 0.1.1" >&2
  exit 2
fi

VERSION="$1"
PACKAGE="agentsquid"
PIPX_VENV="${PACKAGE}-test"
TMPDIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

ensure_pipx() {
  if command -v python3 >/dev/null 2>&1; then
    export PATH="$(python3 -m site --user-base)/bin:$HOME/.local/bin:$PATH"
  fi

  if command -v pipx >/dev/null 2>&1; then
    return
  fi

  if command -v brew >/dev/null 2>&1; then
    brew install pipx
  else
    python3 -m pip install --user --quiet pipx
    python3 -m pipx ensurepath
    export PATH="$(python3 -m site --user-base)/bin:$HOME/.local/bin:$PATH"
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    echo "pipx install completed but pipx is still not on PATH; restart your shell and re-run this script." >&2
    exit 1
  fi
}

ensure_pipx

python3 -m pip download \
  --no-deps \
  --only-binary=:all: \
  --index-url https://test.pypi.org/simple/ \
  "${PACKAGE}==${VERSION}" \
  -d "$TMPDIR"

shopt -s nullglob
wheels=("$TMPDIR"/"${PACKAGE}"-"${VERSION}"-*.whl)
shopt -u nullglob

if [[ ${#wheels[@]} -ne 1 ]]; then
  echo "expected exactly one ${PACKAGE} ${VERSION} wheel from TestPyPI; found ${#wheels[@]}" >&2
  exit 1
fi

pipx install --suffix=-test --force "${wheels[0]}"
pipx runpip "$PIPX_VENV" show "$PACKAGE"

echo
echo "Installed TestPyPI ${PACKAGE} ${VERSION} as pipx app '${PIPX_VENV}'."
echo "Run 'pipx uninstall ${PIPX_VENV}' when done testing."
