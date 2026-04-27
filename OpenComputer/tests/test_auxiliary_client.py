"""Tier-A item 15 — AuxiliaryClient cheap-task router."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.auxiliary_client import (
    DEFAULT_MODEL_BY_TASK,
    AuxiliaryClient,
    AuxiliaryConfig,
)


def _provider_returning(text: str):
    p = MagicMock()
    p.complete = AsyncMock(
        return_value=MagicMock(message=MagicMock(content=text))
    )
    return p


# ──────────────────────── model resolution ────────────────────────


def test_default_models_per_task():
    p = _provider_returning("ok")
    client = AuxiliaryClient(p)
    for task, expected in DEFAULT_MODEL_BY_TASK.items():
        assert client.model_for(task) == expected  # type: ignore[arg-type]


def test_per_task_override():
    p = _provider_returning("ok")
    cfg = AuxiliaryConfig(summary_model="haiku-4-3", title_model="custom")
    client = AuxiliaryClient(p, config=cfg)
    assert client.model_for("summary") == "haiku-4-3"
    assert client.model_for("title") == "custom"
    # Untouched defaults still apply.
    assert client.model_for("classify") == DEFAULT_MODEL_BY_TASK["classify"]


# ──────────────────────── task-typed entrypoints ────────────────────────


@pytest.mark.asyncio
async def test_complete_summary_calls_provider_with_haiku():
    p = _provider_returning("compacted text")
    client = AuxiliaryClient(p)
    out = await client.complete_summary("conversation history goes here")
    assert out == "compacted text"
    # Provider was called with the cheap model.
    args, kwargs = p.complete.call_args
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 1024
    # The prompt embeds the text.
    msgs = kwargs["messages"]
    assert "conversation history goes here" in msgs[0].content


@pytest.mark.asyncio
async def test_complete_classify_uses_zero_temperature():
    p = _provider_returning("yes")
    client = AuxiliaryClient(p)
    out = await client.complete_classify("Is this code? Reply yes/no.")
    assert out == "yes"
    _, kwargs = p.complete.call_args
    assert kwargs["temperature"] == 0.0
    # Default short max_tokens for classify.
    assert kwargs["max_tokens"] == 64


@pytest.mark.asyncio
async def test_complete_extract_uses_zero_temperature():
    p = _provider_returning('{"key": "value"}')
    client = AuxiliaryClient(p)
    out = await client.complete_extract("extract JSON")
    assert out == '{"key": "value"}'
    _, kwargs = p.complete.call_args
    assert kwargs["temperature"] == 0.0


@pytest.mark.asyncio
async def test_complete_title_returns_stripped():
    p = _provider_returning("  Stock Briefing Setup  ")
    client = AuxiliaryClient(p)
    out = await client.complete_title("user wanted a daily stock briefing...")
    assert out == "Stock Briefing Setup"
    _, kwargs = p.complete.call_args
    # Title-gen uses a small max_tokens budget.
    assert kwargs["max_tokens"] == 32


@pytest.mark.asyncio
async def test_provider_failure_propagates():
    p = MagicMock()
    p.complete = AsyncMock(side_effect=RuntimeError("provider down"))
    client = AuxiliaryClient(p)
    with pytest.raises(RuntimeError, match="provider down"):
        await client.complete_summary("anything")


# ──────────────────────── cheap_for_first_turn alias ────────────────────────


def test_cheap_for_first_turn_short_simple_message():
    assert AuxiliaryClient.cheap_for_first_turn("hi how are you") is True


def test_cheap_for_first_turn_disqualifies_code_question():
    assert AuxiliaryClient.cheap_for_first_turn("can you fix this code") is False


def test_cheap_for_first_turn_disqualifies_long_message():
    long = "x" * 200
    assert AuxiliaryClient.cheap_for_first_turn(long) is False


def test_cheap_for_first_turn_disqualifies_url():
    assert AuxiliaryClient.cheap_for_first_turn("see https://example.com") is False
