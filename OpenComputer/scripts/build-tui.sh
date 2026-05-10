#!/usr/bin/env bash
# Build the Ink+React TUI as a self-contained bundle.
# Output: opencomputer/ui-tui/dist/entry.js (a single ~1.7 MB ESM file).
#
# 2026-05-08: switched from `tsc`-only to `esbuild --bundle`. The previous
# build emitted a multi-file dist that imported `react`, `ink`, etc. at
# runtime — but `node_modules/` is NOT shipped in the wheel, so the
# installed CLI failed with "Cannot find package 'react'". Bundling
# inlines all imports into one file (with a tiny `react-devtools-core`
# stub aliased at bundle time, since ink only loads it for browser
# devtools support which we don't need in a CLI).
#
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

echo "[build-tui] npm run bundle (esbuild)"
npm run bundle

if [ ! -f "${DIST}/entry.js" ]; then
  echo "ERROR: TUI entry not produced at ${DIST}/entry.js" >&2
  exit 1
fi

# Sanity: make sure the bundle is the real thing, not the legacy stub
# that earlier installs accidentally shipped. The stub is < 500 bytes;
# the real bundle is > 100 KB.
size=$(wc -c < "${DIST}/entry.js" | tr -d ' ')
if [ "${size}" -lt 100000 ]; then
  echo "ERROR: dist/entry.js is only ${size} bytes — bundling did not run correctly." >&2
  exit 1
fi

# Move TUI dist into the python package so it ships in the wheel.
PKG_TUI="${OC_ROOT}/opencomputer/ui-tui/dist"
mkdir -p "${PKG_TUI%/*}"
rm -rf "${PKG_TUI}"
cp -r "${DIST}" "${PKG_TUI}"

# ESM marker — the wheel's `dist/` has no enclosing package.json, so
# Node would treat `entry.js` as CommonJS without this. Our bundle is
# ESM (top-level await in `ink/yoga-layout` forces it).
echo '{"type":"module"}' > "${PKG_TUI}/package.json"

echo "[build-tui] OK — ${PKG_TUI}/entry.js (${size} bytes)"
