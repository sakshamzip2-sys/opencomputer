"""
Plugin registry — the active set of loaded plugins + the surfaces they registered.

Holds the provider registry and channel registry (tools go into the
tool registry from tools/registry.py; hooks go into the hook engine).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opencomputer.agent.injection import engine as injection_engine
from opencomputer.hooks.engine import engine as hook_engine
from opencomputer.plugins.discovery import (
    PluginCandidate,
    build_legacy_id_lookup,
    discover,
    find_plugin_ids_for_model,
)
from opencomputer.plugins.loader import (
    LoadedPlugin,
    PluginAPI,
    load_plugin,
    teardown_loaded_plugin,
)
from opencomputer.tools.registry import registry as tool_registry
from plugin_sdk.core import SingleInstanceError
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
    # Phase 12b.6 Task D8: plugin-authored slash commands. Shared across
    # all plugins; threaded into PluginAPI via ``api()``.
    slash_commands: dict[str, Any] = field(default_factory=dict)
    # Task I.9: the most-recent ``PluginAPI`` handed out by ``load_all``.
    # Gateway ``Dispatch`` reads this to wrap each request in
    # ``api.in_request(ctx)`` so plugins can query their per-request
    # scope. ``None`` before any ``load_all`` call — the gateway must
    # have loaded plugins before dispatching.
    shared_api: PluginAPI | None = None
    # Hermes channel-port (PR 2 / amendment §A.3): outgoing-queue facade
    # threaded into every freshly-built ``PluginAPI`` so webhook-style
    # plugins can enqueue messages without importing
    # ``opencomputer.gateway.outgoing_queue``. Populated by the gateway
    # (``Gateway._start_outgoing_drainer``) right before plugin
    # registration; ``None`` outside the gateway.
    outgoing_queue: Any = None
    # Pass-2 F2 (Phase 2 Pre-Task 2.4): map of plugin_id → set of tool
    # names that plugin registered. Populated during ``load_all`` from
    # the snapshot-diff already computed for teardown
    # (``LoadedPlugin.registrations.tool_names``). Read via
    # :meth:`tools_provided_by` — used by the upcoming production
    # AgentLoop factory in Phase 2 Task 2.4 to compute per-profile
    # tool exposures from bindings.yaml.
    _tools_by_plugin: dict[str, set[str]] = field(default_factory=dict)

    def api(self) -> PluginAPI:
        # Pass-2 F8 fix: do NOT capture the default-config session DB
        # path at api() time. ``PluginAPI.session_db_path`` is now a
        # lazy ``@property`` that reads ``_home() / "sessions.db"`` on
        # each access — so under multi-profile dispatch, plugins that
        # read ``api.session_db_path`` from inside ``set_profile(b)``
        # see profile b's path, not the boot-time default.
        #
        # Eager capture here was the F8 bug: ``shared_api`` lives
        # forever and was being passed to non-default-profile loops,
        # so ``api.session_db_path`` returned the wrong path.
        return PluginAPI(
            tool_registry=tool_registry,
            hook_engine=hook_engine,
            provider_registry=self.providers,
            channel_registry=self.channels,
            injection_engine=injection_engine,
            doctor_contributions=self.doctor_contributions,
            slash_commands=self.slash_commands,
            outgoing_queue=self.outgoing_queue,
        )

    def load_all(
        self,
        search_paths: list[Path],
        enabled_ids: frozenset[str] | Literal["*"] | None = None,
    ) -> list[LoadedPlugin]:
        """Discover + activate plugins. Returns the successfully loaded ones.

        Filtering stack (Phase 14.C/14.D/14.M, G.21, G.22):

        1. **Layer A — manifest profile scope** (14.C/14.D): if
           ``manifest.profiles`` is set and doesn't contain the active
           profile or ``"*"``, the plugin is skipped with a clear log
           line. This is the plugin AUTHOR's declared compatibility.
        2. **Layer B — user's enabled_ids filter** (14.M): if
           ``enabled_ids`` is a frozenset, only listed ids pass. ``None``
           or ``"*"`` means "no filter" (backward-compatible default).
        3. **Layer B′ — legacy id normalization** (G.22): before B
           runs, each entry in ``enabled_ids`` is mapped through the
           ``legacy_plugin_ids`` aliases declared on current manifests.
           A user's ``profile.yaml`` written before a plugin rename
           still hits the renamed plugin. Mirrors OpenClaw's
           ``normalizePluginId`` (``sources/openclaw-2026.4.23/src/
           plugins/config-state.ts:83-91``).
        4. **Layer C — model-prefix auto-activation** (G.21): when
           ``enabled_ids`` is a frozenset, plugins whose
           ``manifest.model_support`` matches the active ``cfg.model.model``
           are silently added to the set even if the user's preset didn't
           list them. Mirrors OpenClaw's ``applyPluginAutoEnable``
           (``sources/openclaw-2026.4.23/src/config/plugin-auto-enable.
           model-support.test.ts``). Solves the friction of "I switched
           to gpt-4o, why is openai-provider disabled?" — the user named
           the model, so the matching plugin must come along.
        """
        from opencomputer.profiles import read_active_profile

        active_profile = read_active_profile() or "default"
        candidates = discover(search_paths)
        api = self.api()
        # Task I.9: expose the shared api so the gateway dispatch can
        # wrap each request in ``api.in_request(ctx)`` — plugins then
        # see their per-request scope via ``api.request_context``.
        self.shared_api = api
        wildcard = enabled_ids is None or enabled_ids == "*"

        # Layer B′ — legacy id normalization (G.22). Runs before Layer C
        # so a renamed provider plugin's current id is what model-prefix
        # matching adds to (avoids double-adding the legacy + current ids).
        if not wildcard:
            assert isinstance(enabled_ids, frozenset)
            legacy_lookup = build_legacy_id_lookup(candidates)
            if legacy_lookup:
                normalized = frozenset(
                    legacy_lookup.get(pid, pid) for pid in enabled_ids
                )
                if normalized != enabled_ids:
                    rewrites = sorted(
                        (legacy, legacy_lookup[legacy])
                        for legacy in enabled_ids
                        if legacy in legacy_lookup
                    )
                    logger.info(
                        "legacy plugin id normalization: %s",
                        ", ".join(f"{old!r}→{new!r}" for old, new in rewrites),
                    )
                    enabled_ids = normalized

        # Layer C — model-prefix auto-activation (G.21). Only expands
        # the set when a filter IS active (wildcard already loads
        # everything). Wrapped defensively: a malformed config.yaml
        # mustn't prevent plugin loading.
        if not wildcard:
            assert isinstance(enabled_ids, frozenset)
            try:
                from opencomputer.agent.config import default_config

                cfg = default_config()
                model_id = cfg.model.model
            except Exception:  # noqa: BLE001
                logger.debug(
                    "model-prefix auto-activation skipped: cannot resolve active model",
                    exc_info=True,
                )
                model_id = ""
            if model_id:
                matches = find_plugin_ids_for_model(model_id, candidates)
                additions = [pid for pid in matches if pid not in enabled_ids]
                if additions:
                    enabled_ids = enabled_ids | frozenset(additions)
                    logger.info(
                        "model-prefix auto-activation: model %r → enabling %s",
                        model_id,
                        additions,
                    )

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
            try:
                loaded = load_plugin(cand, api)
            except SingleInstanceError as e:
                # Task B6: one plugin losing the single-instance race
                # must NOT prevent the rest of the registry from coming
                # up. Log a WARNING so ops can see it; continue.
                logger.warning(
                    "skipping plugin '%s': single-instance lock unavailable (%s)",
                    cand.manifest.id,
                    e,
                )
                continue
            if loaded:
                self.loaded.append(loaded)
                # Pass-2 F2: track tool names per plugin id so
                # ``tools_provided_by`` can answer in O(1).
                # ``registrations.tool_names`` was computed by the
                # loader's snapshot-diff (the same data teardown
                # already relies on), so this stays consistent with
                # what the plugin actually registered.
                if loaded.registrations.tool_names:
                    self._tools_by_plugin.setdefault(
                        cand.manifest.id, set()
                    ).update(loaded.registrations.tool_names)
        return self.loaded

    def list_candidates(self, search_paths: list[Path]) -> list[PluginCandidate]:
        """Cheap discovery only — doesn't activate anything."""
        return discover(search_paths)

    def tools_provided_by(self, plugin_id: str) -> frozenset[str]:
        """Return the tool names registered by a given plugin.

        ``plugin_id`` is the manifest's ``id`` (kebab-case dir name).
        Unknown plugin_id returns the empty frozenset (not an error).

        Pass-2 F2 (Phase 2 Pre-Task 2.4): consumed by the production
        AgentLoop factory in Phase 2 Task 2.4 to compute per-profile
        tool exposures from bindings.yaml. The data is sourced from
        the same snapshot-diff that drives plugin teardown, so
        ``tools_provided_by`` and the actual tool-registry stay
        consistent across load + teardown cycles.
        """
        return frozenset(self._tools_by_plugin.get(plugin_id, ()))

    def teardown_plugin(self, plugin_id: str) -> bool:
        """Tear down a single loaded plugin (Task I.4).

        Looks up the ``LoadedPlugin`` by id, then:

        1. Calls its optional ``cleanup()`` / ``teardown()`` entry-point
           function (plugins MAY define one for resource cleanup).
        2. Removes the plugin's registrations from the shared API
           (tools, providers, channels, slash commands, injection
           providers, hooks, doctor contributions, memory provider).
           Uses the delta captured at load time so only the entries
           this plugin added are removed — sibling plugins' entries
           are preserved.
        3. Drops the plugin's synthetic module + common sibling names
           from ``sys.modules`` so a later reload sees fresh state.
        4. Removes the ``LoadedPlugin`` from ``self.loaded``.

        Returns ``True`` if the plugin was found and torn down,
        ``False`` if no plugin with that id was loaded (no-op).

        Teardown is OPT-IN — it's never called automatically during
        normal plugin lifecycle. Callers use it for live-reload
        scenarios and test isolation.

        Mirrors OpenClaw's ``clearPluginLoaderCache`` pattern
        (``sources/openclaw/src/plugins/loader.ts:222-230``).
        """
        target: LoadedPlugin | None = None
        for lp in self.loaded:
            if lp.candidate.manifest.id == plugin_id:
                target = lp
                break
        if target is None:
            return False

        # teardown_loaded_plugin is never-raise; failures are logged.
        teardown_loaded_plugin(target)

        # Drop from loaded list AFTER teardown so a parallel iterator
        # that holds a reference still sees it finish cleanly. Use
        # identity-match so a manifest-id collision doesn't drop an
        # unrelated entry.
        self.loaded = [lp for lp in self.loaded if lp is not target]
        # Pass-2 F2: keep ``_tools_by_plugin`` consistent with the
        # tool registry — torn-down plugins must not appear in
        # ``tools_provided_by`` results.
        self._tools_by_plugin.pop(plugin_id, None)
        logger.info("torn down plugin '%s'", plugin_id)
        return True


registry = PluginRegistry()


__all__ = ["PluginRegistry", "registry"]
