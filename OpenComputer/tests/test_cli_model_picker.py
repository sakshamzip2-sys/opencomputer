"""Tests for ``oc model`` interactive picker (2026-04-30, Hermes-parity)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cli_model_picker import (
    _grouped_models,
    _infer_provider,
    _prompt_pick_one,
)


def test_grouped_models_returns_dict_provider_to_models():
    grouped = _grouped_models()
    # Should return at least anthropic + openai (curated G.32 defaults
    # ship with provider_id=None, but _infer_provider classifies them).
    assert isinstance(grouped, dict)
    assert "anthropic" in grouped, "claude-* models must group under anthropic"
    assert "openai" in grouped, "gpt-* / o-* models must group under openai"
    # Every value is a sorted list of model ids.
    for prov, models in grouped.items():
        assert isinstance(prov, str)
        assert isinstance(models, list)
        assert models == sorted(set(models))


@pytest.mark.parametrize("model_id,expected", [
    ("claude-opus-4-7", "anthropic"),
    ("claude-sonnet-4-6", "anthropic"),
    ("claude-haiku-4-5-20251001", "anthropic"),
    ("gpt-4o", "openai"),
    ("gpt-5.4", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4-mini", "openai"),
    ("gemini-2.0-pro", "google"),
    ("llama-3.1-70b", "meta"),
    ("mixtral-8x7b", "mistral"),
    ("deepseek-coder-v3", "deepseek"),
    ("kimi-k2", "groq"),
    ("some-random-model-xyz", "unknown"),
])
def test_infer_provider_classifies_well_known_prefixes(model_id, expected):
    """_infer_provider must classify every curated G.32 default + common others."""
    assert _infer_provider(model_id) == expected


def test_grouped_models_skips_blank_provider_or_model():
    """Models without provider_id or model_id should not appear."""
    fake = [
        MagicMock(provider_id="anthropic", model_id="claude-opus-4-7"),
        MagicMock(provider_id="", model_id="orphan"),
        MagicMock(provider_id="anthropic", model_id=""),
    ]
    with patch("opencomputer.cli_model_picker.list_models", return_value=fake):
        grouped = _grouped_models()
    assert "anthropic" in grouped
    assert "claude-opus-4-7" in grouped["anthropic"]
    assert "" not in grouped


def test_prompt_pick_one_returns_none_for_empty_options():
    assert _prompt_pick_one("provider", []) is None


def test_prompt_pick_one_accepts_index_input(monkeypatch):
    options = ["a", "b", "c"]
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "2")
    assert _prompt_pick_one("x", options) == "b"


def test_prompt_pick_one_accepts_literal_name(monkeypatch):
    options = ["anthropic", "openai", "groq"]
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "openai")
    assert _prompt_pick_one("provider", options) == "openai"


def test_prompt_pick_one_rejects_out_of_range(monkeypatch):
    options = ["a", "b"]
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "99")
    assert _prompt_pick_one("x", options) is None


def test_prompt_pick_one_rejects_unknown_name(monkeypatch):
    options = ["a", "b"]
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "zorglub")
    assert _prompt_pick_one("x", options) is None


def test_prompt_pick_one_returns_none_for_empty_input(monkeypatch):
    options = ["a", "b"]
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "")
    assert _prompt_pick_one("x", options) is None


# ─── Force-compaction request flag on AgentLoop ──────────────────────


def test_request_force_compaction_sets_flag():
    """AgentLoop.request_force_compaction sets the one-shot flag."""
    from opencomputer.agent.loop import AgentLoop

    # Create a partial loop instance without going through __init__'s
    # heavy setup — just enough to test the flag-setter shape.
    loop = AgentLoop.__new__(AgentLoop)
    loop._force_compact_next_turn = False
    loop.request_force_compaction()
    assert loop._force_compact_next_turn is True
