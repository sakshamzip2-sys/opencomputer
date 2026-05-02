"""tests/test_skill_evolution_skill_extractor.py — includes Subsystem C schema-path coverage."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.skill_evolution.skill_extractor import (
    ProposedSkill,
    _slugify,
    extract_skill_from_session,
)


def _make_response(text: str) -> MagicMock:
    """Build a response mock with both legacy ``.content`` and modern
    ``.message.content`` shapes — so the test fixture works whether the
    extractor uses ``_extract_response_text`` (legacy) or
    ``parse_structured`` (Subsystem C path, which reads
    ``response.message.content``)."""
    resp = MagicMock()
    resp.content = text
    resp.message = MagicMock(content=text)
    return resp


def _ok_provider(
    intent="port cpp module to python",
    procedure_steps=("read source", "add bindings", "run tests"),
    trigger="Use when porting cpp/c modules to python via cython",
    procedure: str | None = None,
):
    """Provider mock returning the 3 expected LLM responses in order.

    The procedure response carries JSON so the Subsystem C schema path
    succeeds — the renderer's deterministic numbering produces the
    same SKILL.md as the legacy ``1. ...\\n2. ...`` format.

    Backwards-compat: callers passing the legacy ``procedure="..."``
    kwarg get the string parsed into steps (line by line, stripping
    leading numbering) and emitted as the JSON shape the schema path
    expects.
    """
    if procedure is not None:
        # Parse legacy "1. step\n2. step\n3. step" into a list of steps.
        parsed: list[str] = []
        for line in procedure.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading numbering like "1. " or "2)".
            import re as _re
            match = _re.match(r"^(?:\d+[.)]?\s*)?(.*)$", line)
            parsed.append(match.group(1) if match else line)
        # Single-line legacy values get split on ". " or treated as one
        # step — the credit-card test passes a single sentence so we
        # accept that as 1 step (schema path will fall back to legacy
        # because <3 steps).
        if len(parsed) == 1 and ". " in parsed[0]:
            parsed = [s.strip() for s in parsed[0].split(". ") if s.strip()]
        procedure_steps = tuple(parsed)
    p = MagicMock()
    responses = [
        _make_response(intent),
        _make_response(json.dumps({"steps": list(procedure_steps)})),
        _make_response(trigger),
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


# ─── Subsystem C follow-up: schema-validated procedure path ─────────


@pytest.mark.asyncio
async def test_procedure_schema_path_renders_numbered_list_deterministically():
    """Schema-validated path: provider returns JSON, renderer numbers
    steps deterministically — output matches the legacy text-path shape
    without depending on the LLM following the prompt's formatting rule.
    """
    db = MagicMock()
    db.get_session.return_value = MagicMock(
        id="sch12345", user_messages_concat="x", tool_calls_summary="y",
    )
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    provider = _ok_provider(
        procedure_steps=("first step", "second step", "third step"),
    )
    proposed = await extract_skill_from_session(
        "sch12345", session_db=db, judge_result=judge,
        provider=provider, cost_guard=_allow_budget(),
    )
    assert proposed is not None
    md = proposed.body
    # Renderer adds the numbering deterministically, regardless of what
    # the LLM emits.
    assert "1. first step" in md
    assert "2. second step" in md
    assert "3. third step" in md


@pytest.mark.asyncio
async def test_procedure_schema_path_falls_back_when_too_few_steps():
    """If the schema-validated steps degrade to <3 after redaction, fall
    back to the legacy text path rather than emit a degraded skill."""
    db = MagicMock()
    db.get_session.return_value = MagicMock(
        id="fb999999", user_messages_concat="x", tool_calls_summary="y",
    )
    judge = MagicMock(confidence=85, is_novel=True, reason="x")

    # Schema returns only 2 steps — Pydantic min_length=3 will reject
    # this at parse time. The fallback legacy path runs.
    p = MagicMock()
    p.complete = AsyncMock(side_effect=[
        _make_response("port cpp module to python"),
        _make_response('{"steps": ["only one", "and another"]}'),
        # Legacy fallback procedure call:
        _make_response("1. first step\n2. second step\n3. third step"),
        _make_response("Use when porting cpp/c modules"),
    ])

    proposed = await extract_skill_from_session(
        "fb999999", session_db=db, judge_result=judge,
        provider=p, cost_guard=_allow_budget(),
    )
    # Fallback path produced a usable skill.
    assert proposed is not None
    assert "1. first step" in proposed.body
