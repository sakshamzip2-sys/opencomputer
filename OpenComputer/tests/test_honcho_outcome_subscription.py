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

from opencomputer.ingestion.bus import get_default_bus
from plugin_sdk.ingestion import TurnCompletedEvent


@pytest.fixture(autouse=True)
def _isolate_bus_subscribers():
    """Snapshot + restore the default bus's subscriber list AND force a
    fresh re-import of the Honcho provider module.

    Two reasons full-suite mode breaks otherwise:
      1. Other tests leave subscribers attached on the module-global
         default bus.
      2. Other tests import the Honcho provider via the package path
         (extensions.memory_honcho.provider), while we use the
         plugin-loader-mode path-insert (provider). Both import paths
         end up in sys.modules under DIFFERENT keys, with separate
         logger globals — our monkeypatch only hits one of them.
    """
    import sys

    bus = get_default_bus()
    saved_subs = list(bus._subs)
    bus._subs.clear()

    # Drop any cached "provider" or extensions.memory_honcho.provider
    # so the test's import re-runs and our monkeypatch lands on the
    # active logger global.
    for k in list(sys.modules.keys()):
        if k == "provider" or k.endswith(".memory_honcho.provider"):
            del sys.modules[k]

    yield

    bus._subs.clear()
    bus._subs.extend(saved_subs)


@pytest.mark.asyncio
async def test_honcho_subscriber_receives_turn_completed_event(monkeypatch):
    """Verify the Honcho provider's subscribe_to_outcome_events wires a
    handler that fires on turn_completed events. We patch the module's
    logger to a deterministic capture instead of relying on caplog,
    which has full-suite ordering interactions with other tests' logger
    configuration."""
    import sys
    sys.path.insert(0, "extensions/memory-honcho")
    import provider as provider_module
    from provider import HonchoSelfHostedProvider

    captured: list[tuple[str, int, str]] = []

    class _CaptureLogger:
        def info(self, fmt, *args):
            captured.append(("info", logging.INFO, fmt % args if args else fmt))

        def warning(self, fmt, *args):
            captured.append(("warning", logging.WARNING, fmt % args if args else fmt))

    monkeypatch.setattr(provider_module, "logger", _CaptureLogger())

    p = HonchoSelfHostedProvider()
    bus = get_default_bus()
    sub = p.subscribe_to_outcome_events(bus)

    evt = TurnCompletedEvent(
        session_id="sess_1",
        source="gateway.dispatch",
        turn_index=3,
        signals={"tool_call_count": 2, "vibe_after": "curious"},
    )
    await bus.apublish(evt)

    # Handler should have logged at INFO with "turn_completed" + session_id
    assert any(
        "turn_completed" in msg.lower() and "sess_1" in msg
        for _, _, msg in captured
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
