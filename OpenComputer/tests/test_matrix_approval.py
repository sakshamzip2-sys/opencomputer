"""Tests for the Matrix reaction-based approval primitive (Wave 6.E.3).

Covers:
- ``ApprovalQueue.register`` + ``on_reaction`` resolves the future
- ``✅`` resolves True; ``❌`` resolves False
- Wrong-event-id reactions are no-ops
- Random emoji on our message keeps the future pending
- Expired entries reap to False
- ``cancel_all`` cancels (not resolves) pending futures
- ``request_approval`` end-to-end against a mocked adapter
- ``request_approval`` with no approval_queue returns False
- ``request_approval`` send-failure returns False
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.matrix.approval import (
    DEFAULT_ALLOW_EMOJI,
    DEFAULT_DENY_EMOJI,
    ApprovalQueue,
    request_approval,
)

# ---- ApprovalQueue ----


@pytest.mark.asyncio
async def test_allow_emoji_resolves_true():
    q = ApprovalQueue()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    q.register("$evt1", future=fut, timeout=5.0)
    assert q.on_reaction("$evt1", DEFAULT_ALLOW_EMOJI) is True
    assert await asyncio.wait_for(fut, timeout=0.5) is True


@pytest.mark.asyncio
async def test_deny_emoji_resolves_false():
    q = ApprovalQueue()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    q.register("$evt2", future=fut, timeout=5.0)
    assert q.on_reaction("$evt2", DEFAULT_DENY_EMOJI) is True
    assert await asyncio.wait_for(fut, timeout=0.5) is False


@pytest.mark.asyncio
async def test_unknown_event_id_is_noop():
    q = ApprovalQueue()
    assert q.on_reaction("$nope", DEFAULT_ALLOW_EMOJI) is False


@pytest.mark.asyncio
async def test_random_emoji_keeps_future_pending():
    """A 🎉 on our message shouldn't resolve True — only ✅/❌."""
    q = ApprovalQueue()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    q.register("$evt3", future=fut, timeout=5.0)
    # First reaction: random emoji → future stays pending
    resolved = q.on_reaction("$evt3", "🎉")
    assert resolved is False
    assert not fut.done()
    # Subsequent ✅ reaction: resolves
    assert q.on_reaction("$evt3", DEFAULT_ALLOW_EMOJI) is True
    assert await asyncio.wait_for(fut, timeout=0.5) is True


@pytest.mark.asyncio
async def test_reap_expired_resolves_to_false():
    q = ApprovalQueue()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    q.register("$evt4", future=fut, timeout=0.0)  # already expired
    n = q.reap_expired()
    assert n == 1
    assert await asyncio.wait_for(fut, timeout=0.5) is False


def test_cancel_all_cancels_pending():
    loop = asyncio.new_event_loop()
    try:
        q = ApprovalQueue()
        fut = loop.create_future()
        q.register("$x", future=fut, timeout=10.0)
        assert len(q) == 1
        q.cancel_all()
        assert len(q) == 0
        assert fut.cancelled()
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_already_resolved_future_not_overwritten():
    q = ApprovalQueue()
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    fut.set_result(True)  # Resolved out-of-band
    q.register("$evt5", future=fut, timeout=5.0)
    # Reaction comes in but future is already done — must not raise
    assert q.on_reaction("$evt5", DEFAULT_DENY_EMOJI) is False


# ---- request_approval ----


@pytest.mark.asyncio
async def test_request_approval_returns_false_when_no_queue():
    adapter = MagicMock()
    adapter.approval_queue = None
    out = await request_approval(adapter, "#room", "do thing?", timeout=1.0)
    assert out is False


@pytest.mark.asyncio
async def test_request_approval_send_failure_returns_false():
    adapter = MagicMock()
    adapter.approval_queue = ApprovalQueue()
    adapter.send = AsyncMock(side_effect=RuntimeError("network down"))
    out = await request_approval(adapter, "#room", "do?", timeout=1.0)
    assert out is False


@pytest.mark.asyncio
async def test_request_approval_resolves_via_reaction():
    """End-to-end: post → register → reaction arrives → future resolves."""
    adapter = MagicMock()
    adapter.approval_queue = ApprovalQueue()
    fake_send_result = MagicMock(spec=["platform_message_id"])
    fake_send_result.platform_message_id = "$event123"
    adapter.send = AsyncMock(return_value=fake_send_result)

    async def _fire_reaction_after_a_moment():
        await asyncio.sleep(0.05)
        adapter.approval_queue.on_reaction("$event123", DEFAULT_ALLOW_EMOJI)

    asyncio.create_task(_fire_reaction_after_a_moment())
    out = await request_approval(adapter, "!room:server", "go?", timeout=2.0)
    assert out is True


@pytest.mark.asyncio
async def test_request_approval_times_out_to_false():
    adapter = MagicMock()
    adapter.approval_queue = ApprovalQueue()
    fake = MagicMock(spec=["platform_message_id"])
    fake.platform_message_id = "$evtz"
    adapter.send = AsyncMock(return_value=fake)
    out = await request_approval(adapter, "!room:s", "go?", timeout=0.1)
    assert out is False
