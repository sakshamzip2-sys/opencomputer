"""P1-3: LLM-judge for per-turn quality verdict."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.judge_reviewer import score_turn_via_judge


@pytest.mark.asyncio
async def test_judge_returns_score_and_reasoning():
    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(
        return_value=type(
            "R", (),
            {"text": "<judge_score>0.72</judge_score>"
             "<reasoning>looked fine</reasoning>"},
        )
    )
    out = await score_turn_via_judge(
        provider=fake_provider,
        model="claude-haiku-4-5",
        trajectory_summary="user asked X, agent did Y",
        composite_score=0.6,
        standing_orders="reply concisely",
    )
    assert out is not None
    assert abs(out.judge_score - 0.72) < 0.01
    assert "looked fine" in out.judge_reasoning
    assert out.judge_model == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_judge_returns_none_on_provider_error():
    fake = AsyncMock()
    fake.complete = AsyncMock(side_effect=RuntimeError("API down"))
    out = await score_turn_via_judge(
        provider=fake, model="claude-haiku-4-5",
        trajectory_summary="x", composite_score=0.5, standing_orders="",
    )
    assert out is None


@pytest.mark.asyncio
async def test_judge_returns_none_on_unparseable_response():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=type("R", (), {"text": "I dunno, lol"})
    )
    out = await score_turn_via_judge(
        provider=fake, model="claude-haiku-4-5",
        trajectory_summary="x", composite_score=0.5, standing_orders="",
    )
    assert out is None


@pytest.mark.asyncio
async def test_judge_returns_none_on_out_of_range_score():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=type(
            "R", (), {"text": "<judge_score>1.5</judge_score><reasoning>x</reasoning>"}
        )
    )
    out = await score_turn_via_judge(
        provider=fake, model="claude-haiku-4-5",
        trajectory_summary="x", composite_score=0.5, standing_orders="",
    )
    assert out is None


@pytest.mark.asyncio
async def test_judge_handles_missing_reasoning_block():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=type("R", (), {"text": "<judge_score>0.5</judge_score>"})
    )
    out = await score_turn_via_judge(
        provider=fake, model="claude-haiku-4-5",
        trajectory_summary="x", composite_score=0.5, standing_orders="",
    )
    assert out is not None
    assert out.judge_reasoning == ""
