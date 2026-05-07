#!/usr/bin/env bash
# Build the dashboard SPA. Runs in CI before packaging the wheel; users
# building from source do `cd OpenComputer && ./scripts/build-dashboard.sh`.
# Idempotent. Exits non-zero on build failure or missing artifact.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WEB_DIR="${OC_ROOT}/ui-web"
SPA_OUT="${OC_ROOT}/opencomputer/dashboard/static/spa"

if [ ! -d "${WEB_DIR}" ]; then
  echo "ERROR: ${WEB_DIR} not found." >&2
  exit 1
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
