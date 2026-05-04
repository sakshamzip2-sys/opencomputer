#!/usr/bin/env bash
# Kill all agent-managed Chrome processes spawned by OpenComputer.
#
# Safe to run anytime — only kills processes that match
#   --user-data-dir=$HOME/.opencomputer/browser/
# so the user's real Chrome (at ~/Library/Application Support/Google/Chrome/)
# is never affected.
#
# Also kills lingering chrome-devtools-mcp Node processes and stale
# playwright/driver/node processes from hung pytest sessions.
#
# Usage:
#   bash scripts/cleanup_agent_chrome.sh
#
# Run this after any test session that may have spawned an agent-managed
# Chrome, until the production agent grows a proper shutdown hook
# (tracked: needs CLI `opencomputer browser stop --all` + atexit handler;
# v0.6 control-extension lease-lifecycle gives 30s idle close for free).

set -uo pipefail

CHROME_PATTERN="--user-data-dir=$HOME/.opencomputer/browser"

# Parent Chrome processes only — children (renderers, GPU, utility) die
# automatically when the parent goes.
chrome_pids=$(pgrep -f "Google Chrome.app/Contents/MacOS/Google Chrome.*$CHROME_PATTERN" 2>/dev/null || true)

if [[ -n "$chrome_pids" ]]; then
    echo "killing agent-managed Chrome parents:"
    ps -o pid,start,command -p $chrome_pids 2>/dev/null | head -20
    kill -TERM $chrome_pids 2>/dev/null || true
    sleep 2
    survivors=$(pgrep -f "Google Chrome.app/Contents/MacOS/Google Chrome.*$CHROME_PATTERN" 2>/dev/null || true)
    if [[ -n "$survivors" ]]; then
        echo "force-killing survivors: $survivors"
        kill -KILL $survivors 2>/dev/null || true
    fi
else
    echo "no agent-managed Chrome processes"
fi

mcp_pids=$(pgrep -f "chrome-devtools-mcp" 2>/dev/null || true)
if [[ -n "$mcp_pids" ]]; then
    echo "killing chrome-devtools-mcp: $mcp_pids"
    kill -TERM $mcp_pids 2>/dev/null || true
fi

pw_pids=$(pgrep -f "playwright/driver/node" 2>/dev/null || true)
if [[ -n "$pw_pids" ]]; then
    echo "killing stale playwright drivers: $pw_pids"
    kill -TERM $pw_pids 2>/dev/null || true
fi

echo "done"
