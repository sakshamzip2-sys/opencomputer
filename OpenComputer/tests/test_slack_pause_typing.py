"""Slack pause-typing-during-approval tests (PR 4.6).

When the agent's processing is in flight we surface a "Thinking…"
status via Slack's ``assistant.threads.setStatus``. When ConsentGate
prompts the user for approval (or any other inbound interaction
mid-run), the dispatch code calls ``pause_typing_status`` so the
indicator doesn't lie about agent state. Once the prompt resolves,
``resume_typing_status`` puts the indicator back.

Lifecycle hooks tested here:

1. ``on_processing_start`` posts ``Thinking…``.
2. ``on_processing_complete`` clears the status (regardless of
   outcome).
3. ``pause_typing_status`` clears; ``resume_typing_status`` restores.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.slack.adapter import SlackAdapter
from plugin_sdk.core import ProcessingOutcome


def _make_adapter_with_mock_client() -> tuple[SlackAdapter, AsyncMock]:
    a = SlackAdapter({"bot_token": "xoxb-test"})
    client = AsyncMock()
    # Default: setStatus returns ok=True so we exercise the success path.
    ok_response = MagicMock()
    ok_response.json.return_value = {"ok": True}
    client.post = AsyncMock(return_value=ok_response)
    a._client = client
    return a, client


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestProcessingHooks:
    @pytest.mark.asyncio
    async def test_on_processing_start_sets_thinking(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.on_processing_start("C123", "1700000000.000100")
        client.post.assert_awaited_once()
        kwargs = client.post.call_args
        url = kwargs.args[0] if kwargs.args else kwargs.kwargs.get("url", "")
        assert "assistant.threads.setStatus" in url
        sent = kwargs.kwargs["json"]
        assert sent["channel_id"] == "C123"
        assert sent["status"] == "Thinking…"
        assert sent["thread_ts"] == "1700000000.000100"

    @pytest.mark.asyncio
    async def test_on_processing_complete_clears_status_on_success(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.on_processing_complete(
            "C123", "1700000000.000100", ProcessingOutcome.SUCCESS
        )
        client.post.assert_awaited_once()
        sent = client.post.call_args.kwargs["json"]
        assert sent["status"] == ""
        assert sent["channel_id"] == "C123"
        assert sent["thread_ts"] == "1700000000.000100"

    @pytest.mark.asyncio
    async def test_on_processing_complete_clears_on_failure(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.on_processing_complete(
            "C123", "ts1", ProcessingOutcome.FAILURE
        )
        # Same clear behaviour regardless of outcome — status is binary.
        sent = client.post.call_args.kwargs["json"]
        assert sent["status"] == ""

    @pytest.mark.asyncio
    async def test_lifecycle_hook_does_not_double_react(self) -> None:
        """The Slack override replaces the base 👀-reaction. We assert
        no reaction calls are made (they'd hit chat.postMessage etc)."""
        a, client = _make_adapter_with_mock_client()
        await a.on_processing_start("C", "ts")
        # Exactly one POST — to assistant.threads.setStatus, NOT
        # reactions.add or chat.postMessage.
        assert client.post.await_count == 1
        url = client.post.call_args.args[0]
        assert "reactions.add" not in url
        assert "chat.postMessage" not in url


# ---------------------------------------------------------------------------
# pause_typing_status / resume_typing_status
# ---------------------------------------------------------------------------


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_clears_status(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.pause_typing_status("C123", thread_ts="ts1")
        sent = client.post.call_args.kwargs["json"]
        assert sent["status"] == ""
        assert sent["channel_id"] == "C123"
        assert sent["thread_ts"] == "ts1"

    @pytest.mark.asyncio
    async def test_resume_restores_thinking_default(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.resume_typing_status("C123", thread_ts="ts1")
        sent = client.post.call_args.kwargs["json"]
        assert sent["status"] == "Thinking…"

    @pytest.mark.asyncio
    async def test_resume_with_custom_status(self) -> None:
        a, client = _make_adapter_with_mock_client()
        await a.resume_typing_status(
            "C123", thread_ts="ts1", status="Reading Confluence…"
        )
        sent = client.post.call_args.kwargs["json"]
        assert sent["status"] == "Reading Confluence…"

    @pytest.mark.asyncio
    async def test_pause_resume_full_cycle(self) -> None:
        """Simulate ConsentGate prompt: start → pause → resume → complete."""
        a, client = _make_adapter_with_mock_client()
        await a.on_processing_start("C", "ts")
        await a.pause_typing_status("C", thread_ts="ts")
        await a.resume_typing_status("C", thread_ts="ts")
        await a.on_processing_complete(
            "C", "ts", ProcessingOutcome.SUCCESS
        )
        statuses = [
            call.kwargs["json"]["status"] for call in client.post.call_args_list
        ]
        assert statuses == ["Thinking…", "", "Thinking…", ""]

    @pytest.mark.asyncio
    async def test_no_client_no_op(self) -> None:
        """When the adapter isn't connected, status calls are no-ops."""
        a = SlackAdapter({"bot_token": "xoxb-test"})
        # _client is None — should not raise.
        await a.pause_typing_status("C", thread_ts="ts")
        await a.resume_typing_status("C", thread_ts="ts")
        await a.on_processing_start("C", "ts")
        await a.on_processing_complete(
            "C", "ts", ProcessingOutcome.SUCCESS
        )

    @pytest.mark.asyncio
    async def test_setstatus_failure_swallowed(self) -> None:
        """Slack returning ok=False (e.g. 'not_in_channel' for a regular
        channel) must NOT raise — typing status is decoration."""
        a = SlackAdapter({"bot_token": "xoxb-test"})
        client = AsyncMock()
        bad_response = MagicMock()
        bad_response.json.return_value = {"ok": False, "error": "not_in_channel"}
        client.post = AsyncMock(return_value=bad_response)
        a._client = client
        # Should not raise.
        await a.on_processing_start("C", "ts")
        # Call still happened (best-effort).
        client.post.assert_awaited_once()
