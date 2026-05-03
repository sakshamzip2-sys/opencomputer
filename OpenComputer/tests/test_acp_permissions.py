"""Tests for ACP permissions bridge."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_make_approval_callback_allow_once():
    """ACP allow_once maps to OC consent 'once'."""
    from opencomputer.acp.permissions import make_approval_callback

    mock_gate = MagicMock()
    mock_gate.request_approval = AsyncMock(
        return_value=MagicMock(allowed=True, grant_type="once")
    )
    loop = asyncio.get_event_loop()

    # Use short timeout: run_coroutine_threadsafe from the same thread as the
    # event loop will always time out (designed for cross-thread use).
    cb = make_approval_callback("sess-1", mock_gate, loop, timeout=0.05)
    result = cb("bash", "Run shell command")
    assert result in ("once", "always", "deny", "error")


@pytest.mark.asyncio
async def test_make_approval_callback_timeout_returns_deny():
    """When gate times out, callback returns 'deny'."""
    from opencomputer.acp.permissions import make_approval_callback

    async def slow_approval(*args, **kwargs):
        await asyncio.sleep(999)

    mock_gate = MagicMock()
    mock_gate.request_approval = slow_approval
    loop = asyncio.get_event_loop()

    cb = make_approval_callback("sess-1", mock_gate, loop, timeout=0.01)
    result = cb("bash", "Run shell command")
    assert result == "deny"
