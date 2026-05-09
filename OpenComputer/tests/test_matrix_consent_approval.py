"""Hermes parity: matrix consent-gate approval via 4-emoji reaction surface."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from plugin_sdk.core import SendResult


def _make_adapter():
    from extensions.matrix.adapter import MatrixAdapter

    return MatrixAdapter({
        "homeserver": "https://matrix.example.com",
        "access_token": "test-token",
    })


@pytest.mark.asyncio
async def test_send_approval_request_seeds_token_and_self_reactions():
    a = _make_adapter()
    a.send = AsyncMock(return_value=SendResult(
        success=True, message_id="$evt-1",
    ))
    a.send_reaction = AsyncMock(return_value=SendResult(success=True))

    result = await a.send_approval_request(
        chat_id="!room:matrix.example.com",
        prompt_text="Allow execute_code.run? [y/N/session/always]",
        request_token="tok123",
    )

    assert result.success is True
    # Token is registered for inbound resolution.
    assert a._consent_approval_targets["$evt-1"] == "tok123"
    # 4 self-reactions seeded.
    assert a.send_reaction.await_count == 4
    seeded_emojis = {c.args[2] for c in a.send_reaction.await_args_list}
    assert seeded_emojis == {"✅", "🕒", "🔒", "❌"}


@pytest.mark.asyncio
async def test_inbound_reaction_dispatches_to_approval_callback():
    a = _make_adapter()
    a._user_id = "@bot:matrix.example.com"
    a._consent_approval_targets["$prompt-1"] = "tok-a"

    callback = AsyncMock()
    a.set_approval_callback(callback)

    sync_payload = {
        "rooms": {
            "join": {
                "!room:matrix.example.com": {
                    "timeline": {
                        "events": [
                            {
                                "type": "m.reaction",
                                "sender": "@user:matrix.example.com",
                                "content": {
                                    "m.relates_to": {
                                        "rel_type": "m.annotation",
                                        "event_id": "$prompt-1",
                                        "key": "🕒",
                                    },
                                },
                            },
                        ],
                    },
                },
            },
        },
    }

    a._handle_sync_response(sync_payload)
    # The callback runs in an asyncio task — yield once so it executes.
    await asyncio.sleep(0)

    callback.assert_awaited_once_with("session", "tok-a")
    # Target consumed — duplicate reactions can't double-resolve.
    assert "$prompt-1" not in a._consent_approval_targets


@pytest.mark.asyncio
async def test_unknown_emoji_does_not_trigger_callback():
    a = _make_adapter()
    a._user_id = "@bot:matrix.example.com"
    a._consent_approval_targets["$prompt-1"] = "tok-b"
    callback = AsyncMock()
    a.set_approval_callback(callback)

    sync_payload = {
        "rooms": {
            "join": {
                "!room:matrix.example.com": {
                    "timeline": {
                        "events": [
                            {
                                "type": "m.reaction",
                                "sender": "@user:matrix.example.com",
                                "content": {
                                    "m.relates_to": {
                                        "rel_type": "m.annotation",
                                        "event_id": "$prompt-1",
                                        "key": "🌮",
                                    },
                                },
                            },
                        ],
                    },
                },
            },
        },
    }

    a._handle_sync_response(sync_payload)
    await asyncio.sleep(0)
    callback.assert_not_awaited()
    # Target survives — user can react again with a known emoji.
    assert a._consent_approval_targets["$prompt-1"] == "tok-b"


@pytest.mark.asyncio
async def test_self_reaction_is_ignored():
    """The bot's own seed reactions must not fire the callback."""
    a = _make_adapter()
    a._user_id = "@bot:matrix.example.com"
    a._consent_approval_targets["$prompt-1"] = "tok-c"
    callback = AsyncMock()
    a.set_approval_callback(callback)

    sync_payload = {
        "rooms": {
            "join": {
                "!room:matrix.example.com": {
                    "timeline": {
                        "events": [
                            {
                                "type": "m.reaction",
                                "sender": "@bot:matrix.example.com",  # self
                                "content": {
                                    "m.relates_to": {
                                        "rel_type": "m.annotation",
                                        "event_id": "$prompt-1",
                                        "key": "✅",
                                    },
                                },
                            },
                        ],
                    },
                },
            },
        },
    }

    a._handle_sync_response(sync_payload)
    await asyncio.sleep(0)
    callback.assert_not_awaited()
