"""Tests for Telegram network-error fatal cap (PR 3a.4).

After 10 consecutive transient network errors during ``getUpdates``
(backoff schedule [5, 10, 20, 40, 60, 60, 60, 60, 60, 60]), the
adapter sets a fatal-retryable error and breaks the poll loop. The
gateway supervisor reads ``has_fatal_error()`` and decides whether
to reconnect.

Successful poll resets the counter to zero.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from extensions.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test"})
    a._client = AsyncMock()
    a._bot_id = 42
    return a


def _ok_empty_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"ok": True, "result": []}
    return r


@pytest.mark.asyncio
async def test_eleventh_network_error_triggers_fatal_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: 10 transient errors are absorbed; the 11th sets fatal-retryable."""
    a = _make_adapter()

    sleeps: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    a._client.get = AsyncMock(side_effect=httpx.ConnectError("transport down"))

    await a._poll_forever()

    assert a.has_fatal_error()
    assert a._fatal_error_code == "telegram-network"
    assert a._fatal_error_retryable is True
    assert "transport down" in (a._fatal_error_message or "")

    # Ten sleeps preceded the fatal break.
    assert sleeps == [5, 10, 20, 40, 60, 60, 60, 60, 60, 60]
    assert a._client.get.await_count == 11


@pytest.mark.asyncio
async def test_network_counter_resets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful poll between network errors resets the counter."""
    a = _make_adapter()

    sleeps: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    call_log: list[str] = []

    def _next(*args: object, **kwargs: object) -> object:
        idx = len(call_log)
        call_log.append("c")
        if idx in (0, 1):
            raise httpx.ConnectError("flap")
        if idx == 2:
            return _ok_empty_response()
        # Stop the loop afterwards.
        a._stop_event.set()
        return _ok_empty_response()

    a._client.get = AsyncMock(side_effect=_next)
    await a._poll_forever()

    assert not a.has_fatal_error()
    # First two ConnectErrors slept 5, 10 (per the schedule indices 0,1).
    # Then success reset the counter; the next call exited cleanly.
    assert sleeps[:2] == [5, 10]


@pytest.mark.asyncio
async def test_ten_errors_no_fatal_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 10 errors followed by success: no fatal, counter resets."""
    a = _make_adapter()

    async def _capture_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    call_log: list[str] = []

    def _next(*args: object, **kwargs: object) -> object:
        idx = len(call_log)
        call_log.append("c")
        if idx < 10:
            raise httpx.ConnectError(f"flap-{idx}")
        a._stop_event.set()
        return _ok_empty_response()

    a._client.get = AsyncMock(side_effect=_next)
    await a._poll_forever()
    assert not a.has_fatal_error()
