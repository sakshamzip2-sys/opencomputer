"""Tests for Telegram 409-conflict fatal cap (PR 3a.4).

After ``_MAX_CONSECUTIVE_409S`` (default 3) consecutive 409 Conflict
responses from ``getUpdates``, the adapter sets a fatal-non-retryable
error (the gateway supervisor will log ERROR rather than reconnect —
restarting won't help when another process holds the polling slot).

Reset on successful poll: counters return to zero.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test"})
    a._client = AsyncMock()
    a._bot_id = 42
    a._bot_username = "hermes_bot"
    return a


def _conflict_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 409
    r.text = "Conflict: terminated by other getUpdates request"
    return r


def _ok_empty_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"ok": True, "result": []}
    return r


@pytest.mark.asyncio
async def test_fourth_conflict_triggers_fatal_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: 3 consecutive 409s tolerated; the 4th sets fatal."""
    a = _make_adapter()

    sleeps: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    # Always return 409.
    a._client.get = AsyncMock(return_value=_conflict_response())

    await a._poll_forever()

    # The loop exited (broke) — adapter is now fatally errored.
    assert a.has_fatal_error()
    assert a._fatal_error_code == "telegram-conflict"
    assert a._fatal_error_retryable is False
    assert "another process is polling" in (a._fatal_error_message or "")

    # Three 10-second sleeps preceded the fatal break (the 4th 409 was
    # not slept on — it tripped the cap and broke the loop).
    assert sleeps == [10, 10, 10]
    assert a._client.get.await_count == 4


@pytest.mark.asyncio
async def test_conflict_counter_resets_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 OK between conflicts must reset the counter."""
    a = _make_adapter()

    async def _capture_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    # 2 conflicts, success, then 3 conflicts — should NOT trip fatal
    # (counter resets after success), the 4th-of-the-second-streak
    # would though if we kept going. Stop after 5 calls by setting
    # stop event from inside the side-effect.
    call_log: list[str] = []

    def _next_response(*args: object, **kwargs: object) -> MagicMock:
        idx = len(call_log)
        call_log.append("call")
        if idx in (0, 1):
            return _conflict_response()
        if idx == 2:
            # Success resets counter.
            a._stop_event.set()
            return _ok_empty_response()
        return _conflict_response()

    a._client.get = AsyncMock(side_effect=_next_response)
    await a._poll_forever()

    # Loop exited via _stop_event, NOT fatal.
    assert not a.has_fatal_error()
    assert a._client.get.await_count == 3


@pytest.mark.asyncio
async def test_under_threshold_does_not_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 conflicts then success — adapter stays alive."""
    a = _make_adapter()

    async def _capture_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "extensions.telegram.adapter.asyncio.sleep", _capture_sleep
    )

    call_log: list[str] = []

    def _next_response(*args: object, **kwargs: object) -> MagicMock:
        idx = len(call_log)
        call_log.append("c")
        if idx < 3:
            return _conflict_response()
        a._stop_event.set()
        return _ok_empty_response()

    a._client.get = AsyncMock(side_effect=_next_response)
    await asyncio.wait_for(a._poll_forever(), timeout=2.0)
    assert not a.has_fatal_error()
