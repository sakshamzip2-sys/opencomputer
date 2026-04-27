"""Persona YAML loader + registry.

Loads from <profile_home>/personas/*.yaml first, falling back to bundled
defaults. Each persona has:
  id, name, description, system_prompt_overlay, preferred_tone,
  preferred_response_format, disabled_capabilities.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from opencomputer.agent.config import _home

_BUNDLED_DIR = Path(__file__).parent / "defaults"


def _load_yaml_files(directory: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and "id" in data:
            out.append(data)
    return out


def list_personas() -> list[dict[str, Any]]:
    """User personas override bundled defaults by id."""
    bundled = _load_yaml_files(_BUNDLED_DIR)
    user_dir = _home() / "personas"
    user = _load_yaml_files(user_dir)
    by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in bundled}
    for p in user:
        by_id[p["id"]] = p
    return list(by_id.values())


def get_persona(persona_id: str) -> dict[str, Any] | None:
    for p in list_personas():
        if p["id"] == persona_id:
            return p
    return None
