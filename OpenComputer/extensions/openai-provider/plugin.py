"""
OpenAI provider plugin — entry point.

Flat layout: plugin.py is the entry, sibling modules are importable via
plain names because the plugin loader puts the plugin root on sys.path.
"""

from __future__ import annotations

try:
    from provider import OpenAIProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.openai_provider.provider import OpenAIProvider  # package mode


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("openai", OpenAIProvider)
