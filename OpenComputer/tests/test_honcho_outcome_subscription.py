"""P0-6b: Honcho extension subscribes to TurnCompletedEvent.

v0 stub: handler logs the event at INFO. Server-side ingestion via a new
Honcho endpoint is a v0.5 deliverable (the upstream Honcho doesn't accept
structured outcome blobs today). What this verifies is the wiring —
that when an outcome event is published, the always-on Honcho provider
DOES receive it.
"""
from __future__ import annotations

import logging

import pytest

from opencomputer.ingestion.bus import get_default_bus, reset_default_bus
from plugin_sdk.ingestion import TurnCompletedEvent


@pytest.fixture(autouse=True)
def _reset_bus():
    """Reset the module-global default bus before EACH test in this file.

    Other tests in the suite leave subscribers attached; cross-test
    leakage causes assertion drift here because we're checking caplog
    output that's tied to subscriber-added log lines."""
    reset_default_bus()
    yield
    reset_default_bus()


@pytest.mark.asyncio
async def test_honcho_subscriber_receives_turn_completed_event(caplog):
    # Import lazily so the test module loads even if Honcho is missing.
    import sys
    sys.path.insert(0, "extensions/memory-honcho")
    from provider import HonchoSelfHostedProvider

    # Set propagating root-logger level BEFORE the subscriber registers,
    # so caplog captures the INFO-level event log regardless of which
    # logger instance the provider module ended up bound to (different
    # qualifying paths produce distinct logger instances in some
    # full-suite import orders).
    caplog.set_level(logging.INFO)

    provider = HonchoSelfHostedProvider()
    bus = get_default_bus()
    sub = provider.subscribe_to_outcome_events(bus)

    evt = TurnCompletedEvent(
        session_id="sess_1",
        source="gateway.dispatch",
        turn_index=3,
        signals={"tool_call_count": 2, "vibe_after": "curious"},
    )
    await bus.apublish(evt)

    assert any(
        "turn_completed" in r.message.lower()
        and "sess_1" in r.message
        for r in caplog.records
    )

    sub.unsubscribe()


@pytest.mark.asyncio
async def test_honcho_subscriber_unsubscribes_cleanly():
    """Multiple init/destroy cycles must not accumulate subscribers."""
    import sys
    sys.path.insert(0, "extensions/memory-honcho")
    from provider import HonchoSelfHostedProvider

    bus = get_default_bus()
    received: list[TurnCompletedEvent] = []
    test_sub = bus.subscribe(
        "turn_completed", lambda e: received.append(e)
    )

    p1 = HonchoSelfHostedProvider()
    s1 = p1.subscribe_to_outcome_events(bus)
    s1.unsubscribe()  # explicitly tear down

    evt = TurnCompletedEvent(session_id="x", turn_index=0, signals={})
    await bus.apublish(evt)

    # Test subscriber should still receive (the unsubscribe was specific
    # to the Honcho one)
    assert len(received) == 1

    test_sub.unsubscribe()
