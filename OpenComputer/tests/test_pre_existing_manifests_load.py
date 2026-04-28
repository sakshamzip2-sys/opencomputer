"""Verify that the 4 ``plugin.json`` files that previously failed
``PluginManifestSchema(extra="forbid")`` validation now load cleanly.

The manifests used to declare a ``capabilities: [...]`` field that the
schema rejected. We removed the dead config (the field had no
consumer); these tests catch any regression that re-adds a field
which the schema doesn't know about.
"""
from __future__ import annotations

import json
from pathlib import Path

from opencomputer.plugins.manifest_validator import PluginManifestSchema

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_manifest(plugin_id: str) -> dict:
    raw = (_REPO_ROOT / "extensions" / plugin_id / "plugin.json").read_text()
    return json.loads(raw)


def test_ambient_sensors_manifest_valid() -> None:
    PluginManifestSchema(**_load_manifest("ambient-sensors"))


def test_browser_control_manifest_valid() -> None:
    PluginManifestSchema(**_load_manifest("browser-control"))


def test_skill_evolution_manifest_valid() -> None:
    PluginManifestSchema(**_load_manifest("skill-evolution"))


def test_voice_mode_manifest_valid() -> None:
    PluginManifestSchema(**_load_manifest("voice-mode"))


def test_all_four_plugins_now_discovered() -> None:
    """End-to-end: run discovery and confirm the 4 ids show up."""
    from opencomputer.plugins.discovery import discover

    candidates = discover([_REPO_ROOT / "extensions"], force_rescan=True)
    ids = {c.manifest.id for c in candidates}
    expected = {
        "ambient-sensors",
        "browser-control",
        "skill-evolution",
        "voice-mode",
    }
    missing = expected - ids
    assert not missing, f"plugins not discovered: {missing}"
