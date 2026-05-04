#!/usr/bin/env bash
# Build the OpenComputer Browser Control extension to dist/background.js.
#
# Requires npm + esbuild. Run from this directory:
#   bash build.sh
#
# Or via npm:
#   npm install
#   npm run build
#
# Output: dist/background.js (the MV3 service worker bundle).

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d node_modules ]]; then
    echo "[opencomputer-extension-build] installing dependencies..."
    npm install --no-audit --no-fund --silent
fi

echo "[opencomputer-extension-build] building dist/background.js..."
npm run build --silent

echo "[opencomputer-extension-build] done"
ls -la dist/
