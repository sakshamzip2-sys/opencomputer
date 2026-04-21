"""
Plugin loader — Phase 2 of the two-phase pattern.

Given a PluginCandidate (from discovery.py), lazily import the entry
module and call its register() function. Plugins register their tools,
channel adapters, provider adapters, and hooks with the core registries.

Plugins declare their entry module in plugin.json via the `entry` field
(e.g. `"entry": "src.plugin"`). We import that module — it must export
a `register(api)` function where `api` exposes the plugin-facing registries.
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.plugins.discovery import PluginCandidate

logger = logging.getLogger("opencomputer.plugins.loader")


@dataclass(slots=True)
class LoadedPlugin:
    """Record of an activated plugin."""

    candidate: PluginCandidate
    module: Any


class PluginAPI:
    """Passed to each plugin's register() — the narrow runtime surface."""

    def __init__(
        self,
        tool_registry: Any,
        hook_engine: Any,
        provider_registry: dict[str, Any],
        channel_registry: dict[str, Any],
    ) -> None:
        self.tools = tool_registry
        self.hooks = hook_engine
        self.providers = provider_registry
        self.channels = channel_registry

    def register_tool(self, tool: Any) -> None:
        self.tools.register(tool)

    def register_hook(self, spec: Any) -> None:
        self.hooks.register(spec)

    def register_provider(self, name: str, provider: Any) -> None:
        self.providers[name] = provider

    def register_channel(self, name: str, adapter: Any) -> None:
        self.channels[name] = adapter


def load_plugin(candidate: PluginCandidate, api: PluginAPI) -> LoadedPlugin | None:
    """Import a candidate's entry module and call its register(api) function."""
    manifest = candidate.manifest
    entry = manifest.entry.strip()
    if not entry:
        logger.warning("plugin '%s' has no 'entry' field in manifest", manifest.id)
        return None

    # Add the plugin root to sys.path so relative imports work,
    # then import the entry module by name.
    plugin_root = str(candidate.root_dir.resolve())
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)

    try:
        module = importlib.import_module(entry)
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to import plugin '%s' (entry=%s): %s", manifest.id, entry, e)
        return None

    register_fn = getattr(module, "register", None)
    if register_fn is None:
        logger.warning(
            "plugin '%s' has no register() function in entry module %s",
            manifest.id,
            entry,
        )
        return None

    try:
        register_fn(api)
    except Exception as e:  # noqa: BLE001
        logger.exception("plugin '%s' register() raised: %s", manifest.id, e)
        return None

    logger.info("loaded plugin '%s' v%s", manifest.id, manifest.version)
    return LoadedPlugin(candidate=candidate, module=module)


__all__ = ["PluginAPI", "LoadedPlugin", "load_plugin"]
