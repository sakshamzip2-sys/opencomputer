"""Tests for ${VAR} substitution in config.yaml.

Hermes config v2 contract:
- Only ${VAR} syntax is expanded; bare $VAR is not.
- Multiple references in one value work: "${HOST}:${PORT}".
- Undefined vars are kept verbatim (${UNDEFINED}).
- Single-pass: no recursive expansion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config_store import _expand_env_vars, load_config


def test_substitutes_defined_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc-123")
    out = _expand_env_vars({"api_key": "${OPENAI_API_KEY}"})
    assert out == {"api_key": "sk-abc-123"}


def test_keeps_undefined_var_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNDEFINED_TEST_VAR_X", raising=False)
    out = _expand_env_vars({"x": "${UNDEFINED_TEST_VAR_X}"})
    assert out == {"x": "${UNDEFINED_TEST_VAR_X}"}


def test_multiple_refs_in_one_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_HOST", "localhost")
    monkeypatch.setenv("TEST_PORT", "8080")
    out = _expand_env_vars({"url": "${TEST_HOST}:${TEST_PORT}"})
    assert out == {"url": "localhost:8080"}


def test_does_not_expand_bare_dollar_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BARE_VAR_TEST", "secret")
    out = _expand_env_vars({"x": "$BARE_VAR_TEST"})
    assert out == {"x": "$BARE_VAR_TEST"}


def test_walks_nested_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NESTED_KEY_TEST", "v")
    out = _expand_env_vars({"outer": {"inner": "${NESTED_KEY_TEST}"}})
    assert out == {"outer": {"inner": "v"}}


def test_walks_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIST_VAR_TEST", "x")
    out = _expand_env_vars({"items": ["${LIST_VAR_TEST}", "${LIST_VAR_TEST}-suffix"]})
    assert out == {"items": ["x", "x-suffix"]}


def test_leaves_non_string_values_untouched() -> None:
    out = _expand_env_vars({"n": 42, "b": True, "f": 3.14, "none": None})
    assert out == {"n": 42, "b": True, "f": 3.14, "none": None}


def test_single_pass_no_recursion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Value of OUTER is the literal string '${INNER}'. After one pass,
    we should see '${INNER}' (literal); we do NOT recurse to resolve INNER.
    """
    monkeypatch.setenv("RECURSE_OUTER", "${RECURSE_INNER}")
    monkeypatch.setenv("RECURSE_INNER", "deep_value")
    out = _expand_env_vars({"x": "${RECURSE_OUTER}"})
    assert out == {"x": "${RECURSE_INNER}"}


def test_load_config_applies_env_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: env vars are substituted at load_config() time."""
    monkeypatch.setenv("MY_LOAD_CONFIG_TEST_KEY", "hello-world")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: ${MY_LOAD_CONFIG_TEST_KEY}\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.memory.provider == "hello-world"
