"""Tests for opencomputer.profile_analysis (2026-04-30)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.profile_analysis import (
    PERSONA_PROFILE_MAP,
    PersonaSessionCount,
    ProfileReport,
    ProfileSuggestion,
    _persona_matches_profile,
    compute_profile_suggestions,
    render_report,
)

# ─── _persona_matches_profile fuzzy matching ──────────────────────────


def test_persona_matches_profile_trading_to_stock():
    assert _persona_matches_profile("trading", "stock") is True


def test_persona_matches_profile_trading_to_finance():
    assert _persona_matches_profile("trading", "finance") is True


def test_persona_matches_profile_coding_to_work():
    assert _persona_matches_profile("coding", "work") is True


def test_persona_matches_profile_coding_to_dev():
    assert _persona_matches_profile("coding", "dev") is True


def test_persona_matches_profile_companion_to_personal():
    """Companion persona → personal profile is a deliberate match (PERSONA_PROFILE_MAP)."""
    assert _persona_matches_profile("companion", "personal") is True


def test_persona_does_not_match_unrelated_profile():
    assert _persona_matches_profile("trading", "personal") is False
    assert _persona_matches_profile("companion", "code") is False


def test_persona_unknown_returns_false():
    assert _persona_matches_profile("unknown_persona", "anything") is False


# ─── compute_profile_suggestions — fixture-based ─────────────────────


def _stub_db(persona_results: dict[str, str], session_ids: list[str]) -> MagicMock:
    """Build a MagicMock SessionDB. ``persona_results`` maps session_id
    to the desired persona; the actual classifier is monkeypatched in
    each test that needs deterministic classification (because the real
    classifier is foreground-app-driven and can't reliably re-derive a
    trading/coding persona from message content alone).
    """
    db = MagicMock()
    db.list_sessions.return_value = [{"id": sid} for sid in session_ids]

    def _get_messages(sid: str):
        msg = MagicMock()
        msg.role = "user"
        msg.content = f"placeholder for {sid}"
        return [msg]

    db.get_messages.side_effect = _get_messages
    return db


def _patch_classifier(monkeypatch, persona_results: dict[str, str]):
    """Monkeypatch the classify() call in profile_analysis to return a
    persona based on the session id embedded in the placeholder message.
    """
    from opencomputer.profile_analysis import _classify_session_persona as _real

    # Replace the per-session classifier call with a stub that reads
    # session_id from the placeholder message content.
    def _stub(db, session_id):
        return persona_results.get(session_id, "default")

    monkeypatch.setattr(
        "opencomputer.profile_analysis._classify_session_persona", _stub,
    )


def test_compute_returns_create_suggestion_for_dominant_unmatched_persona(
    tmp_path, monkeypatch,
):
    """18 trading sessions, no profile matches → suggest CREATE."""
    sessions = {f"sess-{i}": "trading" for i in range(18)}
    sessions.update({f"def-{i}": "default" for i in range(12)})
    db = _stub_db(sessions, list(sessions.keys()))
    _patch_classifier(monkeypatch, sessions)

    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="default",
        available_profiles=("default",),
    )
    create_suggestions = [s for s in report.suggestions if s.kind == "create"]
    assert len(create_suggestions) >= 1
    assert any(s.persona == "trading" for s in create_suggestions)


def test_compute_returns_switch_suggestion_when_matching_profile_exists(
    tmp_path, monkeypatch,
):
    """7 trading sessions + stock profile exists → suggest SWITCH."""
    sessions = {f"sess-{i}": "trading" for i in range(7)}
    sessions.update({f"def-{i}": "default" for i in range(23)})
    db = _stub_db(sessions, list(sessions.keys()))
    _patch_classifier(monkeypatch, sessions)

    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="default",
        available_profiles=("default", "stock"),
    )
    switches = [s for s in report.suggestions if s.kind == "switch"]
    assert len(switches) >= 1
    assert switches[0].profile_name == "stock"


def test_compute_returns_stay_when_already_in_matching_profile(
    tmp_path, monkeypatch,
):
    """7 trading sessions, current is stock → suggest STAY (or no nudge)."""
    sessions = {f"sess-{i}": "trading" for i in range(7)}
    sessions.update({f"def-{i}": "default" for i in range(23)})
    db = _stub_db(sessions, list(sessions.keys()))
    _patch_classifier(monkeypatch, sessions)

    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="stock",
        available_profiles=("default", "stock"),
    )
    stays = [s for s in report.suggestions if s.kind == "stay"]
    assert any(s.profile_name == "stock" for s in stays)


def test_compute_skips_minor_personas_below_threshold(tmp_path, monkeypatch):
    """2 trading sessions < 3-session floor → no suggestion."""
    sessions = {f"sess-{i}": "trading" for i in range(2)}
    sessions.update({f"def-{i}": "default" for i in range(28)})
    db = _stub_db(sessions, list(sessions.keys()))
    _patch_classifier(monkeypatch, sessions)

    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="default",
        available_profiles=("default",),
    )
    assert all(s.persona != "trading" for s in report.suggestions)


def test_compute_handles_empty_history(tmp_path):
    db = MagicMock()
    db.list_sessions.return_value = []
    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="default",
        available_profiles=("default",),
    )
    assert report.sessions_analyzed == 0
    assert report.suggestions == ()
    assert report.persona_breakdown == ()


def test_compute_low_confidence_session_buckets_as_default(
    tmp_path, monkeypatch,
):
    """A session with no-signal content (classifier confidence < 0.5)
    must NOT be counted as the classifier's literal fallback persona,
    or every silent session would spuriously suggest a personal profile.
    """
    sessions = {f"sess-{i}": "default" for i in range(20)}
    db = _stub_db(sessions, list(sessions.keys()))
    _patch_classifier(monkeypatch, sessions)
    report = compute_profile_suggestions(
        home=tmp_path,
        db=db,
        current_profile="default",
        available_profiles=("default",),
    )
    # No suggestions because all sessions bucket as "default".
    assert report.suggestions == ()


# ─── render_report ───────────────────────────────────────────────────


def test_render_report_includes_active_profile_line():
    report = ProfileReport(
        current_profile="stock",
        available_profiles=("default", "stock"),
        persona_breakdown=(),
        suggestions=(),
        sessions_analyzed=0,
    )
    out = render_report(report)
    assert "Active profile: stock" in out


def test_render_report_includes_suggestions():
    report = ProfileReport(
        current_profile="default",
        available_profiles=("default",),
        persona_breakdown=(PersonaSessionCount("trading", 18),),
        suggestions=(
            ProfileSuggestion(
                kind="create",
                profile_name=None,
                persona="trading",
                rationale="18 of last 30 sessions were trading-mode",
                command="oc profile create stock",
            ),
        ),
        sessions_analyzed=30,
    )
    out = render_report(report)
    assert "trading-mode" in out
    assert "oc profile create stock" in out


def test_render_report_handles_empty_history_gracefully():
    report = ProfileReport(
        current_profile="default",
        available_profiles=("default",),
        persona_breakdown=(),
        suggestions=(),
        sessions_analyzed=0,
    )
    out = render_report(report)
    assert "no session history yet" in out.lower()
