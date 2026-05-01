"""Named plugin-activation presets — Phase 14.M.

A preset is a YAML file at ``~/.opencomputer/presets/<name>.yaml`` listing
plugin ids to activate. Presets are shared across profiles — one preset
can back any number of profiles via a ``preset: <name>`` line in each
profile's ``config.yaml`` (see zesty Phase 14.D ``ProfileConfig``).

This module is intentionally standalone: no imports from
``opencomputer.agent.config`` or the plugin loader. The wiring into
``ProfileConfig`` and ``can_load`` happens in Phase 14.D/14.M's loader
edit, not here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

#: Matches ``manifest_validator._ID_RE`` so preset-referenced ids
#: follow the same format as plugin manifest ids.
_ID_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def presets_dir() -> Path:
    """Global presets directory. NOT profile-scoped — presets are shared.

    Uses :func:`opencomputer.profiles.get_default_root` (lazy import) so
    the resolved path is immune to ``$HOME`` mutation by
    ``_apply_profile_override``. Without this, calling ``presets_dir()``
    from inside an active profile would resolve to
    ``<profile>/home/.opencomputer/presets`` instead of the real shared
    location.
    """
    from opencomputer.profiles import get_default_root
    return get_default_root() / "presets"


def preset_path(name: str) -> Path:
    return presets_dir() / f"{name}.yaml"


class Preset(BaseModel):
    """A named plugin-activation set.

    Flat list. No nested ``enabled:`` key — a preset is *the* list that
    becomes a profile's effective enabled-set after resolution.
    """

    model_config = ConfigDict(extra="forbid")

    plugins: list[str]

    @field_validator("plugins")
    @classmethod
    def _validate_plugins(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            seen: set[str] = set()
            dups = [p for p in v if p in seen or seen.add(p)]  # type: ignore[func-returns-value]
            raise ValueError(f"preset has duplicate plugin ids: {dups}")
        for pid in v:
            if not _ID_RE.match(pid):
                raise ValueError(
                    f"plugin id {pid!r} is not a valid id (lowercase, digits, hyphens)"
                )
        return v


def load_preset(name: str, *, root: Path | None = None) -> Preset:
    """Read and validate ``<root>/<name>.yaml``.

    Raises ``FileNotFoundError`` if the preset file is missing —
    callers should surface this to the user, never swallow it. A
    referenced-but-missing preset is a configuration bug.
    """
    base = root if root is not None else presets_dir()
    path = base / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"preset {name!r} not found at {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"preset file {path} must contain a mapping at the top level")
    return Preset.model_validate(raw)


def list_presets(*, root: Path | None = None) -> list[str]:
    """Return sorted preset names in ``<root>`` (default: global presets dir)."""
    base = root if root is not None else presets_dir()
    if not base.exists():
        return []
    return sorted(p.stem for p in base.iterdir() if p.is_file() and p.suffix == ".yaml")


def write_preset(
    name: str,
    plugins: list[str],
    *,
    root: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Serialise a preset to disk. Returns the absolute path written.

    Raises ``FileExistsError`` if the preset already exists and
    ``overwrite`` is False.
    """
    # Validate first — bad input never reaches disk.
    model = Preset(plugins=plugins)

    base = root if root is not None else presets_dir()
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{name}.yaml"

    if dest.exists() and not overwrite:
        raise FileExistsError(f"preset {name!r} already exists at {dest}")

    # Use safe_dump for portable YAML; ``default_flow_style=None`` gives
    # readable block style with short-form for inline lists when they fit.
    payload = {"plugins": list(model.plugins)}
    dest.write_text(yaml.safe_dump(payload, sort_keys=False))
    return dest


__all__ = [
    "Preset",
    "load_preset",
    "list_presets",
    "write_preset",
    "preset_path",
    "presets_dir",
]
