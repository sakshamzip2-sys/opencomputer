"""dev-tools plugin — entry point.

Registers three tools the agent can call when working on a developer's
machine:
- Diff   — git diff (working / staged / vs ref)
- Browser — fetch a JS-rendered page via Playwright (optional dep)
- Fal    — fal.ai REST API for image / video / model generation

Each tool is in its own file at the plugin root (flat layout — see
project CLAUDE.md §7.1 for why we avoid tools/ subdirectories in
plugins). The plugin loader clears `(provider, adapter, plugin, hooks,
handlers)` from sys.modules between loads but NOT `tools` — flat layout
keeps namespaces isolated by design.
"""

from __future__ import annotations

# Dual-import pattern — first form works when the plugin loader has
# inserted this folder onto sys.path (the production path), second form
# works when running from the OpenComputer test suite which imports
# directly via the package path. Mirrors `extensions/discord/plugin.py`.
try:
    from browser_tool import BrowserTool
    from diff_tool import DiffTool
    from fal_tool import FalTool
except ImportError:  # pragma: no cover
    from extensions.dev_tools.browser_tool import BrowserTool
    from extensions.dev_tools.diff_tool import DiffTool
    from extensions.dev_tools.fal_tool import FalTool


def register(api) -> None:  # PluginAPI is duck-typed
    """Register the three dev-tools with the agent's tool registry."""
    api.register_tool(DiffTool())
    api.register_tool(BrowserTool())
    api.register_tool(FalTool())
