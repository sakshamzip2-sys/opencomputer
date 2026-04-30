"""Tests for persona classifier v2 (multi-signal Bayesian combiner)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.awareness.personas.classifier import (
    ClassificationContext,
    classify,
)
from opencomputer.awareness.personas.classifier_v2 import (
    _file_path_signals,
    _foreground_app_signals,
    _message_content_signals,
    _Signal,
    _state_query_signals,
    _time_of_day_signals,
    _window_title_signals,
    classify_v2,
)

# ─── Individual signal extractors ─────────────────────────────────────


def test_foreground_app_signal_trading():
    ctx = ClassificationContext(foreground_app="TradingView")
    sigs = _foreground_app_signals(ctx)
    assert any(s.persona_id == "trading" for s in sigs)


def test_foreground_app_signal_coding():
    ctx = ClassificationContext(foreground_app="Visual Studio Code")
    sigs = _foreground_app_signals(ctx)
    assert any(s.persona_id == "coding" for s in sigs)


def test_foreground_app_signal_relaxed():
    ctx = ClassificationContext(foreground_app="Spotify")
    sigs = _foreground_app_signals(ctx)
    assert any(s.persona_id == "relaxed" for s in sigs)


def test_foreground_app_signal_empty_when_no_match():
    ctx = ClassificationContext(foreground_app="SomeRandomApp")
    sigs = _foreground_app_signals(ctx)
    assert sigs == []


# ─── Window title signals — NEW (Chrome on TradingView) ──────────────


def test_window_title_chrome_on_tradingview():
    ctx = ClassificationContext(
        foreground_app="Google Chrome",
        window_title="AAPL — TradingView",
    )
    sigs = _window_title_signals(ctx)
    assert any(s.persona_id == "trading" for s in sigs)


def test_window_title_chrome_on_youtube():
    ctx = ClassificationContext(
        foreground_app="Google Chrome",
        window_title="cute cat compilation - YouTube",
    )
    sigs = _window_title_signals(ctx)
    # YouTube alone is relaxed; with "tutorial" / "course" it'd be learning.
    assert any(s.persona_id in ("relaxed", "learning") for s in sigs) or sigs == []


def test_window_title_github():
    ctx = ClassificationContext(
        foreground_app="Google Chrome",
        window_title="sakshamzip2-sys/opencomputer · GitHub",
    )
    sigs = _window_title_signals(ctx)
    assert any(s.persona_id == "coding" for s in sigs)


def test_window_title_empty_returns_empty():
    ctx = ClassificationContext(window_title="")
    assert _window_title_signals(ctx) == []


# ─── State-query signals ──────────────────────────────────────────────


def test_state_query_signal_companion():
    ctx = ClassificationContext(last_messages=("how are you?",))
    sigs = _state_query_signals(ctx)
    assert any(s.persona_id == "companion" for s in sigs)


def test_state_query_signal_no_match():
    ctx = ClassificationContext(last_messages=("fix the python bug",))
    assert _state_query_signals(ctx) == []


# ─── File-path signals ────────────────────────────────────────────────


def test_file_path_signal_python_files_coding():
    ctx = ClassificationContext(
        recent_file_paths=("/tmp/foo.py", "/tmp/bar.py"),
    )
    sigs = _file_path_signals(ctx)
    assert any(s.persona_id == "coding" for s in sigs)


def test_file_path_signal_one_python_file_still_coding():
    """v2 lowers the threshold from >=3 to >=1 — one .py is still signal."""
    ctx = ClassificationContext(recent_file_paths=("/tmp/foo.py",))
    sigs = _file_path_signals(ctx)
    assert any(s.persona_id == "coding" for s in sigs)


def test_file_path_signal_md_files_learning():
    ctx = ClassificationContext(
        recent_file_paths=("/notes/a.md", "/notes/b.md"),
    )
    sigs = _file_path_signals(ctx)
    assert any(s.persona_id == "learning" for s in sigs)


# ─── Message content signals — NEW ────────────────────────────────────


def test_message_content_signal_python_keywords():
    ctx = ClassificationContext(
        last_messages=("can you help me debug this Python function?",),
    )
    sigs = _message_content_signals(ctx)
    assert any(s.persona_id == "coding" for s in sigs)


def test_message_content_signal_trading_keywords():
    ctx = ClassificationContext(
        last_messages=("what's the next AAPL earning date?",),
    )
    sigs = _message_content_signals(ctx)
    assert any(s.persona_id == "trading" for s in sigs)


def test_message_content_signal_recency_weighted():
    """Recent messages weight more than older ones — coding wins
    when most-recent is coding, even if older messages are trading."""
    ctx = ClassificationContext(
        last_messages=(
            "what's AAPL doing?",        # oldest
            "I bought 100 shares",
            "fix the python bug",         # most recent
        ),
    )
    sigs = _message_content_signals(ctx)
    coding_score = sum(s.weight for s in sigs if s.persona_id == "coding")
    trading_score = sum(s.weight for s in sigs if s.persona_id == "trading")
    assert coding_score > trading_score


# ─── Time-of-day signals (weak fallback) ──────────────────────────────


def test_time_of_day_relaxed_late_night():
    ctx = ClassificationContext(time_of_day_hour=23)
    sigs = _time_of_day_signals(ctx)
    assert any(s.persona_id == "relaxed" for s in sigs)


def test_time_of_day_no_signal_midday():
    ctx = ClassificationContext(time_of_day_hour=14)
    assert _time_of_day_signals(ctx) == []


# ─── Combined classify_v2 ─────────────────────────────────────────────


def test_classify_v2_combines_signals_correctly():
    """VS Code + 'fix python bug' → coding (foreground app + content)."""
    ctx = ClassificationContext(
        foreground_app="Visual Studio Code",
        last_messages=("fix the python bug in foo.py",),
    )
    result = classify_v2(ctx)
    assert result.persona_id == "coding"


def test_classify_v2_chrome_tradingview_classifies_trading():
    """Chrome (no app match) but window title says TradingView → trading."""
    ctx = ClassificationContext(
        foreground_app="Google Chrome",
        window_title="AAPL — TradingView",
        last_messages=("what's the next earning?",),
    )
    result = classify_v2(ctx)
    assert result.persona_id == "trading"


def test_classify_v2_state_query_overrides_app():
    """\"how are you\" while in VS Code → companion (state-query weight 0.9
    beats coding-app weight 0.85)."""
    ctx = ClassificationContext(
        foreground_app="Visual Studio Code",
        last_messages=("how are you doing today?",),
    )
    result = classify_v2(ctx)
    assert result.persona_id == "companion"


def test_classify_v2_no_signals_default_companion():
    ctx = ClassificationContext()
    result = classify_v2(ctx)
    assert result.persona_id == "companion"
    assert result.confidence == 0.3


def test_classify_v2_confidence_in_band():
    ctx = ClassificationContext(foreground_app="Visual Studio Code")
    result = classify_v2(ctx)
    assert 0.3 <= result.confidence <= 0.95


# ─── User priors integration ──────────────────────────────────────────


def test_priors_record_and_score(tmp_path):
    from opencomputer.awareness.personas.priors import (
        record_override,
        score_priors,
    )

    record_override(
        profile_home=str(tmp_path),
        persona_id="trading",
        foreground_app="Google Chrome",
        hour=14,
        last_msg="what is AAPL doing",
    )

    ctx = ClassificationContext(
        foreground_app="Google Chrome",
        time_of_day_hour=14,
        profile_home=str(tmp_path),
    )
    sigs = score_priors(ctx)
    assert any(s.persona_id == "trading" for s in sigs)


def test_priors_no_match_when_context_differs(tmp_path):
    from opencomputer.awareness.personas.priors import (
        record_override,
        score_priors,
    )

    record_override(
        profile_home=str(tmp_path),
        persona_id="trading",
        foreground_app="Chrome",
        hour=14,
    )
    # Different hour AND different app → no match.
    ctx = ClassificationContext(
        foreground_app="VS Code",
        time_of_day_hour=22,
        profile_home=str(tmp_path),
    )
    assert score_priors(ctx) == []


def test_priors_handles_missing_profile_home():
    from opencomputer.awareness.personas.priors import score_priors

    ctx = ClassificationContext(profile_home="")
    assert score_priors(ctx) == []


def test_priors_persists_to_json(tmp_path):
    from opencomputer.awareness.personas.priors import record_override

    record_override(
        profile_home=str(tmp_path),
        persona_id="coding",
        foreground_app="Code",
        hour=10,
    )
    path = tmp_path / "persona_priors.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["overrides"][0]["persona_id"] == "coding"


def test_priors_caps_at_max_records(tmp_path):
    from opencomputer.awareness.personas.priors import (
        _MAX_RECORDS,
        record_override,
    )

    for i in range(_MAX_RECORDS + 50):
        record_override(
            profile_home=str(tmp_path),
            persona_id="coding",
            foreground_app=f"app{i}",
            hour=10,
        )
    data = json.loads((tmp_path / "persona_priors.json").read_text())
    assert len(data["overrides"]) == _MAX_RECORDS


# ─── LLM classifier (without actual API calls) ────────────────────────


def test_llm_parse_response_valid_json():
    from opencomputer.awareness.personas.llm_classifier import _parse_response

    raw = '{"persona": "trading", "confidence": 0.92, "why": "stocks mentioned"}'
    result = _parse_response(raw)
    assert result is not None
    assert result.persona_id == "trading"
    assert result.confidence == 0.92


def test_llm_parse_response_rejects_unknown_persona():
    from opencomputer.awareness.personas.llm_classifier import _parse_response

    raw = '{"persona": "bogus_persona", "confidence": 0.95, "why": "x"}'
    assert _parse_response(raw) is None


def test_llm_parse_response_handles_markdown_wrapped():
    from opencomputer.awareness.personas.llm_classifier import _parse_response

    raw = '```json\n{"persona": "coding", "confidence": 0.8, "why": "code"}\n```'
    result = _parse_response(raw)
    assert result is not None
    assert result.persona_id == "coding"


def test_llm_parse_response_clamps_confidence():
    from opencomputer.awareness.personas.llm_classifier import _parse_response

    raw = '{"persona": "coding", "confidence": 1.5, "why": "x"}'
    result = _parse_response(raw)
    assert result is not None
    assert result.confidence == 1.0


def test_llm_classify_async_returns_none_without_api_key(monkeypatch):
    import asyncio

    from opencomputer.awareness.personas.llm_classifier import (
        clear_cache,
        llm_classify_async,
    )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    clear_cache()

    result = asyncio.run(llm_classify_async(
        session_id="test-session",
        foreground_app="Code",
        window_title="",
        last_messages=("hello",),
    ))
    assert result is None


# ─── Backwards-compatible classify() delegates to v2 ─────────────────


def test_classify_delegates_to_v2():
    """Public classify() returns v2 result."""
    ctx = ClassificationContext(
        foreground_app="Visual Studio Code",
        last_messages=("debug python",),
    )
    result = classify(ctx)
    assert result.persona_id == "coding"


def test_v1_frozen_for_regression():
    """The v1 implementation is preserved for emergency rollback."""
    from opencomputer.awareness.personas.classifier import _classify_v1

    ctx = ClassificationContext(foreground_app="TradingView")
    result = _classify_v1(ctx)
    assert result.persona_id == "trading"
    assert result.confidence == 0.85  # v1 fixed weight


# ─── Window-title detection (osascript wrapper) ──────────────────────


def test_detect_window_title_returns_str():
    """detect_window_title returns "" or a string. Non-Mac → "".
    On Mac, may return actual title or "" if no front window.
    """
    from opencomputer.awareness.personas._foreground import detect_window_title

    result = detect_window_title()
    assert isinstance(result, str)
