#!/usr/bin/env bash
# ============================================================================
# OpenComputer gateway — macOS LaunchAgent uninstaller
# ============================================================================
# Removes the LaunchAgent installed by ``install.sh``. Idempotent.
# ============================================================================

set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: macOS-only." >&2
    exit 1
fi

PLIST="$HOME/Library/LaunchAgents/com.opencomputer.gateway.plist"

if [[ ! -f "$PLIST" ]]; then
    echo "✓ Nothing to uninstall — $PLIST does not exist."
    exit 0
fi

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

echo "✓ Removed $PLIST and unloaded the LaunchAgent."
echo "  Logs at ~/.opencomputer/logs/gateway.launchd.*.log are preserved —"
echo "  delete manually if you want them gone."
