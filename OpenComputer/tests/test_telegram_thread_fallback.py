"""Telegram thread-not-found retry tests (PR 4.2).

When the bot is told to post into a forum topic that has been deleted /
archived / the bot has been booted from, Telegram returns 400 with
"message thread not found". The adapter should:

1. Retry the same call ONCE without ``message_thread_id`` so the
   message lands in the chat's General topic instead of vanishing.
2. Log a WARN. Other 400s (including parse-error variants) must NOT
   trigger this retry.
3. ``_GENERAL_TOPIC_THREAD_ID`` ("1") is the General topic — when the
   caller passes 1 we OMIT message_thread_id from the outbound payload
   because the API rejects explicit thread_id=1 with the same error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.telegram.adapter import (
    _GENERAL_TOPIC_THREAD_ID,
    TelegramAdapter,
    _is_thread_not_found_error,
)


def _make_adapter() -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test"})
    a._client = AsyncMock()
    a._bot_id = 42
    return a


def _resp(status: int, text: str = "", json_body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body or {"ok": status == 200}
    return r


# ---------------------------------------------------------------------------
# _is_thread_not_found_error helper
# ---------------------------------------------------------------------------


class TestIsThreadNotFoundError:
    def test_400_with_marker_matches(self) -> None:
        r = _resp(400, text='{"description":"Bad Request: message thread not found"}')
        assert _is_thread_not_found_error(r) is True

    def test_case_insensitive(self) -> None:
        r = _resp(400, text="MESSAGE THREAD NOT FOUND")
        assert _is_thread_not_found_error(r) is True

    def test_other_400_does_not_match(self) -> None:
        r = _resp(400, text='{"description":"Bad Request: chat not found"}')
        assert _is_thread_not_found_error(r) is False

    def test_non_400_does_not_match(self) -> None:
        r = _resp(500, text="message thread not found")
        assert _is_thread_not_found_error(r) is False

    def test_none_does_not_match(self) -> None:
        assert _is_thread_not_found_error(None) is False


# ---------------------------------------------------------------------------
# send() integration: retry without thread on thread-not-found
# ---------------------------------------------------------------------------


class TestThreadNotFoundRetry:
    @pytest.mark.asyncio
    async def test_thread_not_found_retries_without_thread(self) -> None:
        a = _make_adapter()
        seen_payloads: list[dict] = []

        async def _fake_post(url: str, **kwargs):
            seen_payloads.append(kwargs.get("json", {}))
            if "message_thread_id" in kwargs.get("json", {}):
                # First attempt: thread set → return thread-not-found
                return _resp(
                    400,
                    text='{"description":"Bad Request: message thread not found"}',
                )
            # Second attempt: no thread → success
            return _resp(200, json_body={"ok": True, "result": {"message_id": 1}})

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send("12345", "hello", message_thread_id=42)
        assert result.success is True
        assert len(seen_payloads) == 2
        assert seen_payloads[0]["message_thread_id"] == 42
        assert "message_thread_id" not in seen_payloads[1]

    @pytest.mark.asyncio
    async def test_other_400_not_retried(self) -> None:
        """A non-thread-not-found 400 must NOT trigger the thread retry."""
        a = _make_adapter()
        call_count = 0

        async def _fake_post(url: str, **kwargs):
            nonlocal call_count
            call_count += 1
            return _resp(
                400, text='{"description":"Bad Request: chat not found"}'
            )

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send("12345", "hello", message_thread_id=42)
        assert result.success is False
        # One call only — no thread-fallback retry; parse-error retry
        # also skipped (different marker).
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_general_topic_id_omitted(self) -> None:
        """thread_id == "1" (General topic) is omitted from the payload."""
        a = _make_adapter()
        seen_payloads: list[dict] = []

        async def _fake_post(url: str, **kwargs):
            seen_payloads.append(kwargs.get("json", {}))
            return _resp(200, json_body={"ok": True, "result": {"message_id": 1}})

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send(
            "12345", "hello", message_thread_id=_GENERAL_TOPIC_THREAD_ID
        )
        assert result.success is True
        # Single call, no thread id on the wire.
        assert len(seen_payloads) == 1
        assert "message_thread_id" not in seen_payloads[0]

    @pytest.mark.asyncio
    async def test_general_topic_int_1_also_omitted(self) -> None:
        """Integer 1 is the same General topic — also omitted."""
        a = _make_adapter()
        seen_payloads: list[dict] = []

        async def _fake_post(url: str, **kwargs):
            seen_payloads.append(kwargs.get("json", {}))
            return _resp(200, json_body={"ok": True, "result": {"message_id": 1}})

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send("12345", "hello", message_thread_id=1)
        assert result.success is True
        assert len(seen_payloads) == 1
        assert "message_thread_id" not in seen_payloads[0]

    @pytest.mark.asyncio
    async def test_no_thread_id_no_retry(self) -> None:
        """When no thread_id was passed, a thread-not-found error
        (which can't realistically happen but defend anyway) does
        NOT trigger an infinite retry loop."""
        a = _make_adapter()
        call_count = 0

        async def _fake_post(url: str, **kwargs):
            nonlocal call_count
            call_count += 1
            return _resp(
                400,
                text='{"description":"Bad Request: message thread not found"}',
            )

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send("12345", "hello")
        assert result.success is False
        # Exactly one call — no thread-fallback retry triggered.
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_thread_id_preserved_on_success(self) -> None:
        a = _make_adapter()
        seen_payloads: list[dict] = []

        async def _fake_post(url: str, **kwargs):
            seen_payloads.append(kwargs.get("json", {}))
            return _resp(200, json_body={"ok": True, "result": {"message_id": 1}})

        a._post_with_retry = AsyncMock(side_effect=_fake_post)

        result = await a.send("12345", "hello", message_thread_id=42)
        assert result.success is True
        assert len(seen_payloads) == 1
        assert seen_payloads[0]["message_thread_id"] == 42
