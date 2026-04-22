"""Phase 14.C — PluginManifest.profiles + single_instance fields.

SDK-level additions that let plugin authors declare which profiles their
plugin is compatible with and whether it's a single-instance plugin
(owns a bot token, for example) that can only be active in one profile
at a time. Defaults are backward-compatible: profiles=None means "any
profile", single_instance=False means "no lock".
"""

from __future__ import annotations

import json
from pathlib import Path


class TestPluginManifestFields:
    def test_default_profiles_is_none(self):
        from plugin_sdk.core import PluginManifest

        m = PluginManifest(id="x", name="X", version="0.1.0")
        assert m.profiles is None
        assert m.single_instance is False

    def test_profiles_can_be_wildcard_tuple(self):
        from plugin_sdk.core import PluginManifest

        m = PluginManifest(id="x", name="X", version="0.1.0", profiles=("*",))
        assert m.profiles == ("*",)

    def test_profiles_can_be_specific_names(self):
        from plugin_sdk.core import PluginManifest

        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            profiles=("coder", "default"),
        )
        assert m.profiles == ("coder", "default")

    def test_single_instance_flag(self):
        from plugin_sdk.core import PluginManifest

        m = PluginManifest(id="telegram", name="T", version="0.1.0", single_instance=True)
        assert m.single_instance is True


class TestManifestValidator:
    def test_accepts_profiles_field(self):
        from opencomputer.plugins.manifest_validator import validate_manifest

        data = {
            "id": "test-plugin",
            "name": "Test",
            "version": "0.1.0",
            "entry": "plugin",
            "profiles": ["coder", "*"],
        }
        schema, err = validate_manifest(data)
        assert schema is not None, err
        assert schema.profiles == ["coder", "*"]

    def test_accepts_single_instance_field(self):
        from opencomputer.plugins.manifest_validator import validate_manifest

        data = {
            "id": "telegram",
            "name": "Telegram",
            "version": "0.1.0",
            "entry": "plugin",
            "single_instance": True,
        }
        schema, err = validate_manifest(data)
        assert schema is not None, err
        assert schema.single_instance is True

    def test_profiles_defaults_to_none_when_absent(self):
        from opencomputer.plugins.manifest_validator import validate_manifest

        data = {
            "id": "test-plugin",
            "name": "Test",
            "version": "0.1.0",
            "entry": "plugin",
        }
        schema, err = validate_manifest(data)
        assert schema is not None, err
        assert schema.profiles is None
        assert schema.single_instance is False

    def test_accepts_schema_version(self):
        """14.M/N ships manifests with schema_version: 2; validator must accept."""
        from opencomputer.plugins.manifest_validator import validate_manifest

        data = {
            "id": "test-plugin",
            "name": "Test",
            "version": "0.1.0",
            "entry": "plugin",
            "schema_version": 2,
        }
        schema, err = validate_manifest(data)
        assert schema is not None, err


class TestDiscoveryHonorsProfiles:
    def test_discovery_populates_profiles_from_json(self, tmp_path):
        """End-to-end: a plugin.json with profiles is parsed into PluginManifest."""
        from opencomputer.plugins.discovery import _parse_manifest

        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        manifest_path = plugin_dir / "plugin.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "my-plugin",
                    "name": "My Plugin",
                    "version": "0.1.0",
                    "entry": "plugin",
                    "profiles": ["coder"],
                    "single_instance": True,
                }
            )
        )
        manifest = _parse_manifest(manifest_path)
        assert manifest is not None
        assert manifest.profiles == ("coder",)
        assert manifest.single_instance is True


class TestBundledPluginsDeclareProfiles:
    """Best-practice check: bundled plugins should explicitly declare profiles."""

    def _load_plugin_json(self, name: str) -> dict:
        repo_root = Path(__file__).resolve().parent.parent
        path = repo_root / "extensions" / name / "plugin.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_memory_honcho_declares_wildcard_profiles(self):
        # memory-honcho may not exist on this branch (it's on a separate
        # branch). Only assert when present.
        data = self._load_plugin_json("memory-honcho")
        if not data:
            return  # plugin not on this branch; fine
        assert data.get("profiles") == ["*"]
