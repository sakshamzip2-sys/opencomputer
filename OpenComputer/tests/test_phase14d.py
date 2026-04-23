"""Phase 14.D — manifest-layer enforcement in the plugin loader.

Layer A of the belt+suspenders profile×plugin binding (v7 plan §3 Decision 4).
Layer B (profile-config ``plugins.enabled``) already lives via 14.M/14.N's
``_resolve_plugin_filter`` + ``enabled_ids`` param on ``load_all``. This
phase adds Layer A: the plugin MANIFEST's own ``profiles`` field is
consulted before the user's enabled filter.

The two layers compose: a plugin must be permitted by BOTH its manifest
(author's declared scope) AND the user's active profile config (user's
explicit opt-in) to load.
"""

from __future__ import annotations

from pathlib import Path


class TestManifestAllowsProfileHelper:
    """Unit tests for the pure-function helper."""

    def _manifest(self, profiles=None):
        from plugin_sdk.core import PluginManifest

        return PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            profiles=profiles,
        )

    def test_none_profiles_allows_everything(self):
        from opencomputer.plugins.registry import _manifest_allows_profile

        ok, _ = _manifest_allows_profile(self._manifest(None), "default")
        assert ok is True
        ok, _ = _manifest_allows_profile(self._manifest(None), "coder")
        assert ok is True

    def test_wildcard_allows_everything(self):
        from opencomputer.plugins.registry import _manifest_allows_profile

        m = self._manifest(("*",))
        assert _manifest_allows_profile(m, "default")[0] is True
        assert _manifest_allows_profile(m, "coder")[0] is True
        assert _manifest_allows_profile(m, "anything")[0] is True

    def test_specific_allow_list(self):
        from opencomputer.plugins.registry import _manifest_allows_profile

        m = self._manifest(("coder", "default"))
        assert _manifest_allows_profile(m, "coder")[0] is True
        assert _manifest_allows_profile(m, "default")[0] is True

        # Not in the list → rejected with reason that shows the allow list
        ok, reason = _manifest_allows_profile(m, "personal")
        assert ok is False
        assert "coder" in reason and "default" in reason

    def test_empty_list_is_valid_but_rejects_all(self):
        """An empty profiles list = 'this plugin runs nowhere'. Odd but valid."""
        from opencomputer.plugins.registry import _manifest_allows_profile

        ok, reason = _manifest_allows_profile(self._manifest(()), "default")
        assert ok is False


class TestLoaderSkipsManifestMismatches:
    """Integration: load_all() actually skips manifest-incompatible plugins."""

    def _make_stub_plugin(
        self, root: Path, name: str, *, profiles=None, single_instance=False
    ) -> Path:
        """Create a plugin.json + stub plugin.py so discovery can find it."""
        import json

        plugin_dir = root / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": name,
            "name": name.title(),
            "version": "0.1.0",
            "entry": "plugin",
            "kind": "mixed",
        }
        if profiles is not None:
            manifest["profiles"] = profiles
        if single_instance:
            manifest["single_instance"] = True
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest))
        (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n")
        return plugin_dir

    def test_load_all_loads_wildcard_plugin_in_any_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        # Active profile = default (no active_profile file present)
        self._make_stub_plugin(tmp_path / "pluginroot", "test-any", profiles=["*"])

        # Build a fresh registry (not the global one) so we don't pollute it
        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry()
        reg.load_all([tmp_path / "pluginroot"])
        assert any(p.candidate.manifest.id == "test-any" for p in reg.loaded)

    def test_load_all_skips_plugin_not_matching_active_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        # Set active profile = "personal"
        (tmp_path).mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("personal\n")

        # Plugin says it only works in "coder" or "default"
        self._make_stub_plugin(tmp_path / "pluginroot", "coder-only", profiles=["coder", "default"])
        # Control plugin: wildcard, should still load
        self._make_stub_plugin(tmp_path / "pluginroot", "any-plugin", profiles=["*"])

        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry()
        reg.load_all([tmp_path / "pluginroot"])
        loaded_ids = {p.candidate.manifest.id for p in reg.loaded}
        assert "any-plugin" in loaded_ids
        assert "coder-only" not in loaded_ids

    def test_load_all_loads_specific_plugin_when_active_matches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        (tmp_path).mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        self._make_stub_plugin(tmp_path / "pluginroot", "coder-tool", profiles=["coder"])
        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry()
        reg.load_all([tmp_path / "pluginroot"])
        loaded_ids = {p.candidate.manifest.id for p in reg.loaded}
        assert "coder-tool" in loaded_ids

    def test_layer_a_and_b_both_apply(self, tmp_path, monkeypatch):
        """A plugin passes Layer A (manifest) but is filtered by Layer B."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        # Active profile = default
        self._make_stub_plugin(tmp_path / "pluginroot", "pass-a", profiles=["*"])
        self._make_stub_plugin(tmp_path / "pluginroot", "pass-a-fail-b", profiles=["*"])

        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry()
        # User's enabled_ids filter only includes pass-a
        reg.load_all([tmp_path / "pluginroot"], enabled_ids=frozenset({"pass-a"}))
        loaded_ids = {p.candidate.manifest.id for p in reg.loaded}
        assert "pass-a" in loaded_ids
        assert "pass-a-fail-b" not in loaded_ids
