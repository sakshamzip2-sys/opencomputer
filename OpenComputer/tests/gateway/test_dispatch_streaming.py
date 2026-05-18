"""A1 — the dispatcher live-streams replies through an edit-capable adapter.

End-to-end over ``Dispatch.handle_message``: an adapter that advertises
``EDIT_MESSAGE`` gets the reply streamed into a placeholder; a plain
adapter keeps the one-shot path.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult


class _EditAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    capabilities = ChannelCapabilities.EDIT_MESSAGE
    max_message_length = 4096

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...

    async def send(self, chat_id: str, text: str, **kw) -> SendResult:
        self.sends.append(text)
        return SendResult(success=True, message_id="m1")

    async def send_typing(self, chat_id: str) -> None: ...

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kw
    ) -> SendResult:
        self.edits.append(text)
        return SendResult(success=True, message_id=message_id)


def _streaming_loop():
    """A loop whose run_conversation drives the stream_callback."""
    loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        cb = kw.get("stream_callback")
        if cb is not None:
            cb("Hello ")
            cb("there")
        result = MagicMock()
        result.final_message = MagicMock(content="Hello there")
        return result

    loop.run_conversation = fake_run
    return loop


def _evt() -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="55",
        user_id="u",
        text="hi",
        timestamp=0.0,
    )


@pytest.mark.asyncio
async def test_edit_capable_adapter_streams_and_suppresses_resend(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    adapter = _EditAdapter()
    dispatch = Dispatch(loop=_streaming_loop())
    dispatch._adapters_by_platform = {"telegram": adapter}

    reply = await dispatch.handle_message(_evt())

    # The final reply landed on the streamed (edited) message...
    assert adapter.edits, "expected the reply to be streamed via edit_message"
    assert adapter.edits[-1] == "Hello there"
    # ...and only the placeholder was *sent* — no duplicate final message.
    assert adapter.sends == ["…"]
    # handle_message returns None so the adapter does not re-send.
    assert reply is None


@pytest.mark.asyncio
async def test_streaming_opt_out_keeps_one_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    adapter = _EditAdapter()
    dispatch = Dispatch(
        loop=_streaming_loop(),
        config={"display": {"streaming": {"enabled": False}}},
    )
    dispatch._adapters_by_platform = {"telegram": adapter}

    reply = await dispatch.handle_message(_evt())

    # Opted out → no placeholder, no edits; the reply is returned for a
    # normal one-shot send.
    assert adapter.edits == []
    assert adapter.sends == []
    assert reply == "Hello there"
