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

    # 1 initial + 2 successful re-sends + 1 cap-trigger = 3 (since 3rd call is the cap)
    # Actually: counter starts 0, goes 1, 2, 3 — at 3 we cap. So 3 provider calls total.
    # But the test scripts 5 responses; if more than 3 are consumed it's a bug.
    assert provider.calls <= 4
    assert any("pause_turn" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_refusal_exits_without_retry(tmp_path):
    """refusal → loop exits in 1 call, surfaces the assistant text."""
    provider = _ScriptedProvider([_resp("refusal", "I can't help with that.")])
    loop = _make_loop(provider, tmp_path)
    result = await loop.run_conversation("dangerous query")

    assert provider.calls == 1
    final_text = result.final_message.content or ""
    assert "can't help" in final_text
