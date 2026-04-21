"""
Anthropic provider plugin — entry point.

Registers the AnthropicProvider class with OpenComputer. Selected by
setting config.model.provider = "anthropic" (default).

Supported env vars:
    ANTHROPIC_API_KEY        — required. API key or proxy key.
    ANTHROPIC_BASE_URL       — optional. Override endpoint (proxies).
    ANTHROPIC_AUTH_MODE      — optional. "x-api-key" (default) or "bearer".
"""

from __future__ import annotations

try:
    from provider import AnthropicProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.anthropic_provider.provider import AnthropicProvider  # package mode


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("anthropic", AnthropicProvider)
