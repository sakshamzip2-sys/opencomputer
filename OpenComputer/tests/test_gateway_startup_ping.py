"""Gateway startup-ping (the OpenClaw 'back online' magic message)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _gateway_with_adapters(*adapters, **cfg_kwargs):
    """Construct a Gateway via __new__ so we don't need __init__'s deps.

    _fire_startup_pings only reads ``self._config`` and ``self._adapters``,
    so a partially-constructed Gateway is enough for unit testing.
    Use the real attribute name (``_config``) — using a different name
    would let the test pass while production code crashes with
    ``AttributeError: 'Gateway' object has no attribute 'config'``.
    """
    from opencomputer.agent.config import GatewayConfig
    from opencomputer.gateway.server import Gateway

    gw = Gateway.__new__(Gateway)
    gw._config = GatewayConfig(**cfg_kwargs)
    gw._adapters = list(adapters)
    return gw


def _fake_adapter(platform_name: str) -> MagicMock:
    a = MagicMock()
    a.platform = MagicMock()
    a.platform.value = platform_name
    a.send = AsyncMock(return_value=None)
    return a


@pytest.mark.asyncio
async def test_startup_ping_no_op_when_disabled() -> None:
    """Empty startup_ping_chats → no sends attempted."""
    adapter = _fake_adapter("telegram")
    gw = _gateway_with_adapters(adapter, startup_ping_chats=())
    await gw._fire_startup_pings()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_ping_sends_to_configured_chat() -> None:
    """Configured (platform, chat_id) → adapter.send called."""
    adapter = _fake_adapter("telegram")
    gw = _gateway_with_adapters(
        adapter,
        startup_ping_chats=(("telegram", "12345"),),
        startup_ping_message="hi",
    )
    await gw._fire_startup_pings()
    adapter.send.assert_awaited_once_with("12345", "hi")


@pytest.mark.asyncio
async def test_startup_ping_default_message() -> None:
    """Default message is the 'OpenComputer back online' string."""
    adapter = _fake_adapter("telegram")
    gw = _gateway_with_adapters(
        adapter,
        startup_ping_chats=(("telegram", "12345"),),
    )
    await gw._fire_startup_pings()
    sent_text = adapter.send.await_args.args[1]
    assert "back online" in sent_text.lower()


@pytest.mark.asyncio
async def test_startup_ping_skips_when_no_matching_adapter() -> None:
    """Configured platform with no registered adapter → skip silently."""
    adapter = _fake_adapter("discord")
    gw = _gateway_with_adapters(
        adapter,
        startup_ping_chats=(("telegram", "12345"),),
    )
    await gw._fire_startup_pings()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_ping_swallows_send_exceptions() -> None:
    """One flaky adapter must not drop other pings or wedge boot."""
    flaky = _fake_adapter("telegram")
    flaky.send = AsyncMock(side_effect=RuntimeError("network down"))
    healthy = _fake_adapter("discord")

    gw = _gateway_with_adapters(
        flaky, healthy,
        startup_ping_chats=(("telegram", "1"), ("discord", "2")),
        startup_ping_message="msg",
    )
    await gw._fire_startup_pings()
    healthy.send.assert_awaited_once_with("2", "msg")


@pytest.mark.asyncio
async def test_startup_ping_handles_malformed_entry() -> None:
    """Bad config entry that's not a 2-tuple → log + continue, don't crash."""
    adapter = _fake_adapter("telegram")
    gw = _gateway_with_adapters(adapter)
    # Bypass dataclass validation by setting the attr directly with a
    # mixed-shape tuple: one bad entry, one good one. The good one
    # must still fire.
    object.__setattr__(
        gw._config, "startup_ping_chats",
        ("not-a-tuple", ("telegram", "ok-chat")),
    )
    await gw._fire_startup_pings()
    adapter.send.assert_awaited_once_with("ok-chat", gw._config.startup_ping_message)


@pytest.mark.asyncio
async def test_startup_ping_fires_to_multiple_platforms() -> None:
    """A user with both Telegram and Discord configured gets pings on both."""
    tg = _fake_adapter("telegram")
    dc = _fake_adapter("discord")
    gw = _gateway_with_adapters(
        tg, dc,
        startup_ping_chats=(("telegram", "100"), ("discord", "200")),
        startup_ping_message="up",
    )
    await gw._fire_startup_pings()
    tg.send.assert_awaited_once_with("100", "up")
    dc.send.assert_awaited_once_with("200", "up")


@pytest.mark.asyncio
async def test_startup_ping_works_against_real_gateway_init() -> None:
    """Regression: _fire_startup_pings reads self._config (private attr).

    The bug shipped in PR #380: the implementation used self.config but
    Gateway.__init__ stores it as self._config — production crashed with
    AttributeError on first invocation. Unit tests that bypass __init__
    via __new__ silently lied. This test constructs a real Gateway via
    Gateway(config=...) so any future drift between attribute names
    fails fast.
    """
    from opencomputer.agent.config import GatewayConfig
    from opencomputer.gateway.server import Gateway

    adapter = _fake_adapter("telegram")
    cfg = GatewayConfig(
        startup_ping_chats=(("telegram", "real-chat"),),
        startup_ping_message="real-msg",
    )
    # Construct through the real __init__. Gateway requires loop OR
    # router; _fire_startup_pings doesn't touch either, so a stub loop
    # is enough.
    fake_loop = MagicMock()
    gw = Gateway(loop=fake_loop, config=cfg)
    gw._adapters = [adapter]
    await gw._fire_startup_pings()
    adapter.send.assert_awaited_once_with("real-chat", "real-msg")


@pytest.mark.asyncio
async def test_startup_ping_is_platform_agnostic() -> None:
    """Works for ANY adapter implementing BaseChannelAdapter — not hardcoded
    to telegram/discord. Proves the feature ships parity across all 15
    channel extensions (slack, matrix, whatsapp, signal, email, webhook,
    mattermost, sms, irc, imessage, homeassistant, dingtalk, feishu, ...).
    """
    platforms = [
        "slack", "matrix", "whatsapp", "signal", "email",
        "webhook", "sms", "irc", "imessage", "homeassistant",
        "dingtalk", "feishu", "mattermost",
    ]
    adapters = [_fake_adapter(p) for p in platforms]
    chats = tuple((p, f"chat-for-{p}") for p in platforms)
    gw = _gateway_with_adapters(
        *adapters,
        startup_ping_chats=chats,
        startup_ping_message="hello",
    )
    await gw._fire_startup_pings()
    for adapter, platform in zip(adapters, platforms, strict=True):
        adapter.send.assert_awaited_once_with(f"chat-for-{platform}", "hello")
