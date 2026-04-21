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
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.plugins.discovery import PluginCandidate

logger = logging.getLogger("opencomputer.plugins.loader")


# Common short names plugins use for their sibling files. Clearing these
# between plugin loads prevents two plugins (both with a top-level
# `provider.py`, say) from sharing the first-loaded module.
_PLUGIN_LOCAL_NAMES = ("provider", "adapter", "plugin", "handlers", "hooks")


def _clear_plugin_local_cache() -> None:
    for name in _PLUGIN_LOCAL_NAMES:
        sys.modules.pop(name, None)


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
        injection_engine: Any = None,
    ) -> None:
        self.tools = tool_registry
        self.hooks = hook_engine
        self.providers = provider_registry
        self.channels = channel_registry
        self.injection = injection_engine

    def register_tool(self, tool: Any) -> None:
        self.tools.register(tool)

    def register_hook(self, spec: Any) -> None:
        self.hooks.register(spec)

    def register_provider(self, name: str, provider: Any) -> None:
        self.providers[name] = provider

    def register_channel(self, name: str, adapter: Any) -> None:
        self.channels[name] = adapter

    def register_injection_provider(self, provider: Any) -> None:
        """Register a DynamicInjectionProvider (plan mode, yolo mode, etc.)."""
        if self.injection is None:
            raise RuntimeError(
                "Injection engine unavailable — plugin-SDK version mismatch?"
            )
        self.injection.register(provider)


def load_plugin(candidate: PluginCandidate, api: PluginAPI) -> LoadedPlugin | None:
    """Import a candidate's entry module and call its register(api) function.

    Uses importlib.util.spec_from_file_location with a unique synthetic module
    name per plugin (based on plugin id). This avoids Python's module cache
    returning the same module for multiple plugins that happen to share an
    `entry` value (e.g. all three plugins use "plugin" as their entry).

    Also adds the plugin root to sys.path so the entry module's own sibling
    imports (e.g. `from adapter import X`) resolve correctly.
    """
    manifest = candidate.manifest
    entry = manifest.entry.strip()
    if not entry:
        logger.warning("plugin '%s' has no 'entry' field in manifest", manifest.id)
        return None

    plugin_root = candidate.root_dir.resolve()
    plugin_root_str = str(plugin_root)
    if plugin_root_str not in sys.path:
        sys.path.insert(0, plugin_root_str)

    entry_path = plugin_root / f"{entry}.py"
    if not entry_path.exists():
        logger.warning(
            "plugin '%s' entry file not found: %s (expected at %s)",
            manifest.id,
            entry,
            entry_path,
        )
        return None

    # Clear common sibling module names from sys.modules so this plugin sees
    # its OWN siblings (not another plugin's cached 'provider' or 'adapter').
    # Without this, two plugins that both have a top-level 'provider' module
    # would share the one that loaded first.
    _clear_plugin_local_cache()

    # Unique module name so sys.modules doesn't collide between plugins
    synthetic_name = f"_opencomputer_plugin_{manifest.id.replace('-', '_')}_{entry}"

    try:
        spec = importlib.util.spec_from_file_location(synthetic_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no spec for {entry_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[synthetic_name] = module
        spec.loader.exec_module(module)
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
