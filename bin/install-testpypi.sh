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

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx is required. Install it first, then re-run this script." >&2
  exit 1
fi

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
