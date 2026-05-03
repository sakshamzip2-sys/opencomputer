"""Plugin shape classifier - compare manifest claims to actual registrations.

Sub-project G (openclaw-parity) Task 7. Mirrors openclaw
``inspect-shape.ts`` shape - reads what a plugin declares in
``plugin.json`` and what it actually registers via ``register(api)``,
then reports drift.

First-cut keeps two classifications: ``valid`` (declarations match
actuals) and ``drift`` (any divergence). Openclaw's full 4-shape model
(plain-capability / hybrid-capability / hook-only / non-capability)
defers to a follow-up.

Pure data - no side effects, no logging, no plugin loading at the
``inspect_shape_from_candidate`` boundary. The convenience
``inspect_shape(plugin_id)`` does load (idempotent) so it can compare
against the live registry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from opencomputer.plugins.discovery import (
    PluginCandidate,
    discover,
    standard_search_paths,
)

__all__ = [
    "Classification",
    "PluginShape",
    "inspect_shape",
    "inspect_shape_from_candidate",
]


Classification = Literal["valid", "drift"]


@dataclass(frozen=True, slots=True)
class PluginShape:
    """Result of inspecting one plugin's shape."""

    plugin_id: str
    declared_tools: tuple[str, ...] = ()
    actual_tools: tuple[str, ...] = ()
    declared_channels: tuple[str, ...] = ()
    actual_channels: tuple[str, ...] = ()
    declared_providers: tuple[str, ...] = ()
    actual_providers: tuple[str, ...] = ()
    declared_hooks: tuple[str, ...] = ()
    actual_hooks: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    classification: Classification = "valid"


def inspect_shape_from_candidate(
    candidate: PluginCandidate,
    *,
    registered_tools: tuple[str, ...],
    registered_channels: tuple[str, ...],
    registered_providers: tuple[str, ...],
    registered_hooks: tuple[str, ...],
) -> PluginShape:
    """Build a PluginShape from a candidate + a snapshot of what it
    actually registered. Pure - no side effects.

    Used by ``inspect_shape`` itself (with live registry data) and in
    tests (with synthetic registry tuples).
    """
    declared_tools = candidate.manifest.tool_names
    declared_channels: tuple[str, ...] = ()
    declared_providers: tuple[str, ...] = ()
    if candidate.manifest.setup is not None:
        declared_providers = tuple(p.id for p in candidate.manifest.setup.providers)
        declared_channels = tuple(c.id for c in candidate.manifest.setup.channels)
    declared_hooks: tuple[str, ...] = ()  # No declared-hook field in manifest yet.

    drift: list[str] = []

    # Tools: declared (required + optional) vs actual.
    declared_required = set(declared_tools)
    declared_optional = set(candidate.manifest.optional_tool_names)
    actual_tools_set = set(registered_tools)
    # Required missing - drift.
    for missing in sorted(declared_required - actual_tools_set):
        drift.append(f"tool {missing!r} declared but not registered")
    # Registered but neither declared required nor optional - drift.
    for extra in sorted(actual_tools_set - declared_required - declared_optional):
        drift.append(f"tool {extra!r} registered but not declared")

    declared_channels_set = set(declared_channels)
    actual_channels_set = set(registered_channels)
    for missing in sorted(declared_channels_set - actual_channels_set):
        drift.append(f"channel {missing!r} declared but not registered")
    for extra in sorted(actual_channels_set - declared_channels_set):
        drift.append(f"channel {extra!r} registered but not declared")

    declared_providers_set = set(declared_providers)
    actual_providers_set = set(registered_providers)
    for missing in sorted(declared_providers_set - actual_providers_set):
        drift.append(f"provider {missing!r} declared but not registered")
    for extra in sorted(actual_providers_set - declared_providers_set):
        drift.append(f"provider {extra!r} registered but not declared")

    classification: Classification = "drift" if drift else "valid"

    return PluginShape(
        plugin_id=candidate.manifest.id,
        declared_tools=tuple(declared_tools),
        actual_tools=tuple(sorted(actual_tools_set)),
        declared_channels=tuple(declared_channels),
        actual_channels=tuple(sorted(actual_channels_set)),
        declared_providers=tuple(declared_providers),
        actual_providers=tuple(sorted(actual_providers_set)),
        declared_hooks=tuple(declared_hooks),
        actual_hooks=tuple(sorted(registered_hooks)),
        drift=tuple(drift),
        classification=classification,
    )


def inspect_shape(plugin_id: str) -> PluginShape:
    """Inspect a plugin by id. Returns a PluginShape; never raises.

    Behavior:
    - Plugin id not found in discovery -> drift shape with "plugin not loaded".
    - Plugin id found + loaded -> real comparison.
    - Plugin id found + load failure -> drift shape with the load error.

    Reads from the live PluginRegistry (``LoadedPlugin.registrations``) to
    figure out what was actually registered.
    """
    candidates = discover(standard_search_paths())
    matched = next((c for c in candidates if c.manifest.id == plugin_id), None)
    if matched is None:
        return PluginShape(
            plugin_id=plugin_id,
            classification="drift",
            drift=(
                f"plugin {plugin_id!r} not loaded - no candidate found in search paths",
            ),
        )

    actual_tools: tuple[str, ...] = ()
    actual_channels: tuple[str, ...] = ()
    actual_providers: tuple[str, ...] = ()
    actual_hooks: tuple[str, ...] = ()
    load_error: str | None = None
    try:
        from opencomputer.plugins.loader import load_plugin
        from opencomputer.plugins.registry import registry as plugin_registry

        # PluginRegistry.loaded is list[LoadedPlugin]; iterate to find ours.
        loaded = None
        for lp in plugin_registry.loaded:
            if lp.candidate.manifest.id == plugin_id:
                loaded = lp
                break
        if loaded is None:
            api = plugin_registry.api()
            load_plugin(matched, api, plugin_registry)
            for lp in plugin_registry.loaded:
                if lp.candidate.manifest.id == plugin_id:
                    loaded = lp
                    break
        if loaded is not None:
            regs = loaded.registrations
            actual_tools = tuple(sorted(regs.tool_names))
            actual_channels = tuple(sorted(regs.channel_names))
            actual_providers = tuple(sorted(regs.provider_names))
            # hook_specs is identity-keyed (no names); count placeholder.
            actual_hooks = tuple(f"hook[{i}]" for i in range(len(regs.hook_specs)))
    except Exception as e:  # noqa: BLE001
        load_error = f"load failed: {type(e).__name__}: {e}"

    shape = inspect_shape_from_candidate(
        matched,
        registered_tools=actual_tools,
        registered_channels=actual_channels,
        registered_providers=actual_providers,
        registered_hooks=actual_hooks,
    )
    if load_error is not None:
        new_drift = (load_error, *shape.drift)
        shape = replace(shape, drift=new_drift, classification="drift")
    return shape
