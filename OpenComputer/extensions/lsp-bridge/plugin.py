"""lsp-bridge — register the LspDiagnostics tool.

The plugin loader puts this directory on ``sys.path`` for the duration
of the import, so sibling modules import as plain names. See
``OpenComputer/opencomputer/plugins/loader.py`` for the synthetic-module
machinery that prevents collisions with same-named modules in other
plugins.
"""

from __future__ import annotations

from lsp_diagnostics_tool import LspDiagnostics  # type: ignore[import-not-found]


def register(api) -> None:  # noqa: D401 — duck-typed PluginAPI
    """Register the single LspDiagnostics tool."""
    api.register_tool(LspDiagnostics())
