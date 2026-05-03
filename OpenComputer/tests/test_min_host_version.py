"""Manifest min_host_version field validation + parse + load enforcement.

Sub-project G (openclaw-parity) Tasks 1, 2, 10. Field declared in
plugin_sdk.core.PluginManifest, validated at scan, enforced at load.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.manifest_validator import validate_manifest


def _base_manifest(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "test-plug",
        "name": "Test",
        "version": "0.1.0",
        "entry": "plugin",
        "kind": "tool",
    }
    base.update(overrides)
    return base


class TestMinHostVersionValidation:
    def test_field_optional_default_empty(self) -> None:
        schema, err = validate_manifest(_base_manifest())
        assert err == ""
        assert schema is not None
        assert schema.min_host_version == ""

    def test_explicit_semver_value_accepted(self) -> None:
        schema, err = validate_manifest(_base_manifest(min_host_version="1.2.3"))
        assert err == ""
        assert schema is not None
        assert schema.min_host_version == "1.2.3"

    def test_pre_release_accepted(self) -> None:
        schema, err = validate_manifest(_base_manifest(min_host_version="1.2.3-beta"))
        assert err == ""
        assert schema is not None

    def test_calver_value_accepted(self) -> None:
        # OpenComputer itself uses calver (2026.4.27); make sure that parses.
        schema, err = validate_manifest(_base_manifest(min_host_version="2026.4.27"))
        assert err == ""
        assert schema is not None

    def test_malformed_version_rejected(self) -> None:
        schema, err = validate_manifest(_base_manifest(min_host_version="not-a-version"))
        assert schema is None
        assert "min_host_version" in err


class TestActivationField:
    def test_default_is_none(self) -> None:
        schema, err = validate_manifest(_base_manifest())
        assert err == ""
        assert schema is not None
        assert schema.activation is None

    def test_explicit_activation_block_parses(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(
                activation={
                    "on_providers": ["anthropic"],
                    "on_channels": ["telegram"],
                    "on_commands": ["/foo"],
                    "on_tools": ["X"],
                    "on_models": ["claude-"],
                }
            )
        )
        assert err == ""
        assert schema is not None
        assert schema.activation is not None
        assert schema.activation.on_providers == ["anthropic"]
        assert schema.activation.on_channels == ["telegram"]
        assert schema.activation.on_commands == ["/foo"]
        assert schema.activation.on_tools == ["X"]
        assert schema.activation.on_models == ["claude-"]

    def test_partial_activation_other_fields_default_empty(self) -> None:
        schema, err = validate_manifest(_base_manifest(activation={"on_providers": ["openai"]}))
        assert err == ""
        assert schema is not None
        assert schema.activation is not None
        assert schema.activation.on_providers == ["openai"]
        assert schema.activation.on_channels == []

    def test_unknown_activation_key_rejected(self) -> None:
        schema, err = validate_manifest(_base_manifest(activation={"on_unknown": ["x"]}))
        assert schema is None
        assert "activation" in err


class TestMinHostVersionEnforcement:
    """At load time, mismatch raises with both versions in the message."""

    def test_load_passes_when_no_pin(self) -> None:
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(plugin_id="x", min_host_version="", host_version="2026.4.27")

    def test_load_passes_when_host_higher(self) -> None:
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(
            plugin_id="x", min_host_version="2026.1.1", host_version="2026.4.27"
        )

    def test_load_passes_when_host_equal(self) -> None:
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(
            plugin_id="x", min_host_version="2026.4.27", host_version="2026.4.27"
        )

    def test_load_raises_when_host_lower(self) -> None:
        from opencomputer.plugins.loader import (
            PluginIncompatibleError,
            _check_min_host_version,
        )

        with pytest.raises(PluginIncompatibleError) as ei:
            _check_min_host_version(
                plugin_id="x", min_host_version="2026.5.0", host_version="2026.4.27"
            )
        msg = str(ei.value)
        assert "x" in msg
        assert "2026.5.0" in msg
        assert "2026.4.27" in msg
