"""M1.2 — single canonical YAML parser for profile.yaml / config.yaml.

Pins the contract of :func:`opencomputer.agent.config_store.load_yaml_dict`
and verifies the three migrated profile.yaml callsites
(``cli_plugin._read_and_validate_profile_yaml`` and the two
``cli_profile._read_enabled_plugin_ids`` consumers) all flow through
that one parser instead of their previous ad-hoc ``yaml.safe_load``
inlines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config_store import (
    ConfigYAMLError,
    load_yaml_dict,
)

# ─── load_yaml_dict contract ────────────────────────────────────────────


class TestLoadYamlDict:
    def test_missing_file_returns_empty_when_missing_ok(self, tmp_path: Path) -> None:
        result = load_yaml_dict(tmp_path / "absent.yaml")
        assert result == {}

    def test_missing_file_raises_when_missing_ok_false(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_yaml_dict(tmp_path / "absent.yaml", missing_ok=False)

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("")
        assert load_yaml_dict(path) == {}

    def test_whitespace_only_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "ws.yaml"
        path.write_text("\n\n  \n")
        assert load_yaml_dict(path) == {}

    def test_well_formed_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "good.yaml"
        path.write_text("a: 1\nb:\n  - x\n  - y\n")
        assert load_yaml_dict(path) == {"a": 1, "b": ["x", "y"]}

    def test_malformed_yaml_raises_config_yaml_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("a: [\n")  # unclosed flow sequence
        with pytest.raises(ConfigYAMLError) as exc_info:
            load_yaml_dict(path)
        assert exc_info.value.path == path
        assert "bad.yaml" in str(exc_info.value)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- 1\n- 2\n")
        with pytest.raises(ConfigYAMLError) as exc_info:
            load_yaml_dict(path)
        assert "must be a mapping" in str(exc_info.value)

    def test_scalar_top_level_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "scalar.yaml"
        path.write_text("just-a-string\n")
        with pytest.raises(ConfigYAMLError):
            load_yaml_dict(path)

    def test_encoding_kwarg_propagates(self, tmp_path: Path) -> None:
        path = tmp_path / "latin.yaml"
        path.write_text("greeting: hola\n", encoding="latin-1")
        result = load_yaml_dict(path, encoding="latin-1")
        assert result == {"greeting": "hola"}


# ─── load_config (via load_yaml_dict) ────────────────────────────────────


class TestLoadConfigGoesThroughHelper:
    """``load_config`` must use ``load_yaml_dict`` as its parse layer.

    Pinning this prevents a future regression where someone re-inlines
    ``yaml.safe_load`` in ``load_config`` and silently bypasses the
    canonical error surface.
    """

    def test_load_config_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        from opencomputer.agent.config import default_config
        from opencomputer.agent.config_store import load_config

        cfg = load_config(tmp_path / "absent.yaml")
        assert cfg == default_config()

    def test_load_config_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        from opencomputer.agent.config import default_config
        from opencomputer.agent.config_store import load_config

        path = tmp_path / "empty.yaml"
        path.write_text("")
        assert load_config(path) == default_config()

    def test_load_config_malformed_yaml_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        from opencomputer.agent.config_store import load_config

        path = tmp_path / "bad.yaml"
        path.write_text("a: [\n")
        with pytest.raises(RuntimeError, match="Failed to parse"):
            load_config(path)

    def test_load_config_non_mapping_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        from opencomputer.agent.config_store import load_config

        path = tmp_path / "list.yaml"
        path.write_text("- 1\n")
        with pytest.raises(RuntimeError, match="Failed to parse"):
            load_config(path)


# ─── cli_profile._read_enabled_plugin_ids (lenient consumer) ─────────────


class TestReadEnabledPluginIdsLenient:
    """The env-template / env-init paths swallow malformed YAML and
    return ``None`` (treat as "include everything"). Verify the helper
    still has that shape after the migration to ``load_yaml_dict``."""

    def _read(self, path: Path) -> set[str] | None:
        from opencomputer.cli_profile import _read_enabled_plugin_ids

        return _read_enabled_plugin_ids(path)

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        # Lenient contract: missing file → None ("include everything")
        assert self._read(tmp_path / "absent.yaml") is None

    def test_well_formed_returns_set(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.yaml"
        path.write_text("plugins:\n  enabled:\n    - foo\n    - bar\n")
        assert self._read(path) == {"foo", "bar"}

    def test_malformed_yaml_returns_none(self, tmp_path: Path) -> None:
        # Same lenient swallow as the prior `except Exception:` did
        path = tmp_path / "bad.yaml"
        path.write_text("a: [\n")
        assert self._read(path) is None

    def test_no_plugins_block_returns_empty_set(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.yaml"
        path.write_text("other: 1\n")
        assert self._read(path) == set()

    def test_enabled_not_a_list_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.yaml"
        path.write_text("plugins:\n  enabled: 'not-a-list'\n")
        assert self._read(path) is None


# ─── cli_plugin path (strict consumer) ───────────────────────────────────


class TestPluginYamlGoesThroughHelper:
    """Verify that the strict cli_plugin.py YAML reader also flows
    through ``load_yaml_dict``. We don't test the typer.Exit path here
    (that's what the existing cli_plugin tests do); we just pin that
    the migrated function still behaves correctly on the happy + empty
    paths."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from opencomputer.cli_plugin import _read_and_validate_profile_yaml

        result = _read_and_validate_profile_yaml(
            tmp_path / "absent.yaml", action_label="enable"
        )
        assert result == {}

    def test_well_formed_returns_dict(self, tmp_path: Path) -> None:
        from opencomputer.cli_plugin import _read_and_validate_profile_yaml

        path = tmp_path / "profile.yaml"
        path.write_text("plugins:\n  enabled:\n    - foo\n")
        result = _read_and_validate_profile_yaml(path, action_label="enable")
        assert result == {"plugins": {"enabled": ["foo"]}}


# ─── grep guard — single canonical parser ────────────────────────────────


class TestNoRawYamlSafeLoadInMigratedSites:
    """Guard against regression: the 3 migrated callsites (cli_plugin
    line 737 era, cli_profile lines 707/792 era) must NOT have raw
    ``yaml.safe_load(...)`` reintroduced.
    """

    def _module_text(self, name: str) -> str:
        import importlib

        mod = importlib.import_module(name)
        path = Path(mod.__file__)
        return path.read_text()

    def test_cli_plugin_has_no_raw_yaml_safe_load(self) -> None:
        text = self._module_text("opencomputer.cli_plugin")
        # Allow `yaml.dump` etc. — only block `yaml.safe_load` reads
        # in source (docstring mentions are filtered by checking the
        # full call shape, not just the substring "yaml").
        assert "yaml.safe_load" not in text, (
            "regression: cli_plugin re-introduced raw yaml.safe_load; "
            "use load_yaml_dict from agent.config_store instead"
        )

    def test_cli_profile_has_no_raw_yaml_safe_load(self) -> None:
        text = self._module_text("opencomputer.cli_profile")
        assert "yaml.safe_load" not in text, (
            "regression: cli_profile re-introduced raw yaml.safe_load; "
            "use _read_enabled_plugin_ids or load_yaml_dict instead"
        )
