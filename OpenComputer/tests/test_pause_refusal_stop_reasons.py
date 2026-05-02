"""Tests for pause_turn and refusal stop_reason handling in the agent loop.

Fixture pattern adapted from tests/test_loop_emits_bus_events.py — build a
real AgentLoop against a scripted provider.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _ScriptedProvider(BaseProvider):
    """Provider that returns a pre-scripted sequence of responses."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete(
        self, *, model, messages, system, tools, max_tokens, temperature, **_kwargs: Any
    ) -> ProviderResponse:
        if not self._responses:
            raise AssertionError("scripted provider exhausted")
        self.calls += 1
        return self._responses.pop(0)

    async def stream_complete(
        self, *, model, messages, system, tools, max_tokens, temperature, **_kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        # Tests drive .complete() via the loop's non-streaming path.
        resp = await self.complete(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature,
        )

        class _Done:
            kind = "done"

            def __init__(self, r: ProviderResponse) -> None:
                self.response = r

        yield _Done(resp)


def _resp(stop_reason: str, text: str = "") -> ProviderResponse:
    return ProviderResponse(
        message=Message(role="assistant", content=text),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _make_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Minimal loop wired to tmp SessionDB. Mirrors test_loop_emits_bus_events.

    max_iterations bumped to 5 so the cap-exceeded test can show all 4 calls.
    """
    cfg = Config(
        loop=LoopConfig(max_iterations=5, parallel_tools=False),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    return AgentLoop(
        provider=provider,
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


@pytest.mark.asyncio
async def test_pause_turn_then_end_turn_continues_loop(tmp_path):
    """pause_turn → re-send → end_turn yields final answer in 2 calls."""
    provider = _ScriptedProvider([
        _resp("pause_turn", "(paused)"),
        _resp("end_turn", "Final answer."),
    ])
    loop = _make_loop(provider, tmp_path)
    result = await loop.run_conversation("test query")

    assert provider.calls == 2
    final_text = result.final_message.content or ""
    assert "Final answer" in final_text


@pytest.mark.asyncio
async def test_pause_turn_cap_exceeded_exits_with_warning(tmp_path, caplog):
    """4 consecutive pause_turn → loop exits at cap (≤4 calls), warning logged."""
    provider = _ScriptedProvider([
        _resp("pause_turn", f"paused {i}") for i in range(5)
    ])
    loop = _make_loop(provider, tmp_path)

    with caplog.at_level("WARNING"):
        await loop.run_conversation("test query")

    # Counter starts 0; each pause_turn bumps it. Cap fires when counter
    # reaches 3 — at which point the 3rd pause is forced to END_TURN and the
    # loop exits without re-sending. Sequence:
    #   call 1 → counter=1 (continue), call 2 → counter=2 (continue),
    #   call 3 → counter=3 (cap → END_TURN, no further calls).
    # So exactly 3 provider calls; never 4 or more.
    assert provider.calls == 3, (
        f"expected exactly 3 provider calls (cap fires on 3rd pause), got {provider.calls}"
    )
    assert any("pause_turn" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_pause_turn_counter_resets_between_conversations(tmp_path):
    """B1 fix (2026-05-02): _pause_turn_count must reset per run_conversation.

    Without the reset, a long-lived AgentLoop (gateway/daemon) handling
    sequential conversations would leak the counter — session B could start
    with cap=1 or 2 already and force premature END_TURN.
    """
    # First conversation: 2 pause_turn responses (below cap), then end_turn.
    provider = _ScriptedProvider([
        _resp("pause_turn", "p1a"),
        _resp("pause_turn", "p1b"),
        _resp("end_turn", "Done first."),
        # Second conversation: 2 pause_turn (would hit cap 4 if counter leaked)
        # then end_turn. With reset, counter starts fresh; both pauses succeed.
        _resp("pause_turn", "p2a"),
        _resp("pause_turn", "p2b"),
        _resp("end_turn", "Done second."),
    ])
    loop = _make_loop(provider, tmp_path)

    r1 = await loop.run_conversation("first")
    r2 = await loop.run_conversation("second")

    assert provider.calls == 6
    assert "Done first" in (r1.final_message.content or "")
    assert "Done second" in (r2.final_message.content or "")


@pytest.mark.asyncio
async def test_refusal_exits_without_retry(tmp_path):
    """refusal → loop exits in 1 call, surfaces the assistant text."""
    provider = _ScriptedProvider([_resp("refusal", "I can't help with that.")])
    loop = _make_loop(provider, tmp_path)
    result = await loop.run_conversation("dangerous query")

    assert provider.calls == 1
    final_text = result.final_message.content or ""
    assert "can't help" in final_text
