"""Tests for new stop-reason handlers added in the Opus 4.7 migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _ScriptedProvider(BaseProvider):
    """Returns a sequence of pre-built ProviderResponses on successive calls."""

    name = "scripted"
    default_model = "claude-opus-4-7"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = responses
        self._idx = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        resp = self._responses[self._idx]
        self._idx += 1
        return resp

    async def stream_complete(self, **kwargs: Any):
        resp = await self.complete(**kwargs)
        if resp.message.content:
            yield StreamEvent(kind="text_delta", text=resp.message.content)
        yield StreamEvent(kind="done", response=resp)


def _resp(
    stop_reason: str,
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 10,
) -> ProviderResponse:
    return ProviderResponse(
        message=Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        ),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Match the construction pattern from test_loop_thinking_dispatch.py."""
    return AgentLoop(
        provider=provider,
        config=Config(
            model=ModelConfig(provider="scripted", model="claude-opus-4-7"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )


@pytest.mark.asyncio
async def test_refusal_maps_to_stop_reason_refusal(tmp_path) -> None:
    """When stop_reason='refusal', loop emits StopReason.REFUSAL not END_TURN.

    Asserts on the new `ConversationResult.stop_reason` field added in
    this task (additive — defaults to None for legacy callers).
    """
    provider = _ScriptedProvider([
        _resp("refusal", content="I cannot help with that request."),
    ])
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test prompt", session_id="t1")
    assert result.stop_reason == StopReason.REFUSAL
    assert "declined" in result.final_message.content.lower()
    # Original model text should be preserved alongside our marker.
    assert "cannot help" in result.final_message.content.lower()
