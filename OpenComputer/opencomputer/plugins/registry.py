"""
Plugin registry — the active set of loaded plugins + the surfaces they registered.

Holds the provider registry and channel registry (tools go into the
tool registry from tools/registry.py; hooks go into the hook engine).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from opencomputer.agent.injection import engine as injection_engine
from opencomputer.hooks.engine import engine as hook_engine
from opencomputer.plugins.discovery import PluginCandidate, discover
from opencomputer.plugins.loader import LoadedPlugin, PluginAPI, load_plugin
from opencomputer.tools.registry import registry as tool_registry
from plugin_sdk.doctor import HealthContribution
from plugin_sdk.provider_contract import BaseProvider

logger = logging.getLogger("opencomputer.plugins.registry")


@dataclass(slots=True)
class PluginRegistry:
    """Holds all loaded plugins and the shared API they register into."""

    providers: dict[str, BaseProvider] = field(default_factory=dict)
    channels: dict[str, object] = field(default_factory=dict)
    loaded: list[LoadedPlugin] = field(default_factory=list)
    doctor_contributions: list[HealthContribution] = field(default_factory=list)

    def api(self) -> PluginAPI:
        return PluginAPI(
            tool_registry=tool_registry,
            hook_engine=hook_engine,
            provider_registry=self.providers,
            channel_registry=self.channels,
            injection_engine=injection_engine,
            doctor_contributions=self.doctor_contributions,
        )

    def load_all(
        self,
        search_paths: list[Path],
        enabled_ids: frozenset[str] | Literal["*"] | None = None,
    ) -> list[LoadedPlugin]:
        """Discover + activate plugins. Returns the successfully loaded ones.

        ``enabled_ids`` controls filtering (Phase 14.M integration):

        - ``None`` or ``"*"``: load everything discovered (pre-Phase-14
          behaviour, fully backward-compatible).
        - ``frozenset[str]``: load ONLY candidates whose id is in the set.
          Skipped candidates are logged at INFO — they stay visible via
          ``list_candidates`` for diagnostics.
        """
        candidates = discover(search_paths)
        api = self.api()
        wildcard = enabled_ids is None or enabled_ids == "*"
        for cand in candidates:
            if not wildcard:
                assert isinstance(enabled_ids, frozenset)
                if cand.manifest.id not in enabled_ids:
                    logger.info(
                        "skipping plugin '%s' (not in active enabled set)",
                        cand.manifest.id,
                    )
                    continue
            loaded = load_plugin(cand, api)
            if loaded:
                self.loaded.append(loaded)
        return self.loaded

    def list_candidates(self, search_paths: list[Path]) -> list[PluginCandidate]:
        """Cheap discovery only — doesn't activate anything."""
        return discover(search_paths)


registry = PluginRegistry()


__all__ = ["PluginRegistry", "registry"]
