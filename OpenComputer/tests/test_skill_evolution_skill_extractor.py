"""tests/test_skill_evolution_skill_extractor.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.skill_evolution.skill_extractor import (
    ProposedSkill,
    _slugify,
    extract_skill_from_session,
)


def _ok_provider(intent="port cpp module to python", procedure="1. read source\n2. add bindings\n3. run tests", trigger="Use when porting cpp/c modules to python via cython"):
    """Provider mock returning the 3 expected LLM responses in order."""
    p = MagicMock()
    responses = [
        MagicMock(content=intent),
        MagicMock(content=procedure),
        MagicMock(content=trigger),
    ]
    p.complete = AsyncMock(side_effect=responses)
    return p


def _allow_budget():
    g = MagicMock()
    g.check_budget = MagicMock(return_value=True)
    g.record_usage = MagicMock()
    return g


@pytest.mark.asyncio
async def test_extract_happy_path():
    db = MagicMock()
    db.get_session.return_value = MagicMock(
        id="abc12345xyz",
        user_messages_concat="port cpp to python",
        tool_calls_summary="...",
    )
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    proposed = await extract_skill_from_session(
        "abc12345xyz",
        session_db=db,
        judge_result=judge,
        provider=_ok_provider(),
        cost_guard=_allow_budget(),
    )

    assert proposed is not None
    assert proposed.name.startswith("auto-abc12345-")
    assert "port" in proposed.name
    assert "name:" in proposed.body
    assert "description:" in proposed.body
    assert "auto-generated" in proposed.body.lower() or "auto generated" in proposed.body.lower()
    assert proposed.provenance["session_id"] == "abc12345xyz"
    assert proposed.provenance["confidence_score"] == 85


@pytest.mark.asyncio
async def test_extract_returns_none_on_budget_exhausted_first_call():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="x", user_messages_concat="x", tool_calls_summary="x")
    g = MagicMock()
    g.check_budget = MagicMock(side_effect=[False])
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    proposed = await extract_skill_from_session(
        "x", session_db=db, judge_result=judge, provider=MagicMock(), cost_guard=g,
    )
    assert proposed is None


@pytest.mark.asyncio
async def test_extract_redacts_sensitive_in_procedure():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="abc12345", user_messages_concat="x", tool_calls_summary="x")
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    def sensitive_filter(text: str) -> bool:
        return "Banking" in text

    provider = _ok_provider(
        procedure="1. open Banking app\n2. log in\n3. transfer",
    )
    proposed = await extract_skill_from_session(
        "abc12345", session_db=db, judge_result=judge,
        provider=provider, cost_guard=_allow_budget(),
        sensitive_filter=sensitive_filter,
    )
    # Either redacted or None — depending on how aggressive the filter is.
    # We test the contract: if anything containing "Banking" survives, fail.
    if proposed is not None:
        assert "Banking" not in proposed.body, "sensitive content leaked into body"


@pytest.mark.asyncio
async def test_extract_redacts_credit_card_pattern():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="abc12345", user_messages_concat="x", tool_calls_summary="x")
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    provider = _ok_provider(procedure="user typed card 4111-1111-1111-1111 to pay")
    proposed = await extract_skill_from_session(
        "abc12345", session_db=db, judge_result=judge,
        provider=provider, cost_guard=_allow_budget(),
    )
    if proposed is not None:
        assert "4111" not in proposed.body
        assert "redacted" in proposed.body.lower()


@pytest.mark.asyncio
async def test_extract_returns_none_on_llm_error_mid_call():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="abc12345", user_messages_concat="x", tool_calls_summary="x")
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[
        MagicMock(content="intent ok"),
        RuntimeError("LLM down"),
    ])

    proposed = await extract_skill_from_session(
        "abc12345", session_db=db, judge_result=judge,
        provider=provider, cost_guard=_allow_budget(),
    )
    assert proposed is None


def test_slugify_basic():
    assert _slugify("Port C++ Module to Python") == "port-c-module-to-python"


def test_slugify_truncates():
    s = _slugify("a" * 100, max_len=20)
    assert len(s) <= 20


def test_slugify_handles_empty():
    assert _slugify("") == "untitled"


def test_slugify_handles_only_special_chars():
    assert _slugify("!@#$%") == "untitled"


@pytest.mark.asyncio
async def test_extract_generated_name_uses_session_prefix():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="sessionABCDEFGHIJ", user_messages_concat="x", tool_calls_summary="x")
    judge = MagicMock(confidence=85, is_novel=True, reason="x")
    proposed = await extract_skill_from_session(
        "sessionABCDEFGHIJ", session_db=db, judge_result=judge,
        provider=_ok_provider(), cost_guard=_allow_budget(),
    )
    assert proposed is not None
    assert proposed.name.startswith("auto-sessionA-")  # 8-char prefix


@pytest.mark.asyncio
async def test_extract_provenance_includes_metadata():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="abc12345", user_messages_concat="x", tool_calls_summary="x")
    judge = MagicMock(confidence=72, is_novel=True, reason="moderately novel")
    proposed = await extract_skill_from_session(
        "abc12345", session_db=db, judge_result=judge,
        provider=_ok_provider(), cost_guard=_allow_budget(),
    )
    assert proposed is not None
    p = proposed.provenance
    assert p["session_id"] == "abc12345"
    assert p["confidence_score"] == 72
    assert "generated_at" in p
