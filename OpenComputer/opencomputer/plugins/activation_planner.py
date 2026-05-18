"""Manifest-driven activation planner.

Sub-project G (openclaw-parity) Task 6. Reads ``PluginManifest.activation``
declarations and a snapshot of current triggers (active providers,
channels, requested tools, invoked commands, active model id) and
returns the deterministic list of plugin ids that should be activated.

Falls back to ``tool_names`` when ``activation`` is ``None`` — that's
the legacy Sub-project E (PR #26) inference path. When ``activation``
is present, ``activation.on_tools`` is unioned with ``tool_names`` so
older plugins that declare only ``tool_names`` still work even after
the manifest schema gains the new block.

Mirrors openclaw ``activation-planner.ts`` shape from
``sources/openclaw-2026.4.23/src/plugins/activation-planner.ts``. Pure
function — no filesystem I/O, no plugin loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencomputer.plugins.discovery import PluginCandidate

__all__ = [
    "ActivationTriggers",
    "channel_narrowed_ids",
    "plan_activations",
]


@dataclass(frozen=True, slots=True)
class ActivationTriggers:
    """Snapshot of current state that drives activation decisions.

    All fields default to empty so callers can supply only the triggers
    they care about. Frozen + slots so two snapshots can be compared
    deterministically (e.g. caching the planner result by trigger key).
    """

    active_providers: frozenset[str] = field(default_factory=frozenset)
    active_channels: frozenset[str] = field(default_factory=frozenset)
    invoked_commands: frozenset[str] = field(default_factory=frozenset)
    requested_tools: frozenset[str] = field(default_factory=frozenset)
    active_model: str = ""


def plan_activations(
    candidates: list[PluginCandidate],
    triggers: ActivationTriggers,
) -> list[str]:
    """Return ids of plugins whose activation triggers match the snapshot.

    Result is alphabetically sorted for determinism. Plugins with no
    activation declarations AND no ``tool_names`` produce no triggers
    (they must be enabled explicitly via config or
    ``enabled_by_default``).
    """
    activated: set[str] = set()
    for cand in candidates:
        manifest = cand.manifest
        if manifest.activation is not None:
            on_providers = set(manifest.activation.on_providers)
            on_channels = set(manifest.activation.on_channels)
            on_commands = set(manifest.activation.on_commands)
            on_tools = set(manifest.activation.on_tools) | set(manifest.tool_names)
            on_models = list(manifest.activation.on_models)
        else:
            on_providers = set()
            on_channels = set()
            on_commands = set()
            on_tools = set(manifest.tool_names)
            on_models = []

        if on_providers & triggers.active_providers:
            activated.add(manifest.id)
            continue
        if on_channels & triggers.active_channels:
            activated.add(manifest.id)
            continue
        if on_commands & triggers.invoked_commands:
            activated.add(manifest.id)
            continue
        if on_tools & triggers.requested_tools:
            activated.add(manifest.id)
            continue
        if triggers.active_model:
            for prefix in on_models:
                if triggers.active_model.startswith(prefix):
                    activated.add(manifest.id)
                    break
    return sorted(activated)


def channel_narrowed_ids(candidates: list[PluginCandidate]) -> list[str]:
    """Return plugin ids minus the pure channel-adapter plugins.

    Best-of-three Recipe 3 channel-narrowing. A plugin that declares a
    non-empty ``activation.on_channels`` is a pure channel adapter — it
    bridges a messaging platform (Telegram, Discord, …) into the gateway
    daemon and is dead weight on a surface that serves no channels, i.e.
    interactive ``oc chat``.

    Every other plugin is kept: tools, providers, memory, skills, and —
    importantly — channel-*kind* plugins that do NOT declare
    ``on_channels`` because they also register chat-usable surface
    (e.g. homeassistant, which registers tools). The opt-in
    ``on_channels`` annotation, not the manifest ``kind``, is what makes
    a plugin narrowable, so a tool-providing channel plugin is never
    dropped by accident.

    Result is alphabetically sorted for determinism. This is the
    deny-list half of activation; :func:`plan_activations` is the
    allow-list half used for trigger-driven activation.
    """
    kept: list[str] = []
    for cand in candidates:
        activation = cand.manifest.activation
        if activation is not None and activation.on_channels:
            continue  # annotated channel adapter — narrowed out
        kept.append(cand.manifest.id)
    return sorted(kept)
