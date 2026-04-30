"""Tests for ``oc model`` interactive picker (2026-04-30, Hermes-exact UX)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cli_model_picker import (
    _grouped_models,
    _infer_provider,
    _label_with_marker,
    _provider_label,
)

# ─── _grouped_models — registry → provider→models dict ──────────────


def test_grouped_models_returns_dict_provider_to_models():
    grouped = _grouped_models()
    # G.32 catalog ships every entry with provider_id=None — _infer_provider
    # classifies them so anthropic + openai must appear.
    assert isinstance(grouped, dict)
    assert "anthropic" in grouped, "claude-* must group under anthropic"
    assert "openai" in grouped, "gpt/o-* must group under openai"
    for prov, models in grouped.items():
        assert isinstance(prov, str)
        assert isinstance(models, list)
        assert models == sorted(set(models))


def test_grouped_models_skips_blank_model_id():
    fake = [
        MagicMock(provider_id="anthropic", model_id="claude-opus-4-7"),
        MagicMock(provider_id="anthropic", model_id=""),
        MagicMock(provider_id="anthropic", model_id=None),
    ]
    with patch("opencomputer.cli_model_picker.list_models", return_value=fake):
        grouped = _grouped_models()
    assert "anthropic" in grouped
    assert grouped["anthropic"] == ["claude-opus-4-7"]


# ─── _infer_provider — model-id → provider mapping ─────────────────


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
    assert _infer_provider(model_id) == expected


# ─── _label_with_marker — Hermes-exact suffix ──────────────────────


def test_label_with_marker_renders_arrow_suffix():
    out = _label_with_marker("anthropic", marker="currently active")
    assert out == "anthropic  ← currently active"


def test_label_with_marker_for_model_in_use():
    out = _label_with_marker("claude-opus-4-7", marker="currently in use")
    assert out == "claude-opus-4-7  ← currently in use"


# ─── _provider_label — capitalised display ─────────────────────────


@pytest.mark.parametrize("provider,expected", [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("groq", "Groq"),
    ("openrouter", "OpenRouter"),
    ("deepseek", "DeepSeek"),
    ("google", "Google"),
    ("unknown_provider_x", "Unknown_provider_x"),
])
def test_provider_label_capitalises(provider, expected):
    assert _provider_label(provider) == expected


# ─── pick_one terminal-menu wrapper ────────────────────────────────


def test_pick_one_returns_none_for_empty_choices():
    from opencomputer.cli_ui.term_menu import pick_one
    assert pick_one("title", []) is None


def test_pick_one_falls_through_to_numbered_in_non_tty(monkeypatch):
    """When TerminalMenu + curses both unavailable (non-TTY), pick_one
    falls through to the numbered fallback."""
    from opencomputer.cli_ui import term_menu

    # Force both tty checks to fail.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # Patch input() to pick option 2.
    monkeypatch.setattr("builtins.input", lambda *_: "2")

    result = term_menu.pick_one("title", ["a", "b", "c"], allow_cancel=False)
    assert result == 1  # 0-based, "b"


# ─── Force-compaction request flag on AgentLoop ────────────────────


def test_request_force_compaction_sets_flag():
    """AgentLoop.request_force_compaction sets the one-shot flag."""
    from opencomputer.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._force_compact_next_turn = False
    loop.request_force_compaction()
    assert loop._force_compact_next_turn is True
