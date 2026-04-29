"""OpenAI provider must emit StreamEvent(kind="thinking_delta")
when delta.reasoning_content is present on streaming chunks."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import StreamEvent

_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions" / "openai-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider", _PROVIDER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_openai_provider"] = mod
    spec.loader.exec_module(mod)
    return mod


def _chunk(text: str = "", reasoning: str = "", finish: str | None = None):
    """Build a chat-completion streaming chunk shape OpenAI yields."""
    delta = SimpleNamespace(
        content=text or None,
        reasoning_content=reasoning or None,
        tool_calls=None,
        role=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(
        choices=[choice], usage=None, id="cmpl-x", model="gpt-test",
    )


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_stream_complete_emits_thinking_delta_from_reasoning_content() -> None:
    mod = _load_provider_module()
    Provider = mod.OpenAIProvider

    fake_chunks = [
        _chunk(reasoning="Let me think... "),
        _chunk(reasoning="step one. "),
        _chunk(text="The answer "),
        _chunk(text="is 42."),
        _chunk(finish="stop"),
    ]

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_FakeStream(list(fake_chunks))
    )

    p = Provider.__new__(Provider)
    # Real attribute is ``self.client`` (no underscore).
    p.client = fake_client  # type: ignore[attr-defined]
    p._credential_pool = None  # force native (non-pool) path
    p.name = "openai"
    p.config = SimpleNamespace(api_key="x", base_url=None)

    events: list[StreamEvent] = []
    async for ev in p.stream_complete(
        model="gpt-5",
        messages=[Message(role="user", content="2+2")],
    ):
        events.append(ev)

    thinking_texts = [e.text for e in events if e.kind == "thinking_delta"]
    text_chunks = [e.text for e in events if e.kind == "text_delta"]

    assert thinking_texts == ["Let me think... ", "step one. "]
    assert "".join(text_chunks) == "The answer is 42."
    assert events[-1].kind == "done"
