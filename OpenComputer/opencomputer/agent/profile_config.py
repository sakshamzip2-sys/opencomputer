"""Profile-level config (Phase 14.M integration).

A profile's ``config.yaml`` may declare either:

- ``preset: <name>`` — reference a named preset in
  ``~/.opencomputer/presets/<name>.yaml``, OR
- ``plugins.enabled: [a, b, c]`` — inline list (zesty 14.D's shape).

Setting both is an error. Neither set falls back to the safe default:
``plugins.enabled = "*"`` (load everything — matches pre-Phase-14
behaviour).

This module is the *resolution* step — it turns the declarative profile
config + workspace overlay into a concrete "these ids may load" set that
the plugin loader can filter against.

NOTE: Full zesty 14.A–E (``--profile`` flag routing, profile dir layout,
manifest ``profiles`` field) is NOT required for this resolver to
function. The resolver reads from whichever directory the caller
provides; in practice that's ``_home()`` (the active profile's root,
which ``OPENCOMPUTER_HOME`` already controls).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from opencomputer.agent.workspace import WorkspaceOverlay
from opencomputer.plugins.preset import load_preset

# ``"*"`` sentinel means "all plugins allowed"; matches zesty 14.D's
# ``plugins.enabled: "*"`` shape. A concrete list means "only these ids".
EnabledPlugins = frozenset[str] | Literal["*"]


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """What a profile's ``config.yaml`` parses into.

    Kept small. Full zesty 14.D also puts manifest-profiles/
    single-instance hooks on the profile level; this MVP ships only
    what 14.M/14.N need.
    """

    preset: str | None = None
    enabled_plugins: EnabledPlugins = "*"


@dataclass(frozen=True, slots=True)
class ResolvedPluginFilter:
    """Result of ``resolve_enabled_plugins``. Carries the resolved set
    plus a human-readable trail for logging + doctor."""

    enabled: EnabledPlugins
    source: str = ""  # e.g. "preset 'coding' + overlay additional [x,y]"


class ProfileConfigError(ValueError):
    """Raised for malformed profile.yaml — e.g. both preset and
    plugins.enabled set. Callers (doctor, loader) decide whether to
    surface or fall back."""


def profile_config_path(profile_dir: Path) -> Path:
    """Where profile.yaml lives for a given profile dir."""
    return profile_dir / "profile.yaml"


def validate_profile_config_dict(
    raw: dict, *, path: Path | str = "profile.yaml"
) -> ProfileConfig:
    """Validate a parsed profile.yaml dict against the schema.

    Pure function — does NOT touch the filesystem. Use this when you've
    already parsed the YAML (e.g. inside a flock-protected
    read-modify-write window) and want the same validation semantics
    that :func:`load_profile_config` enforces.

    The ``path`` argument is used purely for human-readable error
    messages and accepts a ``Path`` or a ``str`` placeholder.

    What's validated:
      * top-level shape is a mapping
      * ``plugins`` (if present) is a mapping
      * ``plugins.enabled`` (if present) is ``"*"`` or a list of strings
      * ``preset`` and ``plugins.enabled`` are mutually exclusive

    What's NOT rejected:
      * Unknown top-level keys (e.g. user-added ``description``,
        ``notes``, ``owner``). These are preserved on round-trip
        write by both the agent loop's reader (which discards them
        but doesn't error) and the CLI mutators (which round-trip
        the raw dict). Rejecting these would break user workflows
        that document profiles inline — ``test_enable_preserves_
        other_profile_yaml_keys`` pins this behavior.
    """
    if not isinstance(raw, dict):
        raise ProfileConfigError(f"{path} must contain a mapping at the top level")

    preset = raw.get("preset")
    plugins_block = raw.get("plugins")

    enabled: EnabledPlugins = "*"
    if plugins_block is not None:
        if not isinstance(plugins_block, dict):
            raise ProfileConfigError(f"{path}: `plugins` must be a mapping")
        block_enabled = plugins_block.get("enabled", "*")
        if block_enabled == "*":
            enabled = "*"
        elif isinstance(block_enabled, list):
            if not all(isinstance(x, str) for x in block_enabled):
                raise ProfileConfigError(
                    f"{path}: `plugins.enabled` must be a list of strings"
                )
            enabled = frozenset(block_enabled)
        else:
            raise ProfileConfigError(
                f"{path}: `plugins.enabled` must be a list or '*' "
                f"(got {type(block_enabled).__name__})"
            )

    explicit_enabled = plugins_block is not None and "enabled" in plugins_block
    if preset is not None and explicit_enabled:
        raise ProfileConfigError(
            f"{path}: both `preset` and `plugins.enabled` are set — "
            f"pick one (preset references a shared list; "
            f"plugins.enabled is an inline list)"
        )

    return ProfileConfig(preset=preset, enabled_plugins=enabled)


def load_profile_config(profile_dir: Path) -> ProfileConfig:
    """Read ``<profile_dir>/profile.yaml`` into a ProfileConfig.

    Missing file returns defaults. Delegates to
    :func:`validate_profile_config_dict` for the actual schema check —
    the same code path the strict CLI mutators (plugin enable/disable)
    take, so all readers see exactly the same errors.
    """
    path = profile_config_path(profile_dir)
    if not path.exists():
        return ProfileConfig()

    raw = yaml.safe_load(path.read_text()) or {}
    return validate_profile_config_dict(raw, path=path)


def resolve_enabled_plugins(
    profile_cfg: ProfileConfig,
    overlay: WorkspaceOverlay | None = None,
    *,
    presets_root: Path | None = None,
) -> ResolvedPluginFilter:
    """Expand profile config + workspace overlay into a plugin-id filter.

    Resolution order (per plan §Decision N3):

    1. Profile's preset (if set) OR inline enabled list (if set) OR
       "*" (default).
    2. Overlay's ``preset:`` *replaces* the base list (if set).
    3. Overlay's ``plugins.additional:`` is unioned on top.
    4. The result is either a frozenset of ids or the ``"*"`` sentinel.

    Raises ``FileNotFoundError`` if any referenced preset doesn't
    exist. Callers (loader, doctor) decide how to report.
    """
    trail: list[str] = []

    overlay_overrides_base = overlay is not None and overlay.preset is not None

    # Step 1 — base. Skip loading the profile's preset if the overlay is
    # about to replace it wholesale; this also means a broken profile
    # preset doesn't block a workspace that has explicitly overridden it.
    base: EnabledPlugins
    if overlay_overrides_base:
        assert overlay is not None and overlay.preset is not None
        preset = load_preset(overlay.preset, root=presets_root)
        base = frozenset(preset.plugins)
        trail.append(f"overlay preset '{overlay.preset}' [{len(preset.plugins)}] (overrode base)")
    elif profile_cfg.preset is not None:
        preset = load_preset(profile_cfg.preset, root=presets_root)
        base = frozenset(preset.plugins)
        trail.append(f"preset '{profile_cfg.preset}' [{len(preset.plugins)}]")
    else:
        base = profile_cfg.enabled_plugins
        trail.append(
            "profile.plugins.enabled=*" if base == "*" else f"profile.plugins.enabled [{len(base)}]"
        )

    # Step 3 — overlay additional union.
    additional: list[str] = []
    if overlay is not None and overlay.plugins.additional:
        additional = list(overlay.plugins.additional)

    enabled: EnabledPlugins
    if base == "*":
        # "*" absorbs everything; additional has no effect on a wildcard.
        enabled = "*"
        if additional:
            trail.append(f"overlay additional {additional} ignored (base is '*')")
    else:
        # Concrete set — union with additional.
        assert isinstance(base, frozenset)
        if additional:
            enabled = base | frozenset(additional)
            trail.append(f"+ overlay additional {additional}")
        else:
            enabled = base

    return ResolvedPluginFilter(enabled=enabled, source=" -> ".join(trail))


__all__ = [
    "ProfileConfig",
    "ProfileConfigError",
    "ResolvedPluginFilter",
    "EnabledPlugins",
    "load_profile_config",
    "validate_profile_config_dict",
    "resolve_enabled_plugins",
    "profile_config_path",
]
