"""Phase 10f.K/L/M/N — memory-honcho plugin tests.

10f.K (this file — skeleton tests only):
  - Manifest is valid and has the required fields.
  - IMAGE_VERSION file exists and is a non-empty single-line tag.
  - Stub plugin.py register() imports + runs without error.

Later sub-phases (10f.L/M/N) will append tests to this file for the
provider implementation, docker bootstrap, and first-run wizard flow.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"


class TestHonchoSkeleton:
    def test_plugin_json_exists_and_parses(self):
        manifest_path = _EXT_DIR / "plugin.json"
        assert manifest_path.exists(), f"missing: {manifest_path}"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Required fields
        for key in ("id", "name", "version", "kind", "entry"):
            assert key in data, f"plugin.json missing required field: {key!r}"
        # Sanity on values
        assert data["id"] == "memory-honcho"
        assert data["kind"] == "provider"
        assert data["entry"] == "plugin"

    def test_plugin_json_has_profiles_wildcard(self):
        """Honcho overlay should be available in every profile by default."""
        data = json.loads((_EXT_DIR / "plugin.json").read_text(encoding="utf-8"))
        # The manifest schema added in 14.C will make this field required; for
        # now it's additive — but the plugin's own manifest should declare it
        # explicitly so it passes the future validator unchanged.
        profiles = data.get("profiles")
        assert profiles == ["*"], (
            f"memory-honcho plugin.json profiles should be ['*'] "
            f"(any profile can opt in); got {profiles!r}"
        )

    def test_image_version_file_exists_and_is_a_tag(self):
        tag_path = _EXT_DIR / "IMAGE_VERSION"
        assert tag_path.exists(), f"missing: {tag_path}"
        content = tag_path.read_text(encoding="utf-8").strip()
        assert content, "IMAGE_VERSION file must not be empty"
        # One line, no internal whitespace
        assert "\n" not in content, "IMAGE_VERSION must be a single line"
        assert " " not in content, "IMAGE_VERSION must not contain spaces"

    def test_readme_exists_and_mentions_agpl(self):
        readme = _EXT_DIR / "README.md"
        assert readme.exists(), f"missing: {readme}"
        content = readme.read_text(encoding="utf-8")
        assert "AGPL" in content, "README must acknowledge Honcho's AGPL license"
        assert "Docker" in content, "README must mention Docker prerequisite"

    def test_plugin_entry_module_stub_registers_without_error(self):
        """Phase 10f.K register() is a stub and should be a no-op on any api."""
        entry = _EXT_DIR / "plugin.py"
        assert entry.exists()
        spec = importlib.util.spec_from_file_location("_opencomputer_test_honcho_plugin", entry)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "register")

        # Pass in a minimal duck-typed api — stub should ignore it cleanly
        class _FakeAPI:
            def __getattr__(self, name):
                # Fail loudly if the stub accidentally calls anything
                raise AssertionError(f"Phase 10f.K stub must NOT call api.{name} yet")

        mod.register(_FakeAPI())  # must not raise
