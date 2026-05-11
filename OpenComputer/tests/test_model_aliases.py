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


def test_resolve_unknown_idshaped_passes_through_when_not_strict():
    """Names that LOOK like model ids (contain a ``-`` / ``/`` / ``:``
    separator) pass through unchanged — legacy compat for users with
    custom or third-party model ids the builtin table doesn't know
    about.

    2026-05-11 — was ``test_resolve_unknown_passes_through_when_not_strict``
    which asserted that the bare-short ``"magic"`` passed through too.
    That contract caused the ``/model opus`` 404 bug — ``"opus"``
    similarly passed through and got forwarded to Anthropic as the
    literal model id. New contract: bare-short unknowns raise, see
    ``test_resolve_unknown_bare_short_now_rejects`` below.
    """
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    assert resolve_model("custom-vendor/magic-model", aliases) == (
        "custom-vendor/magic-model"
    )
    # Plain dashy id also passes through (covers locally-hosted models
    # whose ids the builtin table can't enumerate exhaustively).
    assert resolve_model("llama3.2-3b-instruct", aliases) == (
        "llama3.2-3b-instruct"
    )


def test_resolve_unknown_bare_short_now_rejects():
    """``/model opus``-style bare-short unknowns must raise instead of
    silently passing through — the lesson of the 2026-05-11 regression.
    A name without any separator that isn't in any alias map almost
    certainly produces a 404 on the next API call; refusing here lets
    the slash handler surface a fixable error before the swap persists
    garbage."""
    aliases = {"fast": "claude-haiku-4-5-20251001"}
    with pytest.raises(ValueError, match="unknown model alias 'magic'"):
        resolve_model("magic", aliases)


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
    """Defensive: YAML might surface ints; we coerce silently and then
    apply the same shape-check as any other resolved value.

    2026-05-11 — pre-fix this asserted ``resolve_model("port", {"port":
    8080}) == "8080"``. New contract: ``"8080"`` (no separators) is
    rejected because forwarding it to a provider API would 404 — the
    coercion still happens but the final shape-check then refuses.
    Misconfigured ``model_aliases`` should fail loud at swap time, not
    silently store an int as a model id.

    What's still covered: the int-→-str coercion itself. We verify
    that by mapping to a value that DOES look like a model id (so the
    shape-check accepts it) and confirming the int gets coerced to
    string along the way.
    """
    # mypy-type-violating but realistic for YAML-loaded configs.
    # The int 4 here represents a YAML scalar that someone wrote
    # without quoting — common mistake for digits.
    aliases: dict = {
        "weird": "claude-opus-4-7",
        "host": "localhost",  # NOTE: localhost has no separator either
        # The coercion path: int value gets coerced to "8080" via
        # str(v). We verify the coercion happened by checking that
        # NO TypeError fires (which a non-coerced int would cause
        # downstream).
        "port": 8080,
    }
    # Resolves cleanly when target looks like a model id.
    assert resolve_model("weird", aliases) == "claude-opus-4-7"
    # The coerced int value would now fail the shape-check (intentional
    # — that's the new contract); verify the failure is the "looks
    # unlike a model id" rejection, NOT a TypeError from un-coerced int.
    with pytest.raises(ValueError, match="unknown model alias 'port'"):
        resolve_model("port", aliases)


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
