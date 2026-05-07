#!/usr/bin/env bash
# Build the Ink+React TUI. Output: opencomputer/ui-tui/dist/entry.js.
# CI runs this after the dashboard SPA build.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TUI_DIR="${OC_ROOT}/ui-tui"
DIST="${TUI_DIR}/dist"

if [ ! -d "${TUI_DIR}" ]; then
  echo "ERROR: ${TUI_DIR} not found." >&2
  exit 1
fi

cd "${TUI_DIR}"

echo "[build-tui] npm ci"
if [ -f package-lock.json ]; then
  npm ci --no-audit --fund=false
else
  npm install --no-audit --fund=false
fi

echo "[build-tui] npm run build"
npm run build

if [ ! -f "${DIST}/entry.js" ]; then
  echo "ERROR: TUI entry not produced at ${DIST}/entry.js" >&2
  exit 1
fi

# Move TUI dist into the python package so it ships in the wheel
PKG_TUI="${OC_ROOT}/opencomputer/ui-tui/dist"
mkdir -p "${PKG_TUI%/*}"
rm -rf "${PKG_TUI}"
cp -r "${DIST}" "${PKG_TUI}"

echo "[build-tui] OK — ${PKG_TUI}/entry.js"
