#!/usr/bin/env bash
# Build the dashboard SPA. Runs in CI before packaging the wheel; users
# building from source do `cd OpenComputer && ./scripts/build-dashboard.sh`.
# Idempotent. Exits non-zero on build failure or missing artifact.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WEB_DIR="${OC_ROOT}/ui-web"
SPA_OUT="${OC_ROOT}/opencomputer/dashboard/static/spa"

# CI-tolerance (2026-05-14): the ``ui-web/`` source tree isn't tracked
# in this repo (lives separately or generated). When missing, stub the
# SPA output dir with a minimal index.html so the hatch force-include
# in pyproject.toml still finds something to ship and pip install
# succeeds. Local devs with ui-web/ checked out get the real build
# path below.
if [ ! -d "${WEB_DIR}" ]; then
  echo "[build-dashboard] ${WEB_DIR} not present — writing SPA stub for wheel packaging"
  mkdir -p "${SPA_OUT}/assets"
  if [ ! -f "${SPA_OUT}/index.html" ]; then
    cat > "${SPA_OUT}/index.html" <<'STUB'
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>OpenComputer Dashboard (stub)</title></head><body><pre>Dashboard SPA was not built. Install the ui-web source tree and re-run scripts/build-dashboard.sh.</pre></body></html>
STUB
  fi
  if [ ! -f "${SPA_OUT}/assets/.gitkeep" ]; then
    touch "${SPA_OUT}/assets/.gitkeep"
  fi
  echo "[build-dashboard] OK (stub) — ${SPA_OUT}/index.html"
  exit 0
fi

cd "${WEB_DIR}"

echo "[build-dashboard] npm ci"
if [ -f package-lock.json ]; then
  npm ci --no-audit --fund=false
else
  npm install --no-audit --fund=false
fi

echo "[build-dashboard] npm run build"
npm run build

if [ ! -f "${SPA_OUT}/index.html" ]; then
  echo "ERROR: SPA artifact not produced at ${SPA_OUT}/index.html" >&2
  exit 1
fi

echo "[build-dashboard] OK — ${SPA_OUT}/index.html"
