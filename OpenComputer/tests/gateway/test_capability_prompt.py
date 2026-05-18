"""B3 — channel-capability prompt block rendering."""
from __future__ import annotations

from opencomputer.gateway.capability_prompt import describe_channel_capabilities
from plugin_sdk.channel_contract import ChannelCapabilities
from plugin_sdk.core import Platform


class _Adapter:
    def __init__(self, caps: ChannelCapabilities, *, cap_len: int = 4096):
        self.platform = Platform.TELEGRAM
        self.capabilities = caps
        self.max_message_length = cap_len


def test_none_adapter_returns_empty():
    assert describe_channel_capabilities(None) == ""


def test_non_capabilities_object_returns_empty():
    """A MagicMock-style adapter (caps is not a real flag) → no block."""
    class _Mock:
        platform = "telegram"
        capabilities = object()
        max_message_length = 100

    assert describe_channel_capabilities(_Mock()) == ""


def test_lists_supported_affordances():
    adapter = _Adapter(
        ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.PHOTO_OUT
        | ChannelCapabilities.REACTIONS
    )
    text = describe_channel_capabilities(adapter)
    assert "telegram" in text
    assert "edit messages" in text
    assert "send images" in text
    assert "react to messages with emoji" in text
    # Affordances this channel lacks are not advertised.
    assert "voice" not in text


def test_includes_length_cap():
    text = describe_channel_capabilities(
        _Adapter(ChannelCapabilities.NONE, cap_len=2000)
    )
    assert "2000 characters" in text


def test_no_affordances_still_names_the_channel():
    text = describe_channel_capabilities(
        _Adapter(ChannelCapabilities.NONE, cap_len=0)
    )
    # No flags, no cap → just the channel identification line.
    assert "telegram" in text
    assert "can" not in text  # no affordance sentence


def test_single_affordance_has_no_dangling_comma():
    text = describe_channel_capabilities(
        _Adapter(ChannelCapabilities.PHOTO_OUT, cap_len=0)
    )
    assert "can send images —" in text


# ─── dispatcher threads the capability block onto the runtime ───────────


def test_dispatch_injects_channel_capabilities(tmp_path, monkeypatch):
    """End-to-end: an edit-capable adapter → the runtime handed to
    run_conversation carries the channel_capabilities prompt block."""
    import asyncio
    from unittest.mock import MagicMock

    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.channel_contract import BaseChannelAdapter
    from plugin_sdk.core import MessageEvent, SendResult

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))

    class _Adapter(BaseChannelAdapter):
        platform = Platform.TELEGRAM
        capabilities = ChannelCapabilities.PHOTO_OUT | ChannelCapabilities.REACTIONS
        max_message_length = 4096

        def __init__(self) -> None:  # no config needed for the stub
            ...

        async def connect(self) -> bool:
            return True

        async def disconnect(self) -> None: ...

        async def send(self, chat_id, text, **kw) -> SendResult:
            return SendResult(success=True, message_id="m1")

        async def send_typing(self, chat_id) -> None: ...

    captured: dict = {}

    async def fake_run(user_message, session_id, **kw):
        captured["runtime"] = kw.get("runtime")
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    loop = MagicMock()
    loop.run_conversation = fake_run
    dispatch = Dispatch(loop=loop)
    dispatch._adapters_by_platform = {"telegram": _Adapter()}

    asyncio.run(
        dispatch.handle_message(
            MessageEvent(
                platform=Platform.TELEGRAM,
                chat_id="9",
                user_id="u",
                text="hi",
                timestamp=0.0,
            )
        )
    )

    runtime = captured["runtime"]
    assert runtime is not None
    block = (runtime.custom or {}).get("channel_capabilities", "")
    assert "telegram" in block
    assert "send images" in block
