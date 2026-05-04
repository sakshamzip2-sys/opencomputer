"""llama.cpp local-server provider plugin — entry point.

Flat layout: plugin.py is the entry, sibling provider.py is importable
via plain name because the plugin loader puts the plugin root on sys.path.
"""

from __future__ import annotations

try:
    from provider import LlamaCppServerProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.llama_cpp_server_provider.provider import (
        LlamaCppServerProvider,
    )  # package mode


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("llama-cpp-server", LlamaCppServerProvider)
