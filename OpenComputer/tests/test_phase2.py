"""Phase 2 tests: gateway protocol, dispatch, telegram adapter basics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── wire protocol ──────────────────────────────────────────────


def test_wire_request_response_validate() -> None:
    from opencomputer.gateway.protocol import WireRequest, WireResponse

    req = WireRequest(id="1", method="chat", params={"message": "hi"})
    assert req.type == "req"
    assert req.method == "chat"

    res = WireResponse(id="1", ok=True, payload={"text": "hello"})
    assert res.type == "res"
    assert res.ok


# ─── dispatch ───────────────────────────────────────────────────


def test_dispatch_session_id_is_stable_per_chat() -> None:
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent, Platform

    mock_loop = MagicMock()
    d = Dispatch(mock_loop)
    e1 = MessageEvent(platform=Platform.TELEGRAM, chat_id="123", user_id="u", text="hi", timestamp=0.0)
    e2 = MessageEvent(platform=Platform.TELEGRAM, chat_id="123", user_id="u", text="yo", timestamp=0.0)
    e3 = MessageEvent(platform=Platform.DISCORD, chat_id="123", user_id="u", text="hi", timestamp=0.0)

    assert d._session_id_for(e1) == d._session_id_for(e2)
    assert d._session_id_for(e1) != d._session_id_for(e3)


def test_dispatch_skips_empty_messages() -> None:
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent, Platform

    mock_loop = MagicMock()
    d = Dispatch(mock_loop)
    e = MessageEvent(platform=Platform.TELEGRAM, chat_id="1", user_id="u", text="   ", timestamp=0.0)
    result = asyncio.run(d.handle_message(e))
    assert result is None
    mock_loop.run_conversation.assert_not_called()


def test_dispatch_routes_to_agent_loop() -> None:
    from opencomputer.agent.loop import ConversationResult
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import Message, MessageEvent, Platform

    final = Message(role="assistant", content="world")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(return_value=result)
    d = Dispatch(mock_loop)
    e = MessageEvent(platform=Platform.TELEGRAM, chat_id="1", user_id="u", text="hello", timestamp=0.0)
    out = asyncio.run(d.handle_message(e))
    assert out == "world"


# ─── telegram helpers ───────────────────────────────────────────


def test_telegram_escape_and_chunk() -> None:
    from extensions.telegram.src.adapter import _chunk_for_telegram, _escape_mdv2, _utf16_len

    # Escaping
    assert _escape_mdv2("hello_world") == "hello\\_world"
    assert _escape_mdv2("a.b(c)") == "a\\.b\\(c\\)"

    # UTF-16 length vs char count — emoji takes 2 units
    assert _utf16_len("hi") == 2
    assert _utf16_len("😀") == 2  # surrogate pair

    # Chunking splits long text on line boundaries
    long = "\n".join(["line " * 100 for _ in range(50)])
    chunks = _chunk_for_telegram(long, limit=4096)
    assert all(_utf16_len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == long


def test_telegram_plugin_manifest_discoverable(tmp_path) -> None:
    """The Telegram plugin manifest should be discoverable by the plugin loader."""
    from pathlib import Path

    from opencomputer.plugins.discovery import discover

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    candidates = discover([ext_dir])
    ids = [c.manifest.id for c in candidates]
    assert "telegram" in ids
    tg = next(c for c in candidates if c.manifest.id == "telegram")
    assert tg.manifest.kind == "channel"
    assert tg.manifest.entry == "src"
