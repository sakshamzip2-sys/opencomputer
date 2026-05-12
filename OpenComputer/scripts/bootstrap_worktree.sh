#!/usr/bin/env bash
# Bootstrap a fresh git worktree of OpenComputer so that
# ``uv tool install --editable .`` succeeds.
#
# pyproject.toml force-includes two build-artifact dirs that are
# gitignored:
#   * opencomputer/ui-tui/dist/             (Ink TUI build, ~MB)
#   * opencomputer/dashboard/static/spa/    (Vite dashboard SPA)
#
# A freshly-cloned worktree does NOT have those, so the hatchling
# editable install bails with::
#
#   FileNotFoundError: Forced include not found: .../ui-tui/dist
#
# This script creates symlinks from the worktree to the matching dirs
# in a "donor" checkout (defaulting to the main checkout next door).
# The symlinks are gitignored automatically because the leaf names
# (``dist``, ``spa``) are covered by repo-level ignore rules.
#
# Usage:
#   ./scripts/bootstrap_worktree.sh                    # use sibling main checkout
#   ./scripts/bootstrap_worktree.sh /path/to/main      # use explicit donor

set -euo pipefail

# Resolve worktree root (parent of scripts/).
WORKTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DONOR="${1:-}"

# Auto-discover donor: the parent's "OpenComputer" sibling when the worktree
# itself is named "OpenComputer-*/OpenComputer". Override with $1.
if [ -z "$DONOR" ]; then
  # Walk up to find a sibling "OpenComputer" that is NOT the current worktree.
  PARENT="$(dirname "$WORKTREE_ROOT")"
  while [ "$PARENT" != "/" ] && [ "$PARENT" != "$HOME" ]; do
    CANDIDATE="$(dirname "$PARENT")/claude/OpenComputer"
    if [ -d "$CANDIDATE" ] && [ "$CANDIDATE" != "$WORKTREE_ROOT" ]; then
      DONOR="$CANDIDATE"
      break
    fi
    PARENT="$(dirname "$PARENT")"
  done
fi

if [ -z "$DONOR" ] || [ ! -d "$DONOR" ]; then
  echo "error: could not find a donor OpenComputer checkout."
  echo "       Pass the path explicitly:"
  echo "         $0 /path/to/OpenComputer"
  exit 1
fi

echo "worktree: $WORKTREE_ROOT"
echo "donor:    $DONOR"

link_if_missing() {
  local rel="$1"
  local target="$DONOR/$rel"
  local link="$WORKTREE_ROOT/$rel"

  if [ ! -e "$target" ]; then
    echo "  skip ($rel): donor doesn't have it either — you may need to build it first"
    return
  fi

  # Already a valid symlink or directory? Leave it alone.
  if [ -L "$link" ]; then
    echo "  ok   ($rel): already a symlink → $(readlink "$link")"
    return
  fi
  if [ -d "$link" ] && [ "$(ls -A "$link" 2>/dev/null | wc -l)" -gt 0 ]; then
    echo "  ok   ($rel): real directory present, skipping symlink"
    return
  fi

  # Empty dir or absent — symlink it.
  if [ -d "$link" ] && [ "$(ls -A "$link" 2>/dev/null | wc -l)" -eq 0 ]; then
    rmdir "$link"
  fi
  mkdir -p "$(dirname "$link")"
  ln -s "$target" "$link"
  echo "  link ($rel) → $target"
}

link_if_missing "opencomputer/ui-tui/dist"
link_if_missing "opencomputer/dashboard/static/spa"

echo
echo "Done. You can now run:"
echo "  uv tool install --force --reinstall --editable $WORKTREE_ROOT"
