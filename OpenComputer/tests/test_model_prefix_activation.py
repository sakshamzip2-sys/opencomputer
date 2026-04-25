"""Tests for G.21 — model-prefix auto-activation (Tier 4 OpenClaw port).

Covers:

1. ``ModelSupport`` dataclass + manifest schema parse the field correctly.
2. ``find_plugin_ids_for_model`` resolves prefixes (and patterns).
3. The bundled provider plugins (anthropic-provider, openai-provider)
   declare the right prefixes — regression guard so a future edit can't
   silently delete them.
4. ``PluginRegistry.load_all`` expands ``enabled_ids`` with matching
   plugins (Layer C).

Reference: ``sources/openclaw-2026.4.23/src/plugins/providers.ts:316-337``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import (
    PluginCandidate,
    discover,
    find_plugin_ids_for_model,
)
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import ModelSupport, PluginManifest

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _write_provider_manifest(
    root: Path,
    plugin_id: str,
    *,
    model_prefixes: list[str] | None = None,
    model_patterns: list[str] | None = None,
) -> Path:
    """Drop a minimal provider plugin.json with optional model_support."""
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "provider",
        "entry": "plugin",
    }
    if model_prefixes is not None or model_patterns is not None:
        ms: dict = {}
        if model_prefixes is not None:
            ms["model_prefixes"] = model_prefixes
        if model_patterns is not None:
            ms["model_patterns"] = model_patterns
        manifest["model_support"] = ms
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("", encoding="utf-8")
    return plugin_dir


def _candidate(plugin_id: str, model_support: ModelSupport | None) -> PluginCandidate:
    """Build a PluginCandidate fixture without filesystem I/O."""
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        version="0.0.1",
        kind="provider",
        entry="plugin",
        model_support=model_support,
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
# 1. Manifest schema + ModelSupport dataclass
# ---------------------------------------------------------------------------


class TestManifestParse:
    def test_omitted_field_yields_none(self) -> None:
        schema, err = validate_manifest(
            {"id": "p", "name": "P", "version": "0.0.1", "entry": "plugin"}
        )
        assert schema is not None, err
        assert schema.model_support is None

    def test_prefixes_only(self) -> None:
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "model_support": {"model_prefixes": ["claude-"]},
            }
        )
        assert schema is not None
        assert schema.model_support is not None
        assert schema.model_support.model_prefixes == ["claude-"]
        assert schema.model_support.model_patterns == []

    def test_drops_empty_strings(self) -> None:
        # OpenClaw tolerance: empty strings would match every model id and
        # break resolution — silently filtered. Mirrors
        # ``manifest.json5-tolerance.test.ts`` "normalizes modelSupport".
        schema, _ = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "model_support": {"model_prefixes": ["gpt-", "", "  "]},
            }
        )
        assert schema is not None
        assert schema.model_support is not None
        assert schema.model_support.model_prefixes == ["gpt-"]

    def test_unknown_extra_field_rejected(self) -> None:
        # extra="forbid" on ModelSupportSchema means typos surface loudly.
        schema, err = validate_manifest(
            {
                "id": "p",
                "name": "P",
                "version": "0.0.1",
                "entry": "plugin",
                "model_support": {"modelPrefixes": ["claude-"]},  # camelCase typo
            }
        )
        assert schema is None
        assert "modelPrefixes" in err


class TestParsedManifest:
    def test_parsed_into_pluginmanifest(self, tmp_path: Path) -> None:
        plugin_root = _write_provider_manifest(
            tmp_path, "fake-provider", model_prefixes=["fake-"]
        )
        manifest_path = plugin_root / "plugin.json"
        manifest = discovery._parse_manifest(manifest_path)
        assert manifest is not None
        assert manifest.model_support is not None
        assert manifest.model_support.model_prefixes == ("fake-",)
        # Tuple-valued so the dataclass stays hashable.
        assert isinstance(manifest.model_support.model_prefixes, tuple)


# ---------------------------------------------------------------------------
# 2. find_plugin_ids_for_model resolver
# ---------------------------------------------------------------------------


class TestFindPluginIds:
    def test_prefix_match(self) -> None:
        candidates = [
            _candidate("anthropic-provider", ModelSupport(model_prefixes=("claude-",))),
            _candidate(
                "openai-provider",
                ModelSupport(model_prefixes=("gpt-", "o1", "o3", "o4")),
            ),
        ]
        assert find_plugin_ids_for_model("claude-3-5-sonnet", candidates) == [
            "anthropic-provider"
        ]
        assert find_plugin_ids_for_model("gpt-4o", candidates) == ["openai-provider"]
        assert find_plugin_ids_for_model("o3-mini", candidates) == ["openai-provider"]

    def test_pattern_match_takes_precedence(self) -> None:
        # patterns checked before prefixes
        candidates = [
            _candidate(
                "regex-provider",
                ModelSupport(model_patterns=(r"^.*-tuned-.*$",)),
            )
        ]
        assert find_plugin_ids_for_model("foo-tuned-bar", candidates) == [
            "regex-provider"
        ]

    def test_invalid_pattern_skipped(self) -> None:
        # Bad regex must NOT raise — one malformed manifest can't break
        # the rest of the registry.
        candidates = [
            _candidate(
                "broken-provider",
                ModelSupport(model_patterns=(r"[unclosed",)),
            ),
            _candidate(
                "good-provider",
                ModelSupport(model_prefixes=("good-",)),
            ),
        ]
        assert find_plugin_ids_for_model("good-model", candidates) == ["good-provider"]

    def test_no_model_support_means_no_match(self) -> None:
        candidates = [_candidate("plain-provider", None)]
        assert find_plugin_ids_for_model("anything", candidates) == []

    def test_empty_model_id_returns_empty(self) -> None:
        candidates = [
            _candidate("anthropic-provider", ModelSupport(model_prefixes=("claude-",)))
        ]
        assert find_plugin_ids_for_model("", candidates) == []

    def test_results_are_sorted_for_determinism(self) -> None:
        # If two plugins both claim a prefix, ordering matters for the
        # prompt-cache rule. Sorted alphabetically keeps it deterministic.
        candidates = [
            _candidate("zeta-provider", ModelSupport(model_prefixes=("foo",))),
            _candidate("alpha-provider", ModelSupport(model_prefixes=("foo",))),
        ]
        assert find_plugin_ids_for_model("foobar", candidates) == [
            "alpha-provider",
            "zeta-provider",
        ]


# ---------------------------------------------------------------------------
# 3. Regression guard on bundled provider manifests
# ---------------------------------------------------------------------------


class TestBundledProviderManifests:
    def test_anthropic_declares_claude_prefix(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "anthropic-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        assert data["model_support"]["model_prefixes"] == ["claude-"]

    def test_openai_declares_gpt_and_reasoning_prefixes(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "openai-provider"
            / "plugin.json"
        )
        data = json.loads(path.read_text())
        prefixes = data["model_support"]["model_prefixes"]
        assert "gpt-" in prefixes
        assert "o1" in prefixes
        assert "o3" in prefixes
        assert "o4" in prefixes


# ---------------------------------------------------------------------------
# 4. PluginRegistry.load_all expands enabled_ids (Layer C)
# ---------------------------------------------------------------------------


class TestLoadAllAutoActivates:
    def test_expands_enabled_ids_when_model_matches(self, tmp_path: Path) -> None:
        # Write three plugins; user only enables ``other``. Active model
        # matches ``alpha`` via prefix, so ``alpha`` should auto-load.
        _write_provider_manifest(
            tmp_path, "alpha", model_prefixes=["alpha-"]
        )
        _write_provider_manifest(tmp_path, "beta", model_prefixes=["beta-"])
        _write_provider_manifest(tmp_path, "other")
        candidates = discover([tmp_path])
        assert {c.manifest.id for c in candidates} == {"alpha", "beta", "other"}

        # Test the resolver standalone (full load_all wiring tested in
        # the registry layer's existing tests; this verifies the
        # candidate set computed correctly).
        assert find_plugin_ids_for_model("alpha-7b", candidates) == ["alpha"]
        assert find_plugin_ids_for_model("nothing", candidates) == []

    def test_load_all_includes_model_match(self, tmp_path: Path) -> None:
        # End-to-end: PluginRegistry.load_all expands enabled_ids by
        # Layer C — a plugin whose model_support matches the active
        # model id is auto-activated even when not in the user's
        # enabled set. Uses stub plugins (not the real bundled
        # extensions/) to keep the test hermetic — loading real provider
        # plugins would pollute the global ToolRegistry / HookEngine
        # singletons and cascade into unrelated tests.
        from dataclasses import replace

        from opencomputer.agent.config import Config, ModelConfig
        from opencomputer.plugins.registry import PluginRegistry

        plugin_root = tmp_path / "plugins"
        plugin_root.mkdir()

        # Plugin "stub-anthropic" claims claude- prefix.
        stub_anthropic = plugin_root / "stub-anthropic"
        stub_anthropic.mkdir()
        (stub_anthropic / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "stub-anthropic",
                    "name": "Stub Anthropic",
                    "version": "0.0.1",
                    "kind": "provider",
                    "entry": "plugin",
                    "model_support": {"model_prefixes": ["claude-"]},
                }
            )
        )
        (stub_anthropic / "plugin.py").write_text("def register(api):\n    pass\n")

        # Plugin "stub-other" — user explicitly enabled this one.
        stub_other = plugin_root / "stub-other"
        stub_other.mkdir()
        (stub_other / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "stub-other",
                    "name": "Stub Other",
                    "version": "0.0.1",
                    "kind": "tool",
                    "entry": "plugin",
                }
            )
        )
        (stub_other / "plugin.py").write_text("def register(api):\n    pass\n")

        # Both Config and ModelConfig are frozen — use replace().
        cfg = replace(
            Config(),
            model=ModelConfig(
                provider="anthropic", model="claude-3-5-sonnet-latest"
            ),
        )

        registry = PluginRegistry()
        with patch(
            "opencomputer.agent.config.default_config", return_value=cfg
        ):
            registry.load_all(
                [plugin_root],
                enabled_ids=frozenset({"stub-other"}),
            )

        loaded_ids = {lp.candidate.manifest.id for lp in registry.loaded}
        assert "stub-anthropic" in loaded_ids, (
            f"expected stub-anthropic auto-activated by claude- prefix, "
            f"got {loaded_ids}"
        )
        # stub-other was explicitly enabled — still loads.
        assert "stub-other" in loaded_ids
