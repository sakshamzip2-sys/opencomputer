"""Tests for model alias resolution + ModelConfig.model_aliases field."""
from __future__ import annotations

import pytest

from opencomputer.agent.model_resolver import resolve_model


def test_resolve_alias_returns_canonical():
    aliases = {"fast": "claude-haiku-4-5-20251001", "smart": "claude-opus-4-7"}
    assert resolve_model("fast", aliases) == "claude-haiku-4-5-20251001"
    assert resolve_model("smart", aliases) == "claude-opus-4-7"


def test_resolve_canonical_passes_through():
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    assert resolve_model("claude-opus-4-7", aliases) == "claude-opus-4-7"


def test_resolve_unknown_alias_raises_when_strict():
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    with pytest.raises(ValueError, match="unknown model alias 'magic'"):
        resolve_model("magic", aliases, strict=True)


def test_resolve_unknown_passes_through_when_not_strict():
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    assert resolve_model("magic", aliases) == "magic"


def test_resolve_chained_aliases():
    aliases = {"a": "b", "b": "c", "c": "claude-opus-4-7"}
    assert resolve_model("a", aliases) == "claude-opus-4-7"


def test_resolve_circular_aliases_raises():
    aliases = {"a": "b", "b": "a"}
    with pytest.raises(ValueError, match="circular"):
        resolve_model("a", aliases)


def test_resolve_self_reference_detected():
    aliases = {"a": "a"}
    with pytest.raises(ValueError, match="circular"):
        resolve_model("a", aliases)


def test_resolve_empty_aliases_passes_through():
    assert resolve_model("claude-opus-4-7", {}) == "claude-opus-4-7"


def test_resolve_none_aliases_passes_through():
    assert resolve_model("claude-opus-4-7", None) == "claude-opus-4-7"


def test_resolve_coerces_non_str_values():
    """Defensive: YAML might surface ints; we coerce silently."""
    # mypy-type-violating but realistic for YAML-loaded configs
    aliases: dict = {"port": 8080, "host": "localhost"}
    assert resolve_model("port", aliases) == "8080"


def test_resolve_skips_none_values():
    aliases: dict = {"a": None, "b": "claude-opus-4-7"}
    assert resolve_model("b", aliases) == "claude-opus-4-7"


def test_max_depth_cap_raises():
    """Chain longer than max_depth treated as a cycle."""
    aliases = {chr(ord("a") + i): chr(ord("a") + i + 1) for i in range(10)}
    with pytest.raises(ValueError):
        resolve_model("a", aliases, max_depth=3)


# ── ModelConfig field round-trip ───────────────────────────────────────


def test_model_config_default_aliases_empty():
    from opencomputer.agent.config import ModelConfig

    m = ModelConfig()
    assert m.model_aliases == {}


def test_model_config_aliases_round_trip():
    from opencomputer.agent.config import ModelConfig

    m = ModelConfig(model_aliases={"fast": "x", "smart": "y"})
    assert m.model_aliases["fast"] == "x"
    assert m.model_aliases["smart"] == "y"


def test_load_config_parses_model_aliases(tmp_path, monkeypatch):
    """End-to-end: YAML → load_config → ModelConfig.model_aliases populated."""
    import yaml

    from opencomputer.agent import config_store

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "anthropic",
                    "model": "claude-opus-4-7",
                    "model_aliases": {
                        "fast": "claude-haiku-4-5-20251001",
                        "smart": "claude-opus-4-7",
                    },
                },
            }
        )
    )
    monkeypatch.setattr(config_store, "config_file_path", lambda: cfg_yaml)
    loaded = config_store.load_config()
    assert loaded.model.model_aliases == {
        "fast": "claude-haiku-4-5-20251001",
        "smart": "claude-opus-4-7",
    }
