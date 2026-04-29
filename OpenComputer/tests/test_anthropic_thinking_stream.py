"""Anthropic provider must emit StreamEvent(kind="thinking_delta")
when the SDK yields content_block_delta events with thinking deltas."""
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
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider", _PROVIDER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_anthropic_provider"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeStream:
    """Mimic Anthropic's async-iterator streaming response — when iterated
    directly it yields raw event objects, NOT strings (text_stream)."""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_stream_complete_emits_thinking_delta() -> None:
    mod = _load_provider_module()
    Provider = mod.AnthropicProvider

    # Compose a fake stream: one thinking_delta, one text_delta. The
    # provider should translate the thinking_delta into a
    # StreamEvent(kind="thinking_delta", text="step 1...").
    fake_events = [
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="step 1..."),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="hello"),
        ),
    ]

    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="step 1..."),
            SimpleNamespace(type="text", text="hello"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    fake_stream_obj = _FakeStream(list(fake_events))
    fake_stream_obj.get_final_message = AsyncMock(return_value=fake_response)

    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_stream_obj)
    cm.__aexit__ = AsyncMock(return_value=None)
    fake_client.messages.stream.return_value = cm

    p = Provider.__new__(Provider)
    # The native-stream path uses ``self.client``, NOT ``self._client``.
    p.client = fake_client  # type: ignore[attr-defined]
    p._credential_pool = None  # force native (non-pool) path
    p.name = "anthropic"
    p.config = SimpleNamespace(api_key="x", base_url=None)

    kinds: list[str] = []
    texts: list[str] = []
    async for ev in p.stream_complete(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="hi")],
    ):
        assert isinstance(ev, StreamEvent)
        kinds.append(ev.kind)
        if ev.kind in ("text_delta", "thinking_delta"):
            texts.append(ev.text)

    assert "thinking_delta" in kinds
    assert "text_delta" in kinds
    assert kinds[-1] == "done"
    assert "step 1..." in texts
    assert "hello" in texts
