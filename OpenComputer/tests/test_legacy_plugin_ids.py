"""Tests for G.22 — legacy plugin id normalization (Tier 4 OpenClaw port).

Covers:

1. ``legacy_plugin_ids`` parses through manifest schema + dataclass.
2. ``build_legacy_id_lookup`` builds the legacy → current map.
3. ``normalize_plugin_id`` maps unknown ids unchanged, known ids to current.
4. Conflict cases: self-alias dropped, alias-of-current-id rejected,
   duplicate-claim warns with last-write-wins.
5. End-to-end: ``PluginRegistry.load_all`` accepts old ids in
   ``enabled_ids`` and still loads the renamed plugin.

Reference: ``sources/openclaw-2026.4.23/src/plugins/config-state.ts:69-91``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import (
    PluginCandidate,
    build_legacy_id_lookup,
    normalize_plugin_id,
)
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import PluginManifest

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _candidate(plugin_id: str, legacy_ids: tuple[str, ...] = ()) -> PluginCandidate:
    """Build a PluginCandidate fixture with the given legacy ids."""
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        version="0.0.1",
        kind="tool",
        entry="plugin",
        legacy_plugin_ids=legacy_ids,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=Path("/tmp/fake") / plugin_id,
        manifest_path=Path("/tmp/fake") / plugin_id / "plugin.json",
    )


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    legacy_plugin_ids: list[str] | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "tool",
        "entry": "plugin",
    }
    if legacy_plugin_ids is not None:
        manifest["legacy_plugin_ids"] = legacy_plugin_ids
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    return plugin_dir


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


# ---------------------------------------------------------------------------
# 1. Schema + dataclass parse
# ---------------------------------------------------------------------------


class TestManifestParse:
    def test_omitted_field_yields_empty_tuple(self) -> None:
        schema, err = validate_manifest(
            {"id": "p", "name": "P", "version": "0.0.1", "entry": "plugin"}
        )
        assert schema is not None, err
        assert schema.legacy_plugin_ids == []

    def test_list_of_strings_accepted(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "legacy_plugin_ids": ["old-id", "older-id"],
            }
        )
        assert schema is not None
        assert schema.legacy_plugin_ids == ["old-id", "older-id"]

    def test_drops_empty_strings(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "legacy_plugin_ids": ["old", "", "  "],
            }
        )
        assert schema is not None
        assert schema.legacy_plugin_ids == ["old"]

    def test_parsed_into_pluginmanifest(self, tmp_path: Path) -> None:
        plugin_root = _write_plugin(
            tmp_path, "current", legacy_plugin_ids=["old-name", "older-name"]
        )
        manifest = discovery._parse_manifest(plugin_root / "plugin.json")
        assert manifest is not None
        assert manifest.legacy_plugin_ids == ("old-name", "older-name")
        # Tuple-valued so the dataclass stays hashable.
        assert isinstance(manifest.legacy_plugin_ids, tuple)


# ---------------------------------------------------------------------------
# 2. build_legacy_id_lookup
# ---------------------------------------------------------------------------


class TestBuildLookup:
    def test_simple_rename(self) -> None:
        candidates = [_candidate("new-name", legacy_ids=("old-name",))]
        assert build_legacy_id_lookup(candidates) == {"old-name": "new-name"}

    def test_multiple_legacy_ids(self) -> None:
        candidates = [
            _candidate("new", legacy_ids=("v1", "v2", "v3"))
        ]
        assert build_legacy_id_lookup(candidates) == {
            "v1": "new",
            "v2": "new",
            "v3": "new",
        }

    def test_no_legacy_ids_yields_empty(self) -> None:
        candidates = [_candidate("plain")]
        assert build_legacy_id_lookup(candidates) == {}

    def test_self_alias_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        # A plugin that lists its own id in legacy_plugin_ids is a typo.
        # Drop silently — no warning needed because it's a no-op.
        candidates = [_candidate("alpha", legacy_ids=("alpha",))]
        assert build_legacy_id_lookup(candidates) == {}

    def test_alias_collides_with_other_current_id_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # If beta lists "alpha" as legacy but alpha exists as a current
        # id, that's confusing — skip the alias and warn.
        candidates = [
            _candidate("alpha"),
            _candidate("beta", legacy_ids=("alpha",)),
        ]
        with caplog.at_level(logging.WARNING, logger="opencomputer.plugins.discovery"):
            lookup = build_legacy_id_lookup(candidates)
        assert "alpha" not in lookup
        assert any("legacy id" in rec.message for rec in caplog.records)

    def test_duplicate_claim_last_write_wins_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Two plugins both claim "shared-old-name" as their legacy id —
        # ambiguous rename. Last-write wins (whichever discover()
        # returns last) and a warning surfaces in logs.
        candidates = [
            _candidate("alpha", legacy_ids=("shared-old-name",)),
            _candidate("beta", legacy_ids=("shared-old-name",)),
        ]
        with caplog.at_level(logging.WARNING, logger="opencomputer.plugins.discovery"):
            lookup = build_legacy_id_lookup(candidates)
        # Last-write — beta is iterated second.
        assert lookup["shared-old-name"] == "beta"
        assert any(
            "claimed by multiple current plugins" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# 3. normalize_plugin_id
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_unknown_id_returned_unchanged(self) -> None:
        candidates = [_candidate("alpha")]
        assert normalize_plugin_id("unknown", candidates) == "unknown"

    def test_legacy_id_mapped_to_current(self) -> None:
        candidates = [_candidate("new-name", legacy_ids=("old-name",))]
        assert normalize_plugin_id("old-name", candidates) == "new-name"

    def test_current_id_returned_unchanged(self) -> None:
        # Current ids should pass through without rewrite.
        candidates = [_candidate("new-name", legacy_ids=("old-name",))]
        assert normalize_plugin_id("new-name", candidates) == "new-name"


# ---------------------------------------------------------------------------
# 4. End-to-end load_all picks up renamed plugin via legacy id
# ---------------------------------------------------------------------------


class TestLoadAllNormalization:
    def test_old_id_in_enabled_set_loads_renamed_plugin(self, tmp_path: Path) -> None:
        # User config still references the OLD id; the renamed plugin's
        # manifest declares the rename. The loader must transparently
        # normalize and load the new plugin.
        from opencomputer.plugins.registry import PluginRegistry

        plugin_root = tmp_path / "plugins"
        plugin_root.mkdir()
        # New plugin, declares "ye-olde-name" as a legacy alias.
        _write_plugin(
            plugin_root, "renamed-plugin", legacy_plugin_ids=["ye-olde-name"]
        )

        registry = PluginRegistry()
        registry.load_all(
            [plugin_root],
            enabled_ids=frozenset({"ye-olde-name"}),
        )
        loaded_ids = {lp.candidate.manifest.id for lp in registry.loaded}
        assert "renamed-plugin" in loaded_ids, (
            f"expected legacy id 'ye-olde-name' to normalize to 'renamed-plugin', "
            f"got loaded {loaded_ids}"
        )

    def test_unknown_id_in_enabled_set_still_filters(self, tmp_path: Path) -> None:
        # An id that isn't a current plugin AND isn't anyone's legacy
        # alias should still filter (load nothing).
        from opencomputer.plugins.registry import PluginRegistry

        plugin_root = tmp_path / "plugins"
        plugin_root.mkdir()
        _write_plugin(plugin_root, "alpha")
        _write_plugin(plugin_root, "beta")

        registry = PluginRegistry()
        registry.load_all(
            [plugin_root],
            enabled_ids=frozenset({"never-existed"}),
        )
        assert registry.loaded == [], (
            "non-matching id should not load anything"
        )

    def test_mixed_legacy_and_current_ids(self, tmp_path: Path) -> None:
        # User has both an old id and a current id — both should load.
        from opencomputer.plugins.registry import PluginRegistry

        plugin_root = tmp_path / "plugins"
        plugin_root.mkdir()
        _write_plugin(
            plugin_root, "renamed", legacy_plugin_ids=["old-id"]
        )
        _write_plugin(plugin_root, "always-current")

        registry = PluginRegistry()
        registry.load_all(
            [plugin_root],
            enabled_ids=frozenset({"old-id", "always-current"}),
        )
        loaded_ids = {lp.candidate.manifest.id for lp in registry.loaded}
        assert loaded_ids == {"renamed", "always-current"}
