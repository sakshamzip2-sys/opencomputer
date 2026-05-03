"""P2-13: Telegram extension subscribes to policy_change + policy_reverted."""
from __future__ import annotations

import sys

import pytest

from opencomputer.ingestion.bus import get_default_bus
from plugin_sdk.ingestion import (
    PolicyChangeEvent,
    PolicyRevertedEvent,
)

# Plugin-loader-mode import (telegram is loaded with its own dir on sys.path)
sys.path.insert(0, "extensions/telegram")
from policy_notifier import (  # noqa: E402
    register_policy_notifier,
    register_revert_notifier,
)


@pytest.mark.asyncio
async def test_pending_approval_event_triggers_dm():
    bus = get_default_bus()
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    sub = register_policy_notifier(
        bus=bus, admin_chat_id="123", send_fn=fake_send,
    )

    evt = PolicyChangeEvent(
        source="cron.policy_engine",
        change_id="abcdef12-3456",
        knob_kind="recall_penalty",
        target_id="42",
        status="pending_approval",
        approval_mode="explicit",
        engine_version="MostCitedBelowMedian/1",
        reason="cited 7x mean 0.30",
    )
    await bus.apublish(evt)

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "123"
    assert "abcdef12" in text
    assert "/policy-approve abcdef12" in text
    assert "MostCitedBelowMedian/1" in text

    sub.unsubscribe()


@pytest.mark.asyncio
async def test_active_status_does_not_trigger_dm():
    bus = get_default_bus()
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))

    sub = register_policy_notifier(
        bus=bus, admin_chat_id="123", send_fn=fake_send,
    )

    evt = PolicyChangeEvent(
        source="x", change_id="c1",
        knob_kind="recall_penalty", target_id="1",
        status="active",  # not pending_approval
        approval_mode="auto_ttl", engine_version="X/1", reason="r",
    )
    await bus.apublish(evt)

    assert sent == []
    sub.unsubscribe()


@pytest.mark.asyncio
async def test_reverted_event_triggers_dm():
    bus = get_default_bus()
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))

    sub = register_revert_notifier(
        bus=bus, admin_chat_id="123", send_fn=fake_send,
    )

    evt = PolicyRevertedEvent(
        source="cron.auto_revert",
        change_id="abcdef12",
        knob_kind="recall_penalty",
        target_id="42",
        reverted_reason="statistical: post_mean 0.40 < baseline - 1σ",
    )
    await bus.apublish(evt)

    assert len(sent) == 1
    text = sent[0][1]
    assert "rolled back" in text.lower()
    assert "abcdef12" in text
    assert "statistical" in text.lower()

    sub.unsubscribe()


@pytest.mark.asyncio
async def test_send_failure_does_not_propagate():
    bus = get_default_bus()

    async def boom(chat_id, text):
        raise RuntimeError("Telegram API down")

    sub = register_policy_notifier(
        bus=bus, admin_chat_id="123", send_fn=boom,
    )

    evt = PolicyChangeEvent(
        source="x", change_id="c1",
        knob_kind="recall_penalty", target_id="1",
        status="pending_approval",
        approval_mode="explicit",
        engine_version="X/1", reason="r",
    )
    # Must not raise
    await bus.apublish(evt)
    sub.unsubscribe()
