"""Plan 3 Task 6 — seeded SOUL.md generator."""
from __future__ import annotations

from opencomputer.profile_analysis_daily import DailySuggestion
from opencomputer.profile_seeder import render_seeded_soul


def test_seeded_soul_for_coding_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="work",
        persona="coding",
        rationale="22 of last 30 sessions classified as coding, 9am-12pm",
        command="/profile-suggest accept work",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "work-mode agent for Saksham" in soul
    assert "engineering" in soul.lower() or "coding" in soul.lower()
    assert "22 of last 30" in soul  # rationale embedded


def test_seeded_soul_for_trading_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="trading",
        persona="trading",
        rationale="12 of last 30 sessions classified as trading",
        command="/profile-suggest accept trading",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "trading" in soul.lower()
    assert "Saksham" in soul


def test_seeded_soul_for_companion_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="personal",
        persona="companion",
        rationale="8 of last 30 sessions are state-queries",
        command="/profile-suggest accept personal",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "personal-mode agent for Saksham" in soul
    assert "companion register" in soul.lower()


def test_seeded_soul_for_learning_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="study",
        persona="learning",
        rationale="6 of last 30 sessions classified as learning",
        command="/profile-suggest accept study",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "study-mode agent for Saksham" in soul


def test_seeded_soul_falls_back_for_unknown_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="custom",
        persona="custom-persona",
        rationale="weird pattern",
        command="/profile-suggest accept custom",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert len(soul) > 100
    assert "Saksham" in soul
    assert "custom-mode" in soul or "custom" in soul.lower()
