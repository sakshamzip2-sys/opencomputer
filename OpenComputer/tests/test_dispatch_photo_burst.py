"""Tests for photo-burst merging in Dispatch (Hermes PR 2 Task 2.6 + amendment §A.1).

The original plan's implementation (``pending._replace(...)`` on a
frozen+slots dataclass; return contract changed to None) was BROKEN.
This file exercises the amendment §A.1 cancel-on-text + future-chained-
joiners design instead.

Critical invariants:

* ``Dispatch.handle_message`` MUST preserve its ``str | None`` return
  contract. 7 adapters (slack/mattermost/email/signal/sms/imessage/
  webhook) await the return and call ``self.send(chat_id, response)``.
* When text arrives mid-burst, the pending burst dispatch is CANCELLED
  and its attachments are merged into the text event. Single agent run.
* When two pure-photo events arrive within the burst window, they
  collapse into ONE agent run with merged attachments.
* Joiners (subsequent pure-photo events) await the same future the
  original event resolves so every caller sees the same answer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import MessageEvent, Platform


def _conversation_result(text: str):
    """Build a real-shaped ConversationResult for AsyncMock return."""
    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    final = Message(role="assistant", content=text)
    return ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )


# ─── Return-contract regression (§A.1 critical) ──────────────────────


@pytest.mark.asyncio
async def test_handle_message_default_text_only_returns_assistant_text() -> None:
    """REGRESSION (§A.1): simple text event preserves str-or-None return contract.

    7 adapters depend on this. If handle_message ever returns None for
    a happy-path text event, slack/mattermost/email/signal/sms/imessage/
    webhook silently stop replying.
    """
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(
        return_value=_conversation_result("hello back")
    )
    d = Dispatch(loop_mock)

    event = MessageEvent(
        platform=Platform.SMS,
        chat_id="+1",
        user_id="u",
        text="hi",
        attachments=[],
        timestamp=1000.0,
        metadata={},
    )
    result = await d.handle_message(event)
    assert result == "hello back"


@pytest.mark.asyncio
async def test_handle_message_empty_text_no_attachments_returns_none() -> None:
    """Existing behaviour: blank text + no attachments → None, no agent run."""
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    d = Dispatch(loop_mock)
    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="c",
        user_id="u",
        text="   ",
        timestamp=0.0,
    )
    result = await d.handle_message(event)
    assert result is None
    loop_mock.run_conversation.assert_not_called()


# ─── Photo-burst merge ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_photo_burst_merges_attachments_within_window() -> None:
    """3 pure-photo events for the same chat in <0.3s → ONE agent run."""
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(
        return_value=_conversation_result("response")
    )

    d = Dispatch(loop_mock)
    d._burst_window_seconds = 0.3

    def base(i: int) -> MessageEvent:
        return MessageEvent(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            user_id="u-1",
            text="",
            attachments=[f"telegram:f{i}"],
            timestamp=1000.0 + i * 0.05,
            metadata={"message_id": str(i)},
        )

    results = await asyncio.gather(
        d.handle_message(base(1)),
        d.handle_message(base(2)),
        d.handle_message(base(3)),
    )
    # Every caller sees the same answer.
    assert all(r == "response" for r in results)
    # Only ONE agent run.
    assert loop_mock.run_conversation.call_count == 1
    args, kwargs = loop_mock.run_conversation.call_args
    user_msg = kwargs.get("user_message")
    assert user_msg is not None  # text path used


@pytest.mark.asyncio
async def test_photo_burst_separate_sessions_not_merged() -> None:
    """Different chat_ids → separate dispatches, no merge."""
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(
        return_value=_conversation_result("ok")
    )
    d = Dispatch(loop_mock)
    d._burst_window_seconds = 0.3

    e1 = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="A", user_id="u",
        text="", attachments=["t:1"], timestamp=1000.0,
        metadata={"message_id": "1"},
    )
    e2 = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="B", user_id="u",
        text="", attachments=["t:2"], timestamp=1000.0,
        metadata={"message_id": "2"},
    )
    await asyncio.gather(d.handle_message(e1), d.handle_message(e2))
    assert loop_mock.run_conversation.call_count == 2


# ─── Text cancels pending burst (§A.1 design) ────────────────────────


@pytest.mark.asyncio
async def test_text_arrival_cancels_pending_burst_and_merges() -> None:
    """When text arrives mid-burst, cancel pending dispatch + merge attachments.

    Per amendment §A.1 design — the photo's attachments are absorbed
    into the text event and a single agent run handles both. Joiners
    (the original photo handler) get the same answer via shared future.
    """
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(
        return_value=_conversation_result("combined response")
    )
    d = Dispatch(loop_mock)
    d._burst_window_seconds = 0.5

    photo = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="A",
        user_id="u",
        text="",
        attachments=["t:1"],
        timestamp=1000.0,
        metadata={"message_id": "1"},
    )
    text = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="A",
        user_id="u",
        text="what's this?",
        attachments=[],
        timestamp=1001.0,
        metadata={"message_id": "2"},
    )
    photo_task = asyncio.create_task(d.handle_message(photo))
    await asyncio.sleep(0.05)  # photo is pending in the burst window
    result = await d.handle_message(text)
    photo_result = await photo_task
    assert result == "combined response"
    assert photo_result == "combined response"  # joiner gets same answer
    # Single agent run with text + merged attachments.
    assert loop_mock.run_conversation.call_count == 1


# ─── Future-chained joiner ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_joiner_future_propagates_exception() -> None:
    """If the burst dispatch raises, joiners see the exception too — never silent.

    But Dispatch already converts agent exceptions into a friendly
    error string, so the OUTPUT here is a string (not raised). The key
    invariant is: joiners and the original handler return the SAME
    string.
    """
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(side_effect=RuntimeError("kaboom"))
    d = Dispatch(loop_mock)
    d._burst_window_seconds = 0.3

    e1 = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="A", user_id="u",
        text="", attachments=["t:1"], timestamp=1000.0,
        metadata={"message_id": "1"},
    )
    e2 = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="A", user_id="u",
        text="", attachments=["t:2"], timestamp=1000.05,
        metadata={"message_id": "2"},
    )
    r1, r2 = await asyncio.gather(d.handle_message(e1), d.handle_message(e2))
    # Both callers see the same friendly-error string — Dispatch's
    # catch-all converts the RuntimeError to a one-liner.
    assert r1 == r2
    assert isinstance(r1, str)


@pytest.mark.asyncio
async def test_photo_only_returns_assistant_text() -> None:
    """A single pure-photo event still returns the assistant text via the burst path."""
    from opencomputer.gateway.dispatch import Dispatch

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(
        return_value=_conversation_result("photo seen")
    )
    d = Dispatch(loop_mock)
    d._burst_window_seconds = 0.05

    photo = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="X",
        user_id="u",
        text="",
        attachments=["t:42"],
        timestamp=1000.0,
        metadata={"message_id": "42"},
    )
    result = await d.handle_message(photo)
    assert result == "photo seen"
    assert loop_mock.run_conversation.call_count == 1


# ─── Burst window configurable ───────────────────────────────────────


@pytest.mark.asyncio
async def test_burst_window_configurable_via_init() -> None:
    """Per amendment §B.5 — burst window comes from gateway config."""
    from opencomputer.gateway.dispatch import Dispatch

    d = Dispatch(MagicMock(), config={"photo_burst_window": 0.123})
    assert d._burst_window_seconds == 0.123


@pytest.mark.asyncio
async def test_burst_window_default_is_point_eight() -> None:
    from opencomputer.gateway.dispatch import Dispatch

    d = Dispatch(MagicMock())
    assert d._burst_window_seconds == 0.8
