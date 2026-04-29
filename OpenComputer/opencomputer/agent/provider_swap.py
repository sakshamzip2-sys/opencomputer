"""Mid-session provider swap support (Sub-project D of model-agnosticism).

Looks up a registered provider class by name, instantiates it, and
returns the instance ready for the agent loop to use. Used by
``/provider <name>`` and the cross-provider variant of ``/model``.

Provider plugins today take ``__init__()`` with no required positional
args (env-based config); they raise RuntimeError if their env var is
missing. Both Anthropic and OpenAI providers accept optional
``api_key=`` / ``base_url=`` kwargs but defaulting to env keeps the
swap closure simple.
"""
from __future__ import annotations

from typing import Any


def lookup_provider(name: str) -> Any:
    """Return a freshly-constructed provider instance by registered name.

    Args:
        name: provider id as registered via ``api.register_provider(name, cls)``.

    Returns:
        A new provider instance ready to plug into ``AgentLoop.provider``.

    Raises:
        ValueError: if ``name`` is not registered. The message includes the
            list of registered providers so the caller can echo it to the user.
        RuntimeError: if the provider's __init__ raises (e.g. missing API key
            env var). Re-raised so the swap closure surfaces the cause.
    """
    from opencomputer.plugins.registry import registry

    provider_cls = registry.providers.get(name)
    if provider_cls is None:
        known = sorted(registry.providers.keys())
        raise ValueError(
            f"unknown provider {name!r} — registered: {known or '(none)'}"
        )
    # All bundled provider plugins accept zero-arg __init__. If a future
    # provider needs config, the registry-level config_schema validation
    # at register-time enforces shape.
    return provider_cls()


__all__ = ["lookup_provider"]
