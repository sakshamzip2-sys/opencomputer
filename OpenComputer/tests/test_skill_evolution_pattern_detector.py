"""tests/test_skill_evolution_pattern_detector.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.skill_evolution.pattern_detector import (
    CandidateScore,
    is_candidate_session,
    judge_candidate_async,
)

from plugin_sdk.ingestion import SessionEndEvent


def _event(turn_count=10, had_errors=False, session_id="sess123") -> SessionEndEvent:
    return SessionEndEvent(
        session_id=session_id,
        end_reason="completed",
        turn_count=turn_count,
        had_errors=had_errors,
        duration_seconds=120.0,
    )


# Stage 1 tests


def test_stage1_rejects_short_session(tmp_path):
    # Mock session_db to return minimal data
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=2, user_messages_total_chars=200, tool_calls=[]
    )
    score = is_candidate_session(
        _event(turn_count=2),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=None,
    )
    assert score.is_candidate is False
    assert (
        "short" in score.rejection_reason.lower()
        or "turn_count" in score.rejection_reason.lower()
    )


def test_stage1_rejects_conversational_filler(tmp_path):
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=5, user_messages_total_chars=20, tool_calls=[]
    )
    score = is_candidate_session(
        _event(),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=None,
    )
    assert score.is_candidate is False


def test_stage1_rejects_sensitive_session(tmp_path):
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=5, user_messages_total_chars=200, tool_calls=[]
    )
    sensitive_filter = MagicMock(return_value=True)
    score = is_candidate_session(
        _event(),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=sensitive_filter,
    )
    assert score.is_candidate is False
    assert "sensitive" in score.rejection_reason.lower()


def test_stage1_rejects_duplicate_of_existing_skill(tmp_path):
    """Existing skill description has high keyword overlap with session."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Use when reviewing pull requests for security issues\n---\n\nbody"
    )
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=5,
        user_messages_total_chars=200,
        user_messages_concat="please review pull requests for security issues thoroughly",
        tool_calls=[],
    )
    score = is_candidate_session(
        _event(),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=None,
    )
    assert score.is_candidate is False
    assert (
        "duplicate" in score.rejection_reason.lower()
        or "existing" in score.rejection_reason.lower()
    )


def test_stage1_passes_real_pattern(tmp_path):
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=8,
        user_messages_total_chars=400,
        user_messages_concat="port the python module from cpp using cython bindings with proper error handling",
        tool_calls=[],
    )
    score = is_candidate_session(
        _event(turn_count=8),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=None,
    )
    assert score.is_candidate is True
    assert score.rejection_reason == ""


def test_stage1_allows_recovery_after_error(tmp_path):
    """had_errors=True but turns continued after the error → allow."""
    mock_db = MagicMock()
    mock_db.get_session.return_value = MagicMock(
        turn_count=8,
        user_messages_total_chars=400,
        user_messages_concat="something",
        tool_calls=[
            MagicMock(is_error=True, turn_index=2),
            MagicMock(is_error=False, turn_index=4),
            MagicMock(is_error=False, turn_index=6),
        ],
    )
    score = is_candidate_session(
        _event(turn_count=8, had_errors=True),
        session_db=mock_db,
        existing_skills_dir=tmp_path,
        sensitive_filter=None,
    )
    # Should pass — recovery present
    assert score.is_candidate is True


# Stage 2 tests


@pytest.mark.asyncio
async def test_stage2_returns_judgment_on_high_confidence():
    score = CandidateScore(
        is_candidate=True, session_id="s1", turn_count=8, summary_hint="port cpp module"
    )
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(
            content='{"confidence": 85, "novel": true, "reason": "specific port pattern not in skills"}',
        )
    )
    fake_cost_guard = MagicMock()
    fake_cost_guard.check_budget = MagicMock(return_value=True)
    fake_cost_guard.record_usage = MagicMock()

    result = await judge_candidate_async(
        score,
        transcript_summary="user asked to port cpp module to python with cython",
        existing_skill_names=["api-design", "code-review"],
        provider=fake_provider,
        cost_guard=fake_cost_guard,
    )
    assert result is not None
    assert result.confidence == 85
    assert result.is_novel is True


@pytest.mark.asyncio
async def test_stage2_returns_none_on_budget_exhausted():
    score = CandidateScore(is_candidate=True, session_id="s1", turn_count=8)
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock()
    fake_cost_guard = MagicMock()
    fake_cost_guard.check_budget = MagicMock(return_value=False)

    result = await judge_candidate_async(
        score,
        transcript_summary="x",
        existing_skill_names=[],
        provider=fake_provider,
        cost_guard=fake_cost_guard,
    )
    assert result is None
    fake_provider.complete.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_returns_none_on_parse_failure():
    score = CandidateScore(is_candidate=True, session_id="s1", turn_count=8)
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=MagicMock(content="not valid json {{"))
    fake_cost_guard = MagicMock()
    fake_cost_guard.check_budget = MagicMock(return_value=True)
    fake_cost_guard.record_usage = MagicMock()

    result = await judge_candidate_async(
        score,
        transcript_summary="x",
        existing_skill_names=[],
        provider=fake_provider,
        cost_guard=fake_cost_guard,
    )
    assert result is None  # parse failure → safe-default to no candidate


@pytest.mark.asyncio
async def test_stage2_low_confidence_returns_judgment_but_caller_filters():
    score = CandidateScore(is_candidate=True, session_id="s1", turn_count=8)
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(
            content='{"confidence": 35, "novel": false, "reason": "too generic"}',
        )
    )
    fake_cost_guard = MagicMock()
    fake_cost_guard.check_budget = MagicMock(return_value=True)
    fake_cost_guard.record_usage = MagicMock()

    result = await judge_candidate_async(
        score,
        transcript_summary="x",
        existing_skill_names=[],
        provider=fake_provider,
        cost_guard=fake_cost_guard,
    )
    assert result is not None
    assert result.confidence == 35
    # Caller is responsible for thresholding — judge just returns the score
