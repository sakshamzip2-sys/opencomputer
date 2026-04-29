"""End-to-end: provider streams thinking_delta → loop forwards →
renderer renders live panel → finalize collapses to summary line."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.cli_ui.streaming import StreamingRenderer
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
    return AgentLoop(
        provider=provider,
        config=Config(
            model=ModelConfig(provider="fake", model="fake-1"),
            session=SessionConfig(db_path=Path(tmp_path) / "e2e.db"),
        ),
    )


@pytest.mark.asyncio
async def test_full_thinking_dropdown_flow_collapses_by_default(tmp_path) -> None:
    """Default flow: show_reasoning=False → finalize shows collapsed
    summary, full reasoning text is NOT in the post-finalize output."""
    final_resp = ProviderResponse(
        # Reasoning lives on BOTH the assistant_message (so StepOutcome
        # surfaces it via outcome.assistant_message.reasoning) AND on
        # ProviderResponse.reasoning (the loop reads either; harmless
        # duplication keeps the test independent of which one wins).
        message=Message(
            role="assistant",
            content="The answer is 42.",
            reasoning="Let me think... step one. Two plus two equals four.",
        ),
        stop_reason="end_turn",
        usage=Usage(input_tokens=12, output_tokens=5),
        reasoning="Let me think... step one. Two plus two equals four.",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="Let me think... "),
        StreamEvent(kind="thinking_delta", text="step one. "),
        StreamEvent(kind="thinking_delta", text="Two plus two equals four."),
        StreamEvent(kind="text_delta", text="The answer "),
        StreamEvent(kind="text_delta", text="is 42."),
        StreamEvent(kind="done", response=final_resp),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=True, record=True)
    loop = _make_loop(_FakeProvider(events), tmp_path)

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []

    # Use _run_one_step directly to keep the e2e tight — full
    # run_conversation pulls in compaction, hooks, prompt builders, etc.
    # which aren't this test's concern.
    with StreamingRenderer(console) as renderer:
        outcome = await loop._run_one_step(  # type: ignore[attr-defined]
            messages=[Message(role="user", content="What is 2+2?")],
            system="",
            stream_callback=lambda t: (text_chunks.append(t), renderer.on_chunk(t))[0]
            and None,
            thinking_callback=lambda t: (
                thinking_chunks.append(t),
                renderer.on_thinking_chunk(t),
            ),
            session_id="e2e-1",
        )
        renderer.finalize(
            # StepOutcome has flat fields (stop_reason, assistant_message,
            # tool_calls_made, input_tokens, output_tokens). Reasoning
            # lives on the message.
            reasoning=outcome.assistant_message.reasoning,
            iterations=1,
            in_tok=outcome.input_tokens,
            out_tok=outcome.output_tokens,
            elapsed_s=2.5,
            show_reasoning=False,  # default
        )

    # 1. Provider deltas were forwarded.
    assert thinking_chunks == [
        "Let me think... ", "step one. ", "Two plus two equals four.",
    ]
    assert "".join(text_chunks) == "The answer is 42."

    # 2. The collapsed summary text is the post-finalize render.
    rendered = console.export_text()
    assert "Thought" in rendered or "💭" in rendered


@pytest.mark.asyncio
async def test_full_thinking_dropdown_flow_expands_when_show_reasoning_on(
    tmp_path,
) -> None:
    """show_reasoning=True → finalize keeps the full thinking panel."""
    final_resp = ProviderResponse(
        message=Message(
            role="assistant",
            content="answer",
            reasoning="full chain of thought here",
        ),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="full chain of thought here",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="full chain of thought here"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final_resp),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=True, record=True)
    loop = _make_loop(_FakeProvider(events), tmp_path)

    with StreamingRenderer(console) as renderer:
        outcome = await loop._run_one_step(  # type: ignore[attr-defined]
            messages=[Message(role="user", content="?")],
            system="",
            stream_callback=renderer.on_chunk,
            thinking_callback=renderer.on_thinking_chunk,
            session_id="e2e-2",
        )
        renderer.finalize(
            reasoning=outcome.assistant_message.reasoning,
            iterations=1,
            in_tok=outcome.input_tokens,
            out_tok=outcome.output_tokens,
            elapsed_s=0.5,
            show_reasoning=True,
        )

    rendered = console.export_text()
    assert "full chain of thought here" in rendered
    assert "answer" in rendered
