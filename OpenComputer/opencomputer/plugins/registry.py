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


def _manifest_allows_profile(manifest: object, profile_name: str) -> tuple[bool, str]:
    """Layer A check: does this manifest's ``profiles`` field permit this profile?

    Rules (Phase 14.C/14.D):
      - ``manifest.profiles`` is None → permissive, load in any profile.
      - ``manifest.profiles`` contains ``"*"`` → permissive.
      - ``manifest.profiles`` contains ``profile_name`` → explicit allow.
      - Otherwise → skip, with the allowed list in the reason string so
        the user can see why it was excluded.
    """
    profiles = getattr(manifest, "profiles", None)
    if profiles is None:
        return True, ""
    if "*" in profiles or profile_name in profiles:
        return True, ""
    return False, f"manifest restricts to {list(profiles)!r}"


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

        Filtering stack (Phase 14.C/14.D/14.M):

        1. **Layer A — manifest profile scope** (14.C/14.D): if
           ``manifest.profiles`` is set and doesn't contain the active
           profile or ``"*"``, the plugin is skipped with a clear log
           line. This is the plugin AUTHOR's declared compatibility.
        2. **Layer B — user's enabled_ids filter** (14.M): if
           ``enabled_ids`` is a frozenset, only listed ids pass. ``None``
           or ``"*"`` means "no filter" (backward-compatible default).
        """
        from opencomputer.profiles import read_active_profile

        active_profile = read_active_profile() or "default"
        candidates = discover(search_paths)
        api = self.api()
        wildcard = enabled_ids is None or enabled_ids == "*"
        for cand in candidates:
            # Layer A — manifest scope check
            allowed, reason = _manifest_allows_profile(cand.manifest, active_profile)
            if not allowed:
                logger.info(
                    "skipping plugin '%s' in profile '%s': %s",
                    cand.manifest.id,
                    active_profile,
                    reason,
                )
                continue
            # Layer B — user's enabled_ids filter (14.M)
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
