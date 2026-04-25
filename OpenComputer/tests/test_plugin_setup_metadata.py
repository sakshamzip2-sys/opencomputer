"""Tests for G.23 — plugin setup metadata (Tier 4 OpenClaw port).

Covers:

1. ``PluginSetup`` + ``SetupProvider`` parse through the manifest schema.
2. ``find_setup_env_vars_for_provider`` resolves env vars from candidates.
3. The bundled provider plugins (anthropic-provider, openai-provider)
   declare the right env vars — regression guard so a future edit can't
   silently delete them.
4. ``cli._check_provider_key`` reads from the manifest first and falls
   back to the hard-coded dict only when discovery yields nothing.

Reference: ``sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-97``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import (
    PluginCandidate,
    find_setup_env_vars_for_provider,
)
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import PluginManifest, PluginSetup, SetupProvider


def _candidate(
    plugin_id: str,
    setup: PluginSetup | None,
) -> PluginCandidate:
    """Build a PluginCandidate fixture with the given setup metadata."""
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


def _write_provider_plugin(
    root: Path,
    plugin_id: str,
    *,
    setup: dict | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "provider",
        "entry": "plugin",
    }
    if setup is not None:
        manifest["setup"] = setup
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
    def test_omitted_field_yields_none(self) -> None:
        schema, err = validate_manifest(
            {"id": "p", "name": "P", "version": "0.0.1", "entry": "plugin"}
        )
        assert schema is not None, err
        assert schema.setup is None

    def test_minimal_setup(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "providers": [
                        {"id": "anthropic", "env_vars": ["ANTHROPIC_API_KEY"]}
                    ]
                },
            }
        )
        assert schema is not None
        assert schema.setup is not None
        assert len(schema.setup.providers) == 1
        prov = schema.setup.providers[0]
        assert prov.id == "anthropic"
        assert prov.env_vars == ["ANTHROPIC_API_KEY"]
        assert prov.auth_methods == []

    def test_drops_empty_strings(self) -> None:
        # Same OpenClaw tolerance pattern: empty / whitespace-only
        # entries silently filtered.
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "providers": [
                        {
                            "id": "anthropic",
                            "auth_methods": ["api_key", "", "bearer"],
                            "env_vars": ["FOO", "  "],
                        }
                    ]
                },
            }
        )
        assert schema is not None
        assert schema.setup is not None
        prov = schema.setup.providers[0]
        assert prov.auth_methods == ["api_key", "bearer"]
        assert prov.env_vars == ["FOO"]

    def test_unknown_field_in_provider_rejected(self) -> None:
        # ``extra="forbid"`` means a typo'd field surfaces loudly.
        schema, err = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {
                    "providers": [
                        {"id": "anthropic", "envVars": ["X"]}  # camelCase
                    ]
                },
            }
        )
        assert schema is None
        assert "envVars" in err

    def test_requires_runtime_default_false(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "setup": {"providers": []},
            }
        )
        assert schema is not None
        assert schema.setup is not None
        assert schema.setup.requires_runtime is False


class TestParsedManifest:
    def test_flattens_to_pluginsetup(self, tmp_path: Path) -> None:
        plugin_root = _write_provider_plugin(
            tmp_path,
            "fake-provider",
            setup={
                "providers": [
                    {
                        "id": "fake",
                        "auth_methods": ["api_key"],
                        "env_vars": ["FAKE_KEY"],
                    }
                ],
                "requires_runtime": True,
            },
        )
        manifest = discovery._parse_manifest(plugin_root / "plugin.json")
        assert manifest is not None
        assert manifest.setup is not None
        assert isinstance(manifest.setup, PluginSetup)
        assert manifest.setup.requires_runtime is True
        assert len(manifest.setup.providers) == 1
        prov = manifest.setup.providers[0]
        assert isinstance(prov, SetupProvider)
        # Tuple-valued so the dataclass stays hashable.
        assert isinstance(prov.env_vars, tuple)
        assert prov.env_vars == ("FAKE_KEY",)
        assert prov.auth_methods == ("api_key",)


# ---------------------------------------------------------------------------
# 2. find_setup_env_vars_for_provider
# ---------------------------------------------------------------------------


class TestFindEnvVars:
    def test_returns_declared_env_vars(self) -> None:
        candidates = [
            _candidate(
                "anthropic-provider",
                PluginSetup(
                    providers=(
                        SetupProvider(
                            id="anthropic",
                            auth_methods=("api_key",),
                            env_vars=("ANTHROPIC_API_KEY",),
                        ),
                    )
                ),
            ),
        ]
        assert find_setup_env_vars_for_provider("anthropic", candidates) == (
            "ANTHROPIC_API_KEY",
        )

    def test_returns_empty_for_unknown_provider(self) -> None:
        candidates = [
            _candidate(
                "anthropic-provider",
                PluginSetup(
                    providers=(
                        SetupProvider(id="anthropic", env_vars=("ANTHROPIC_API_KEY",)),
                    )
                ),
            ),
        ]
        assert find_setup_env_vars_for_provider("nonexistent", candidates) == ()

    def test_returns_empty_when_no_setup_metadata(self) -> None:
        candidates = [_candidate("plain-provider", None)]
        assert find_setup_env_vars_for_provider("anthropic", candidates) == ()

    def test_first_matching_candidate_wins(self) -> None:
        # If two plugins both declare the same provider id (unusual but
        # legal — two anthropic provider plugins from different vendors),
        # the first iteration order wins. Caller is responsible for
        # passing a deterministically-ordered list.
        candidates = [
            _candidate(
                "alpha",
                PluginSetup(providers=(SetupProvider(id="x", env_vars=("FROM_ALPHA",)),)),
            ),
            _candidate(
                "beta",
                PluginSetup(providers=(SetupProvider(id="x", env_vars=("FROM_BETA",)),)),
            ),
        ]
        assert find_setup_env_vars_for_provider("x", candidates) == ("FROM_ALPHA",)


# ---------------------------------------------------------------------------
# 3. Bundled provider regression guard
# ---------------------------------------------------------------------------


class TestBundledProviderManifests:
    def test_anthropic_declares_api_key_env(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "anthropic-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        providers = data["setup"]["providers"]
        assert len(providers) == 1
        assert providers[0]["id"] == "anthropic"
        assert "ANTHROPIC_API_KEY" in providers[0]["env_vars"]
        assert "api_key" in providers[0]["auth_methods"]
        assert "bearer" in providers[0]["auth_methods"]

    def test_openai_declares_api_key_env(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "openai-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        providers = data["setup"]["providers"]
        assert len(providers) == 1
        assert providers[0]["id"] == "openai"
        assert providers[0]["env_vars"] == ["OPENAI_API_KEY"]


# ---------------------------------------------------------------------------
# 4. cli._check_provider_key reads manifest first, falls back to dict
# ---------------------------------------------------------------------------


class TestCliProviderKeyResolution:
    def test_reads_from_manifest_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock discovery to return a candidate with a custom env var
        # name (not in the hard-coded fallback). _check_provider_key
        # must follow the manifest, not fall through.
        from opencomputer import cli

        fake_candidates = [
            _candidate(
                "custom-provider",
                PluginSetup(
                    providers=(
                        SetupProvider(id="custom", env_vars=("CUSTOM_API_KEY",)),
                    )
                ),
            )
        ]
        monkeypatch.setenv("CUSTOM_API_KEY", "ok")

        with patch(
            "opencomputer.plugins.discovery.discover", return_value=fake_candidates
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            # Should NOT raise — env var is set.
            cli._check_provider_key("custom")

    def test_fallback_to_legacy_dict_when_manifest_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No candidates declare "anthropic" — the legacy fallback dict
        # kicks in and looks for ANTHROPIC_API_KEY. This is the
        # backwards-compat path so old installs without the new G.23
        # manifest fields still work.
        from opencomputer import cli

        monkeypatch.setenv("ANTHROPIC_API_KEY", "ok")

        with patch(
            "opencomputer.plugins.discovery.discover", return_value=[]
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            cli._check_provider_key("anthropic")  # no raise

    def test_unknown_provider_silent_when_no_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A provider neither in the manifest NOR in the fallback dict
        # = nothing to check, no error.
        from opencomputer import cli

        with patch(
            "opencomputer.plugins.discovery.discover", return_value=[]
        ), patch(
            "opencomputer.plugins.discovery.standard_search_paths", return_value=[]
        ):
            cli._check_provider_key("never-heard-of-this")  # no raise
