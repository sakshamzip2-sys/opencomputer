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
    """Unknown names (including bare short names) pass through unchanged
    in lenient mode — preserves legacy behavior for ``loop.py``'s
    per-turn ``resolve_model`` call which must forward test stubs
    (``mock``), CI synthetic ids, and third-party-plugin short names
    to their providers verbatim.

    Strict-mode rejection of bare-shorts lives in swap_model (see
    ``test_resolve_unknown_bare_short_now_rejects`` and the dedicated
    test_model_resolver_builtin_aliases.py file). User-facing /model
    swap uses strict=True; loop hot path stays lenient.
    """
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    # Bare short — lenient passes through (this is the legacy contract).
    assert resolve_model("magic", aliases) == "magic"
    # Id-shaped — also passes through, covers custom / locally-hosted models.
    assert resolve_model("custom-vendor/magic-model", aliases) == (
        "custom-vendor/magic-model"
    )
    assert resolve_model("llama3.2-3b-instruct", aliases) == (
        "llama3.2-3b-instruct"
    )


def test_resolve_unknown_bare_short_now_rejects_in_strict_mode():
    """``/model opus``-style bare-short unknowns must raise in STRICT
    mode (which is what swap_model uses). Legacy lenient callers keep
    passing through.

    Lesson of the 2026-05-11 regression: ``/model opus`` silently
    stored the literal ``"opus"`` then 404'd on the next API call.
    Rejection at strict-mode swap time turns that into a clean error
    the slash handler surfaces immediately.
    """
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    with pytest.raises(ValueError, match="unknown model alias 'magic'"):
        resolve_model("magic", aliases, strict=True)


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
    """Defensive: YAML might surface ints; we coerce silently.

    In lenient mode (the default, used by loop.py per-turn) the coerced
    string passes through unchanged regardless of shape. In strict mode
    (used by swap_model for user-facing /model swap) a bare-short
    coerced value (e.g. ``"8080"``) is rejected so a misconfigured
    ``model_aliases`` block fails loud at swap time rather than
    silently storing a port number as a model id.
    """
    # mypy-type-violating but realistic for YAML-loaded configs.
    aliases: dict = {"port": 8080, "host": "localhost"}
    # Lenient (legacy contract preserved): coerced int passes through.
    assert resolve_model("port", aliases) == "8080"
    # Strict: same coercion, but the bare-short shape check refuses.
    with pytest.raises(ValueError, match="unknown model alias 'port'"):
        resolve_model("port", aliases, strict=True)


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
