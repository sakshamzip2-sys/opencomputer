"""AgentLoop._run_one_step must forward thinking_delta StreamEvents to
the optional ``thinking_callback`` parameter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _FakeProvider(BaseProvider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        raise NotImplementedError

    async def stream_complete(self, **kwargs: Any):
        for ev in self._events:
            yield ev


def _make_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Construct AgentLoop with the real signature.

    AgentLoop.__init__ takes (provider, config, db=None, ...) — no
    ``tools=`` kwarg. ``Config`` is the top-level dataclass at
    opencomputer/agent/config.py:389; ``LoopConfig`` is a sub-component
    on Config.loop and not used here.
    """
    return AgentLoop(
        provider=provider,
        config=Config(
            model=ModelConfig(provider="fake", model="fake-1"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )


@pytest.mark.asyncio
async def test_run_one_step_dispatches_thinking_delta_to_thinking_callback(
    tmp_path,
) -> None:
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="step 1; step 2",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="step 1; "),
        StreamEvent(kind="thinking_delta", text="step 2"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []
    await loop._run_one_step(  # type: ignore[attr-defined]
        messages=[Message(role="user", content="hi")],
        system="",
        stream_callback=text_chunks.append,
        thinking_callback=thinking_chunks.append,
        session_id="s1",
    )

    assert text_chunks == ["answer"]
    assert thinking_chunks == ["step 1; ", "step 2"]


@pytest.mark.asyncio
async def test_run_one_step_ignores_thinking_delta_when_callback_is_none(
    tmp_path,
) -> None:
    """Backwards compat: omitting thinking_callback must not raise."""
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    events = [
        StreamEvent(kind="thinking_delta", text="ignored"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    chunks: list[str] = []
    await loop._run_one_step(  # type: ignore[attr-defined]
        messages=[Message(role="user", content="hi")],
        system="",
        stream_callback=chunks.append,
        session_id="s1",
    )
    assert chunks == ["answer"]


@pytest.mark.asyncio
async def test_run_conversation_threads_thinking_callback_through(
    tmp_path,
) -> None:
    """The PUBLIC entry (run_conversation, loop.py:482) must accept and
    forward ``thinking_callback`` down to ``_run_one_step``."""
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="my chain",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="my chain"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []
    await loop.run_conversation(
        user_message="hi",
        session_id="s1",
        stream_callback=text_chunks.append,
        thinking_callback=thinking_chunks.append,
    )
    assert thinking_chunks == ["my chain"]
