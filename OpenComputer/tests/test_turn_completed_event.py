"""P0-6: TurnCompletedEvent published on bus after each turn write.

Decouples Phase 0 capture from any specific provider. Subscribers
(Honcho extension, analytics, custom reactors) consume the event
without dispatch importing them — preserves SDK boundary.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.agent.turn_outcome_recorder import TurnSignals
from opencomputer.gateway.dispatch import _record_turn_outcome_async
from opencomputer.ingestion.bus import get_default_bus
from plugin_sdk.ingestion import TurnCompletedEvent


@pytest.mark.asyncio
async def test_event_published_after_db_write(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))

    received: list[TurnCompletedEvent] = []

    bus = get_default_bus()

    def handler(evt):
        received.append(evt)

    sub = bus.subscribe("turn_completed", handler)

    sig = TurnSignals(
        session_id="sess_1",
        turn_index=2,
        tool_call_count=3,
        tool_success_count=3,
        vibe_after="curious",
        affirmation_present=True,
    )
    await _record_turn_outcome_async(db, sig)

    assert len(received) == 1
    evt = received[0]
    assert evt.event_type == "turn_completed"
    assert evt.session_id == "sess_1"
    assert evt.turn_index == 2
    assert evt.signals["tool_success_count"] == 3
    assert evt.signals["vibe_after"] == "curious"
    assert evt.signals["affirmation_present"] is True

    # Cleanup the subscription so it doesn't leak into the next test
    # (default bus is module-global)
    sub.unsubscribe()


@pytest.mark.asyncio
async def test_event_not_published_when_db_write_fails(tmp_path):
    """If the DB write raises, no event should fire — subscribers should
    only see durable outcomes."""
    from unittest.mock import MagicMock

    db = MagicMock()
    db._connect.side_effect = RuntimeError("disk full")

    received: list[TurnCompletedEvent] = []
    bus = get_default_bus()

    def handler(evt):
        received.append(evt)

    sub = bus.subscribe("turn_completed", handler)

    sig = TurnSignals(session_id="sess_1", turn_index=0)
    await _record_turn_outcome_async(db, sig)

    assert received == []  # DB write failed → no event

    sub.unsubscribe()


@pytest.mark.asyncio
async def test_event_payload_includes_all_phase_0_signals(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))

    received: list[TurnCompletedEvent] = []
    bus = get_default_bus()
    sub = bus.subscribe("turn_completed", lambda e: received.append(e))

    sig = TurnSignals(
        session_id="sess_1",
        turn_index=0,
        tool_call_count=1,
        tool_success_count=1,
        tool_error_count=0,
        tool_blocked_count=0,
        self_cancel_count=0,
        retry_count=0,
        vibe_before="calm",
        vibe_after="curious",
        reply_latency_s=4.2,
        affirmation_present=True,
        correction_present=False,
        conversation_abandoned=False,
        duration_s=8.7,
    )
    await _record_turn_outcome_async(db, sig)

    assert len(received) == 1
    expected_keys = {
        "tool_call_count", "tool_success_count", "tool_error_count",
        "tool_blocked_count", "self_cancel_count", "retry_count",
        "vibe_before", "vibe_after", "reply_latency_s",
        "affirmation_present", "correction_present",
        "conversation_abandoned", "duration_s",
    }
    assert expected_keys.issubset(received[0].signals.keys())
    sub.unsubscribe()
