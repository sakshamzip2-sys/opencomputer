"""Workspace overlay — ``.opencomputer/config.yaml`` in CWD (Phase 14.N).

A workspace overlay is a small YAML file that overrides a narrow subset
of the active profile's config for the duration of one invocation. It
is discovered by walking upward from CWD, first match wins (exactly the
walk `.git` uses).

Allowed fields (whitelist; ``extra="forbid"``):
    preset              str        override the profile's preset
    plugins.additional  list[str]  union with the profile's plugin list
    env                 dict       override env for this invocation

Explicitly rejected fields:
    profile             — would break ``main()``-time --profile routing
    home                — HOME is upstream of overlay resolution

The actual merge into the active plugin set is a loader-level concern
and lives in zesty Phase 14.D's loader edit; this module is only
responsible for discovery + parsing + shape validation.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

OVERLAY_DIRNAME = ".opencomputer"
OVERLAY_FILENAME = "config.yaml"


class _PluginsField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    additional: list[str] = Field(default_factory=list)


class WorkspaceOverlay(BaseModel):
    """Whitelist of fields permitted in a workspace overlay."""

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    plugins: _PluginsField = Field(default_factory=_PluginsField)
    env: dict[str, str] = Field(default_factory=dict)

    #: The absolute filesystem path the overlay was loaded from. Populated
    #: by ``find_workspace_overlay`` for operational logging. Never
    #: serialised back out — purely diagnostic.
    source_path: Path | None = Field(default=None, exclude=True)


def find_workspace_overlay(*, start: Path | None = None) -> WorkspaceOverlay | None:
    """Walk up from ``start`` (default: CWD) for ``.opencomputer/config.yaml``.

    First match wins. Returns ``None`` if no ancestor has one. Raises
    ``ValueError`` if a match is found but its contents are malformed —
    do not silently ignore a bad overlay, surface it.

    ``$HOME/.opencomputer/config.yaml`` is NEVER treated as a workspace
    overlay — that path is the user's main OpenComputer config (model /
    loop / session / memory / mcp). Workspace overlays are per-project
    files that override a narrow subset; they live inside projects, not
    in ``$HOME``. Without this guard, walking up from any subdir of
    ``$HOME`` would misparse the main config and fail on strict
    ``extra=forbid`` validation.
    """
    # Use real_user_home (HOME-mutation-immune) so the "skip the user's
    # main ~/.opencomputer/config.yaml" guard still works when
    # _apply_profile_override has set HOME to a profile-scoped path.
    # Otherwise the walk-up would happily parse the main config as a
    # workspace overlay and fail strict validation.
    from opencomputer.profiles import real_user_home
    cursor = (start if start is not None else Path.cwd()).resolve()
    home = real_user_home().resolve()
    while True:
        candidate = cursor / OVERLAY_DIRNAME / OVERLAY_FILENAME
        if cursor != home and candidate.exists():
            raw = yaml.safe_load(candidate.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError(
                    f"workspace overlay {candidate} must contain a mapping at the top level"
                )
            overlay = WorkspaceOverlay.model_validate(raw)
            overlay.source_path = candidate
            return overlay
        parent = cursor.parent
        if parent == cursor:
            return None
        cursor = parent


__all__ = [
    "WorkspaceOverlay",
    "find_workspace_overlay",
    "OVERLAY_DIRNAME",
    "OVERLAY_FILENAME",
]
