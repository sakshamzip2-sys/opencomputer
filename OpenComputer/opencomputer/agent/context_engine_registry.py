"""Registry for :class:`opencomputer.agent.context_engine.ContextEngine` factories.

Two registration paths:

1. **Built-in:** ``ContextCompressor`` is registered at import time via
   :func:`register`. It's the default "compressor" engine — the existing
   :class:`opencomputer.agent.compaction.CompactionEngine` wired into the
   ABC.

2. **Plugin-provided:** plugins under
   ``extensions/context-engine-<name>/plugin.py`` (or installed to
   ``~/.opencomputer/profiles/<p>/plugins/context-engine-<name>/``) call
   ``api.register_context_engine(name, factory)`` from their
   ``register(api)`` entry point. The plugin loader threads them through
   the same registry.

The agent loop calls :func:`get` once per session with the configured
name (default ``"compressor"``). Returning ``None`` for an unknown name
lets the loop fall back to the built-in compressor with a warning —
plugin not loaded shouldn't crash a chat session.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("opencomputer.agent.context_engine_registry")

#: Registry maps name → factory. The factory takes ``provider``,
#: ``model``, plus arbitrary kwargs and returns an instance that
#: implements :class:`ContextEngine`. Using a factory (not a class) lets
#: plugins ship engines that need extra construction args.
_registry: dict[str, Callable[..., Any]] = {}


def register(name: str, factory: Callable[..., Any]) -> None:
    """Register a context-engine factory under ``name``.

    Re-registering an existing name overwrites silently — plugins
    expecting to override builtins (e.g. a research engine that
    replaces the default compressor) get the natural behaviour.
    """
    _registry[name] = factory


def unregister(name: str) -> None:
    """Remove a registration. Used by tests; safe no-op if absent."""
    _registry.pop(name, None)


def list_engines() -> list[str]:
    """Return all registered engine names, sorted."""
    return sorted(_registry.keys())


def get(name: str) -> Callable[..., Any] | None:
    """Look up a factory by name. Returns ``None`` if not registered."""
    return _registry.get(name)


def build(name: str, /, **kwargs: Any) -> Any | None:
    """Construct an engine, returning ``None`` on unknown name.

    Convenience helper for the agent loop — passes ``**kwargs`` straight
    through to the factory. Logs a warning when the lookup fails so the
    user knows their config asked for an engine that wasn't loaded.
    """
    factory = _registry.get(name)
    if factory is None:
        logger.warning(
            "context_engine: %r not registered; available: %s",
            name, list_engines(),
        )
        return None
    return factory(**kwargs)


def _register_builtin_compressor() -> None:
    """Wire the existing ``CompactionEngine`` in as the default ``compressor``.

    Lazy import to avoid a circular dependency at module load — the
    compaction module imports from plugin_sdk and stdlib only, so we can
    safely import it inside the registration helper which itself only
    runs at import time of this registry module.
    """
    from opencomputer.agent.compaction import CompactionEngine

    register("compressor", CompactionEngine)


# Auto-register the default on first import. Plugins can override later.
_register_builtin_compressor()


__all__ = [
    "build",
    "get",
    "list_engines",
    "register",
    "unregister",
]
