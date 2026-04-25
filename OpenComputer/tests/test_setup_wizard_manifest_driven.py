"""Tests for G.24 — setup wizard reads display metadata from plugin manifests.

Covers:

1. ``SetupProvider`` parses the new display fields (``label``,
   ``default_model``, ``signup_url``).
2. The bundled provider manifests declare populated display fields —
   regression guard so a future edit can't silently drop them.
3. ``_discover_supported_providers`` merges manifest data over the
   legacy fallback dict.
4. The wizard's hard-coded fallback still kicks in when discovery
   yields nothing (backwards-compat).

Reference: ``sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-83``
+ existing G.23 metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import PluginManifest, PluginSetup, SetupProvider


def _candidate(plugin_id: str, setup: PluginSetup | None) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        version="0.0.1",
        kind="provider",
        entry="plugin",
        setup=setup,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=Path("/tmp/fake") / plugin_id,
        manifest_path=Path("/tmp/fake") / plugin_id / "plugin.json",
    )


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


# ---------------------------------------------------------------------------
# 1. Schema parses the new display fields
# ---------------------------------------------------------------------------


class TestDisplayFieldsParse:
    def test_label_default_model_signup_url_accepted(self) -> None:
        schema, err = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "providers": [
                        {
                            "id": "anthropic",
                            "env_vars": ["ANTHROPIC_API_KEY"],
                            "label": "Anthropic (Claude)",
                            "default_model": "claude-opus-4-7",
                            "signup_url": "https://console.anthropic.com/settings/keys",
                        }
                    ]
                },
            }
        )
        assert schema is not None, err
        assert schema.setup is not None
        prov = schema.setup.providers[0]
        assert prov.label == "Anthropic (Claude)"
        assert prov.default_model == "claude-opus-4-7"
        assert prov.signup_url.startswith("https://")

    def test_omitted_display_fields_default_to_empty(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {"providers": [{"id": "x"}]},
            }
        )
        assert schema is not None
        prov = schema.setup.providers[0]
        assert prov.label == ""
        assert prov.default_model == ""
        assert prov.signup_url == ""


# ---------------------------------------------------------------------------
# 2. Bundled provider manifest regression guards
# ---------------------------------------------------------------------------


class TestBundledManifestDisplayFields:
    def test_anthropic_provider_has_display_metadata(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "anthropic-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        prov = data["setup"]["providers"][0]
        assert prov["label"] == "Anthropic (Claude)"
        assert prov["default_model"]  # any non-empty default
        assert prov["signup_url"].startswith("https://console.anthropic.com")

    def test_openai_provider_has_display_metadata(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "openai-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        prov = data["setup"]["providers"][0]
        assert prov["label"] == "OpenAI (GPT)"
        assert prov["default_model"]
        assert prov["signup_url"].startswith("https://platform.openai.com")


# ---------------------------------------------------------------------------
# 3. _discover_supported_providers merges manifest over fallback
# ---------------------------------------------------------------------------


class TestDiscoverSupportedProviders:
    def test_manifest_overrides_fallback(self) -> None:
        # A plugin that overrides the default model for "anthropic"
        # via its manifest should win against the hard-coded dict.
        from opencomputer.setup_wizard import _discover_supported_providers

        fake_candidates = [
            _candidate(
                "custom-anthropic",
                PluginSetup(
                    providers=(
                        SetupProvider(
                            id="anthropic",
                            env_vars=("ANTHROPIC_API_KEY",),
                            label="Custom Claude",
                            default_model="claude-3-5-sonnet-latest",
                            signup_url="https://example.com/keys",
                        ),
                    )
                ),
            )
        ]
        with patch(
            "opencomputer.plugins.discovery.discover", return_value=fake_candidates
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            catalog = _discover_supported_providers()
        assert catalog["anthropic"]["label"] == "Custom Claude"
        assert catalog["anthropic"]["default_model"] == "claude-3-5-sonnet-latest"
        assert catalog["anthropic"]["signup_url"] == "https://example.com/keys"
        assert catalog["anthropic"]["env_key"] == "ANTHROPIC_API_KEY"

    def test_empty_manifest_field_does_not_overwrite_fallback(self) -> None:
        # If a manifest declares `label=""` (empty), the fallback's
        # label should be preserved — empty string is "no value", not
        # "explicit override to empty."
        from opencomputer.setup_wizard import _discover_supported_providers

        fake_candidates = [
            _candidate(
                "partial",
                PluginSetup(
                    providers=(
                        SetupProvider(
                            id="anthropic",
                            env_vars=("ANTHROPIC_API_KEY",),
                            # label, default_model, signup_url all ""
                        ),
                    )
                ),
            )
        ]
        with patch(
            "opencomputer.plugins.discovery.discover", return_value=fake_candidates
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            catalog = _discover_supported_providers()
        # Fallback's label "Anthropic (Claude)" should still be there.
        assert catalog["anthropic"]["label"] == "Anthropic (Claude)"

    def test_third_party_provider_added(self) -> None:
        # A third-party plugin that declares an entirely new provider
        # id should appear in the catalog without core changes.
        from opencomputer.setup_wizard import _discover_supported_providers

        fake_candidates = [
            _candidate(
                "groq-provider",
                PluginSetup(
                    providers=(
                        SetupProvider(
                            id="groq",
                            env_vars=("GROQ_API_KEY",),
                            label="Groq (LPU)",
                            default_model="mixtral-8x7b",
                            signup_url="https://console.groq.com/keys",
                        ),
                    )
                ),
            )
        ]
        with patch(
            "opencomputer.plugins.discovery.discover", return_value=fake_candidates
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            catalog = _discover_supported_providers()
        assert "groq" in catalog
        assert catalog["groq"]["label"] == "Groq (LPU)"

    def test_discovery_failure_falls_back_silently(self) -> None:
        # A filesystem error during discovery must NOT break the wizard;
        # the legacy hard-coded fallback dict is still returned.
        from opencomputer.setup_wizard import _discover_supported_providers

        with patch(
            "opencomputer.plugins.discovery.discover",
            side_effect=OSError("permission denied"),
        ):
            catalog = _discover_supported_providers()
        # Built-in providers still present.
        assert "anthropic" in catalog
        assert "openai" in catalog


# ---------------------------------------------------------------------------
# 4. Helper symbol exists + has the right shape
# ---------------------------------------------------------------------------


class TestExportedSymbols:
    def test_get_supported_providers_returns_dict(self) -> None:
        from opencomputer.setup_wizard import _get_supported_providers

        catalog = _get_supported_providers()
        assert isinstance(catalog, dict)
        assert "anthropic" in catalog
        for _pid, meta in catalog.items():
            assert isinstance(meta, dict)
            assert "label" in meta
            assert "env_key" in meta
            assert "default_model" in meta
            assert "signup_url" in meta
