"""Tests for I.8 — manifest id-derivation provenance tracking.

`PluginCandidate` now records HOW its id was resolved (``manifest`` /
``package_name`` / ``directory``). Today every candidate gets
``"manifest"`` because that's the only derivation path OpenComputer
supports, but the field is first-class so future fallbacks (package
name, directory basename) can carry their own provenance and collision
warnings can say exactly which derivation produced each side.

Mirrors OpenClaw's id-derivation tracking
(sources/openclaw/src/plugins/discovery.ts:678-725,
``resolvePackageExtensionEntries`` + ``deriveIdHint``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import PluginCandidate, discover
from plugin_sdk.core import PluginManifest


def _write_manifest(root: Path, plugin_id: str, entry: str = "plugin") -> Path:
    """Scaffold a minimal valid plugin.json under ``root / plugin_id``."""
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "tool",
        "entry": entry,
    }
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / f"{entry}.py").write_text("", encoding="utf-8")
    return plugin_dir


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


def _make_manifest(plugin_id: str) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name=plugin_id.title(),
        version="0.0.1",
        kind="tool",
        entry="plugin",
    )


def test_plugin_candidate_defaults_to_manifest_source(tmp_path: Path) -> None:
    """A bare PluginCandidate gets id_source='manifest' — today's only path."""
    candidate = PluginCandidate(
        manifest=_make_manifest("alpha"),
        root_dir=tmp_path / "alpha",
        manifest_path=tmp_path / "alpha" / "plugin.json",
    )
    assert candidate.id_source == "manifest"


def test_plugin_candidate_accepts_directory_source(tmp_path: Path) -> None:
    """Future derivation paths pass through untouched."""
    candidate = PluginCandidate(
        manifest=_make_manifest("beta"),
        root_dir=tmp_path / "beta",
        manifest_path=tmp_path / "beta" / "plugin.json",
        id_source="directory",
    )
    assert candidate.id_source == "directory"


def test_plugin_candidate_accepts_package_name_source(tmp_path: Path) -> None:
    """The third derivation path (package_name) is also a valid literal."""
    candidate = PluginCandidate(
        manifest=_make_manifest("gamma"),
        root_dir=tmp_path / "gamma",
        manifest_path=tmp_path / "gamma" / "plugin.json",
        id_source="package_name",
    )
    assert candidate.id_source == "package_name"


def test_discover_sets_manifest_source(tmp_path: Path) -> None:
    """Candidates produced by ``discover`` carry id_source='manifest'."""
    root = tmp_path / "plugins"
    root.mkdir()
    _write_manifest(root, "alpha")
    _write_manifest(root, "beta")

    result = discover([root])

    assert len(result) == 2
    for candidate in result:
        assert candidate.id_source == "manifest"


def test_collision_warning_includes_both_id_sources(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When two candidates collide, the log says which derivation path each came from.

    Two search roots each host a plugin that resolved to the same id
    ('dup'). Today both sides are ``manifest`` (because that is the only
    supported derivation), but the log format must expose the source for
    both so a future mix (manifest-vs-directory, say) produces a useful
    message.
    """
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write_manifest(root_a, "dup")
    _write_manifest(root_b, "dup")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.discovery")
    result = discover([root_a, root_b])

    # The higher-priority root (root_a, passed first) wins; the second
    # occurrence is skipped with a warning.
    assert len(result) == 1
    assert result[0].root_dir == root_a / "dup"

    collision_records = [
        r for r in caplog.records if "plugin id collision" in r.getMessage()
    ]
    assert len(collision_records) == 1
    msg = collision_records[0].getMessage()

    # The id itself plus both sides' derivation paths must appear.
    assert "'dup'" in msg
    # Both sides are currently 'manifest'; that specific substring must
    # appear (twice, because the message names both sources).
    assert msg.count("manifest") >= 2
    # And the losing path must be named so operators can find the dup.
    assert str(root_b / "dup") in msg
