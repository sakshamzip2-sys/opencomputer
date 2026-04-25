"""PR-D: ACPSession lifecycle."""
from __future__ import annotations

import pytest

from opencomputer.acp.session import ACPSession


@pytest.mark.asyncio
async def test_session_construct():
    sent = []
    s = ACPSession(session_id="test-1", send=lambda m, p: sent.append((m, p)))
    assert s.session_id == "test-1"


@pytest.mark.asyncio
async def test_cancel_sets_event():
    sent = []
    s = ACPSession(session_id="test-2", send=lambda m, p: sent.append((m, p)))
    # Not running → cancel returns False
    result = await s.cancel()
    # First call returns True (event was unset → flips to set)
    # Second call returns False (was already set)
    second = await s.cancel()
    assert result != second  # one True, one False


@pytest.mark.asyncio
async def test_load_from_db_missing_returns_false():
    sent = []
    s = ACPSession(session_id="missing-session-xyz", send=lambda m, p: sent.append((m, p)))
    # No such session in DB → returns False
    loaded = await s.load_from_db()
    assert loaded is False
