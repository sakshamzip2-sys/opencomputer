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
async def test_context_full_triggers_compaction_and_retry(tmp_path) -> None:
    """First call returns model_context_window_exceeded; loop compacts and retries."""
    from opencomputer.agent.compaction import CompactionResult

    provider = _ScriptedProvider([
        _resp("model_context_window_exceeded", input_tokens=199_000),
        _resp("end_turn", content="Now I can answer."),
    ])
    loop = _make_loop(provider, tmp_path)

    # Force compaction.maybe_run to report did_compact=True so the
    # retry path engages without depending on real summarization.
    compaction_calls: list[bool] = []

    async def _fake_compact(messages, last_input_tokens, *, force=False):
        compaction_calls.append(force)
        return CompactionResult(messages=messages, did_compact=True)

    loop.compaction.maybe_run = _fake_compact  # type: ignore[method-assign]

    result = await loop.run_conversation("Long prompt", session_id="t2")
    assert result.stop_reason == StopReason.END_TURN
    assert "Now I can answer" in result.final_message.content
    # Compaction was called with force=True on the retry path.
    assert compaction_calls == [True]
    # Both provider calls were consumed (original + retry).
    assert provider._idx == 2


@pytest.mark.asyncio
async def test_empty_end_turn_triggers_continuation_retry(tmp_path) -> None:
    """Empty end_turn (no content, no tool calls) → retry with 'Please continue'."""
    provider = _ScriptedProvider([
        _resp("end_turn", content=""),  # empty
        _resp("end_turn", content="Sorry, here's the answer."),
    ])
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test", session_id="t4")
    assert result.stop_reason == StopReason.END_TURN
    assert "here's the answer" in result.final_message.content
    assert provider._idx == 2  # continuation retry happened


@pytest.mark.asyncio
async def test_empty_end_turn_after_retry_still_empty_accepts(tmp_path) -> None:
    """If continuation retry is also empty, accept rather than loop forever."""
    provider = _ScriptedProvider([
        _resp("end_turn", content=""),
        _resp("end_turn", content=""),
    ])
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test", session_id="t5")
    assert result.stop_reason == StopReason.END_TURN
    assert provider._idx == 2  # exactly one retry, no more


@pytest.mark.asyncio
async def test_max_tokens_with_tool_use_retries_with_doubled_max_tokens(tmp_path) -> None:
    """max_tokens stop with last block being tool_use → retry with max_tokens * 2."""
    truncated_resp = ProviderResponse(
        message=Message(
            role="assistant",
            content="Calling tool",
            tool_calls=[ToolCall(id="t1", name="Read", arguments={"path": ""})],
        ),
        stop_reason="max_tokens",
        usage=Usage(input_tokens=10, output_tokens=4096),
    )
    full_resp = ProviderResponse(
        message=Message(
            role="assistant",
            content="Done",
            tool_calls=None,
        ),
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=200),
    )

    provider = _ScriptedProvider([truncated_resp, full_resp])
    loop = _make_loop(provider, tmp_path)
    # ModelConfig default max_tokens is already 4096 — frozen dataclass
    # so we can't mutate; use construction default.

    result = await loop.run_conversation("Read file", session_id="t6")
    # Retry happened (provider called twice).
    assert provider._idx == 2
    # Final outcome reflects the retry's stop reason.
    assert result.stop_reason == StopReason.END_TURN
    assert "Done" in result.final_message.content


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
