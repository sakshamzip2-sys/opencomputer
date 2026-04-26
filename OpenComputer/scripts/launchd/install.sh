#!/usr/bin/env bash
# ============================================================================
# OpenComputer gateway — macOS LaunchAgent installer
# ============================================================================
# Installs ``com.opencomputer.gateway.plist`` to ``~/Library/LaunchAgents/``
# so ``opencomputer gateway`` starts automatically at login and restarts
# on crash. Substitutes the absolute path to the ``opencomputer`` binary
# at install time (LaunchAgent's PATH is sparse — we can't rely on $PATH
# resolving anything).
#
# Usage:
#   bash scripts/launchd/install.sh           # install + load
#   bash scripts/launchd/install.sh --dry-run # show what would happen
#
# Idempotent: re-running unloads the existing job before reloading.
# ============================================================================

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --help|-h)
            sed -n '/^# Usage:/,/^# Idempotent/p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ── Sanity ─────────────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: this installer is macOS-only (LaunchAgents are an Apple primitive)." >&2
    echo "       For Linux, use a systemd unit; for headless deployments use Docker." >&2
    exit 1
fi

OC_BIN=$(command -v opencomputer 2>/dev/null || true)
if [[ -z "$OC_BIN" ]]; then
    echo "Error: 'opencomputer' is not on PATH." >&2
    echo "       Install it first:  pip install opencomputer" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/com.opencomputer.gateway.plist.template"
if [[ ! -f "$TEMPLATE" ]]; then
    echo "Error: template not found at $TEMPLATE" >&2
    exit 1
fi

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS/com.opencomputer.gateway.plist"

# ── Render ─────────────────────────────────────────────────────────────────
RENDERED=$(sed \
    -e "s|{{OPENCOMPUTER_BIN}}|$OC_BIN|g" \
    -e "s|{{HOME}}|$HOME|g" \
    "$TEMPLATE")

if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] would write to: $PLIST"
    echo "[dry-run] would resolve OPENCOMPUTER_BIN -> $OC_BIN"
    echo "[dry-run] would resolve HOME -> $HOME"
    echo "[dry-run] would create $HOME/.opencomputer/logs/ if missing"
    echo "[dry-run] would unload-then-load via launchctl"
    echo ""
    echo "Rendered plist:"
    echo "$RENDERED"
    exit 0
fi

# ── Install ────────────────────────────────────────────────────────────────
mkdir -p "$LAUNCH_AGENTS" "$HOME/.opencomputer/logs"
echo "$RENDERED" > "$PLIST"
chmod 644 "$PLIST"

# Unload any pre-existing version so reload picks up the new plist.
# Ignore the "couldn't unload" error if it isn't loaded.
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

# Verify it's running. Sleep briefly for launchd to spawn the process.
sleep 2
if launchctl list | grep -q "com.opencomputer.gateway"; then
    echo "✓ com.opencomputer.gateway loaded — gateway will auto-start at login."
    echo "  Logs:    ~/.opencomputer/logs/gateway.launchd.{out,err}.log"
    echo "  Status:  launchctl list | grep opencomputer"
    echo "  Stop:    bash $SCRIPT_DIR/uninstall.sh"
else
    echo "! launchctl loaded the plist but the job isn't listed. Check:" >&2
    echo "  tail -50 ~/.opencomputer/logs/gateway.launchd.err.log" >&2
    exit 1
fi
