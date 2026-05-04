"""Tests for MatrixAdapter inbound /sync polling (Wave 6.E.3).

We don't run a real Matrix homeserver — instead the tests construct a
MatrixAdapter, plant a fixture sync response, and verify that the
``_handle_sync_response`` parser correctly drives the approval queue.

Covers:
- Reactions on registered events resolve the queue
- Reactions FROM our own user_id are ignored (don't self-resolve)
- Reactions on UNregistered events are silently dropped
- The ``inbound_sync`` config flag gates whether _sync_task starts
"""

from __future__ import annotations

import asyncio

import pytest

from extensions.matrix.adapter import MatrixAdapter


def _make_adapter(*, inbound: bool = True, user_id: str = "@bot:server") -> MatrixAdapter:
    a = MatrixAdapter({
        "homeserver": "https://example.com",
        "access_token": "token-x",
        "inbound_sync": inbound,
    })
    a._user_id = user_id
    return a


def _sync_response(events: list[dict]) -> dict:
    """Build a minimal sync response with the given timeline events."""
    return {
        "next_batch": "batch-2",
        "rooms": {
            "join": {
                "!room1:server": {"timeline": {"events": events}},
            },
        },
    }


def _reaction_event(*, sender: str, target_event: str, key: str) -> dict:
    return {
        "type": "m.reaction",
        "sender": sender,
        "content": {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target_event,
                "key": key,
            },
        },
    }


# ---- handler ----


@pytest.mark.asyncio
async def test_reaction_on_registered_event_resolves_future():
    adapter = _make_adapter()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    adapter.approval_queue.register("$msg-1", future=fut, timeout=10.0)

    response = _sync_response([
        _reaction_event(sender="@user:server", target_event="$msg-1", key="✅"),
    ])
    adapter._handle_sync_response(response)

    assert await asyncio.wait_for(fut, timeout=0.5) is True


@pytest.mark.asyncio
async def test_reaction_from_self_is_ignored():
    """The bot's own ✅ on its own message must not self-resolve approvals."""
    adapter = _make_adapter(user_id="@bot:server")
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    adapter.approval_queue.register("$msg-2", future=fut, timeout=10.0)

    response = _sync_response([
        _reaction_event(sender="@bot:server", target_event="$msg-2", key="✅"),
    ])
    adapter._handle_sync_response(response)
    assert not fut.done()


@pytest.mark.asyncio
async def test_reaction_on_unknown_event_is_noop():
    adapter = _make_adapter()
    response = _sync_response([
        _reaction_event(sender="@u:s", target_event="$never-registered", key="✅"),
    ])
    # Must not raise — security property A10
    adapter._handle_sync_response(response)


@pytest.mark.asyncio
async def test_handler_skips_message_events():
    """``m.room.message`` events should be ignored — we only care about reactions."""
    adapter = _make_adapter()
    response = _sync_response([
        {"type": "m.room.message", "content": {"body": "hi"}},
    ])
    adapter._handle_sync_response(response)  # no exception


@pytest.mark.asyncio
async def test_handler_tolerates_malformed_response():
    adapter = _make_adapter()
    # Missing rooms.join entirely — must not crash
    adapter._handle_sync_response({"next_batch": "x"})
    # rooms.join is wrong type
    adapter._handle_sync_response({"rooms": {"join": "not-a-dict"}})
    # timeline missing
    adapter._handle_sync_response({"rooms": {"join": {"!r:s": {}}}})


# ---- config gate ----


def test_inbound_sync_off_by_default():
    adapter = MatrixAdapter({
        "homeserver": "https://example.com",
        "access_token": "x",
    })
    assert adapter._inbound_enabled is False


def test_inbound_sync_opt_in_via_config():
    adapter = MatrixAdapter({
        "homeserver": "https://example.com",
        "access_token": "x",
        "inbound_sync": True,
    })
    assert adapter._inbound_enabled is True
