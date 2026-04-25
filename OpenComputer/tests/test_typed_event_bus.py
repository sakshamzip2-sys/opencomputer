"""
Phase 3.A — :class:`opencomputer.ingestion.bus.TypedEventBus` behaviour.

The bus is the foundational pub/sub primitive F2 subscribers attach to
(Session B's B3 trajectory recorder in particular). Tests pin the
public contract so later changes that would break Session B are caught
in CI.

Coverage map (per Phase 3.A plan):

* fan-out to all matching subscribers
* filter by ``event_type`` discriminator
* wildcard (``event_type=None``) receives every event
* glob pattern subscribers (``subscribe_pattern``)
* ``Subscription.unsubscribe`` stops delivery
* exception-isolated fanout: one bad subscriber does not poison others
* ``apublish`` awaits async handlers concurrently
* bounded-queue drop-oldest backpressure + ``dropped_count``
* ``default_bus`` / ``get_default_bus`` singleton identity
* required fields on :class:`SignalEvent` + all 5 subclasses
* subclass discriminator (``event_type``) is correct on every subclass
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from opencomputer.ingestion.bus import (
    BackpressurePolicy,
    TypedEventBus,
    default_bus,
    get_default_bus,
    reset_default_bus,
)
from plugin_sdk.ingestion import (
    FileObservationEvent,
    HookSignalEvent,
    MessageSignalEvent,
    SignalEvent,
    ToolCallEvent,
    WebObservationEvent,
)

# ─── 1. Fan-out ────────────────────────────────────────────────────


def test_publish_fans_out_to_all_subscribers() -> None:
    """Two handlers on the same event type both receive the event."""
    bus = TypedEventBus()
    received_a: list[SignalEvent] = []
    received_b: list[SignalEvent] = []
    bus.subscribe("tool_call", received_a.append)
    bus.subscribe("tool_call", received_b.append)

    evt = ToolCallEvent(tool_name="Read", duration_seconds=0.01)
    returned_id = bus.publish(evt)

    assert returned_id == evt.event_id
    assert received_a == [evt]
    assert received_b == [evt]


# ─── 2. Type-keyed filtering ───────────────────────────────────────


def test_publish_filters_by_event_type() -> None:
    """``tool_call`` subscriber sees ToolCallEvent; ``web_observation`` subscriber doesn't."""
    bus = TypedEventBus()
    tool_bucket: list[SignalEvent] = []
    web_bucket: list[SignalEvent] = []
    bus.subscribe("tool_call", tool_bucket.append)
    bus.subscribe("web_observation", web_bucket.append)

    bus.publish(ToolCallEvent(tool_name="Read"))

    assert len(tool_bucket) == 1
    assert web_bucket == []


# ─── 3. Wildcard subscriber ─────────────────────────────────────────


def test_publish_wildcard_subscriber_receives_all() -> None:
    """Subscriber registered with ``event_type=None`` sees every event."""
    bus = TypedEventBus()
    received: list[SignalEvent] = []
    bus.subscribe(None, received.append)

    bus.publish(ToolCallEvent(tool_name="X"))
    bus.publish(WebObservationEvent(url="https://x", domain="x"))

    assert len(received) == 2
    assert isinstance(received[0], ToolCallEvent)
    assert isinstance(received[1], WebObservationEvent)


# ─── 4. Glob-pattern subscription ───────────────────────────────────


def test_subscribe_pattern_matches_glob() -> None:
    """``web_*`` pattern matches ``web_observation`` but not ``tool_call``."""
    bus = TypedEventBus()
    matched: list[SignalEvent] = []
    bus.subscribe_pattern("web_*", matched.append)

    bus.publish(ToolCallEvent(tool_name="X"))
    bus.publish(WebObservationEvent(url="https://x", domain="x"))
    bus.publish(FileObservationEvent(path="/tmp/x", operation="read"))

    assert len(matched) == 1
    assert isinstance(matched[0], WebObservationEvent)


def test_subscribe_pattern_star_matches_all() -> None:
    """``*`` pattern matches every event_type."""
    bus = TypedEventBus()
    matched: list[SignalEvent] = []
    bus.subscribe_pattern("*", matched.append)

    bus.publish(ToolCallEvent())
    bus.publish(WebObservationEvent())
    bus.publish(HookSignalEvent())

    assert len(matched) == 3


# ─── 5. Unsubscribe ─────────────────────────────────────────────────


def test_unsubscribe_stops_delivery() -> None:
    """After ``Subscription.unsubscribe()``, further events skip this handler."""
    bus = TypedEventBus()
    received: list[SignalEvent] = []
    sub = bus.subscribe("tool_call", received.append)

    bus.publish(ToolCallEvent(tool_name="a"))
    sub.unsubscribe()
    bus.publish(ToolCallEvent(tool_name="b"))

    assert len(received) == 1
    assert received[0].tool_name == "a"


def test_unsubscribe_is_idempotent() -> None:
    """Double-unsubscribe does not raise."""
    bus = TypedEventBus()
    sub = bus.subscribe("tool_call", lambda _: None)
    sub.unsubscribe()
    sub.unsubscribe()  # must not raise


def test_subscribers_list_reflects_registrations() -> None:
    """``TypedEventBus.subscribers`` helper lists registered handles."""
    bus = TypedEventBus()
    assert bus.subscribers() == []
    a = bus.subscribe("tool_call", lambda _: None)
    b = bus.subscribe("web_observation", lambda _: None)
    c = bus.subscribe_pattern("*", lambda _: None)

    all_subs = bus.subscribers()
    assert {s.id for s in all_subs} == {a.id, b.id, c.id}
    # Type-keyed query returns wildcard + exact match.
    tc_subs = bus.subscribers("tool_call")
    assert {s.id for s in tc_subs} == {a.id, c.id}


# ─── 6. Exception isolation ─────────────────────────────────────────


def test_publish_with_failing_subscriber_does_not_break_others(caplog) -> None:
    """Handler A raises → handler B still runs + the bus logs a WARNING."""
    bus = TypedEventBus()
    received: list[SignalEvent] = []

    def boom(_: SignalEvent) -> None:
        raise ValueError("boom")

    bus.subscribe("tool_call", boom)
    bus.subscribe("tool_call", received.append)

    with caplog.at_level(logging.WARNING, logger="opencomputer.ingestion.bus"):
        bus.publish(ToolCallEvent(tool_name="Q"))

    assert len(received) == 1
    assert any("raised" in r.getMessage() for r in caplog.records)


# ─── 7. apublish + async handlers ───────────────────────────────────


def test_apublish_invokes_async_handlers() -> None:
    """Coroutine handlers are awaited by apublish."""
    bus = TypedEventBus()
    received: list[SignalEvent] = []

    async def ah(evt: SignalEvent) -> None:
        received.append(evt)

    bus.subscribe("tool_call", ah)

    asyncio.run(bus.apublish(ToolCallEvent(tool_name="a")))

    assert len(received) == 1


def test_apublish_runs_async_handlers_concurrently() -> None:
    """Two slow async handlers should run concurrently (total ~= max, not sum)."""
    bus = TypedEventBus()

    async def slow_a(_: SignalEvent) -> None:
        await asyncio.sleep(0.1)

    async def slow_b(_: SignalEvent) -> None:
        await asyncio.sleep(0.1)

    bus.subscribe("tool_call", slow_a)
    bus.subscribe("tool_call", slow_b)

    start = time.monotonic()
    asyncio.run(bus.apublish(ToolCallEvent()))
    elapsed = time.monotonic() - start

    # Concurrent: ~0.1 s; serial would be ~0.2 s. Generous upper bound.
    assert elapsed < 0.18, f"Expected concurrent execution, took {elapsed:.3f}s"


def test_apublish_async_exception_isolation() -> None:
    """Async handler raising doesn't stop other async handlers."""
    bus = TypedEventBus()
    ok: list[SignalEvent] = []

    async def bad(_: SignalEvent) -> None:
        raise RuntimeError("oops")

    async def good(evt: SignalEvent) -> None:
        ok.append(evt)

    bus.subscribe("tool_call", bad)
    bus.subscribe("tool_call", good)

    asyncio.run(bus.apublish(ToolCallEvent()))

    assert len(ok) == 1


def test_sync_publish_skips_awaiting_async_handlers() -> None:
    """Sync publish must not block / await coroutine handlers.

    The handler's coroutine is closed without running to completion.
    This is documented behavior: use apublish for async handlers.
    """
    bus = TypedEventBus()
    ran: list[SignalEvent] = []

    async def ah(evt: SignalEvent) -> None:
        ran.append(evt)

    bus.subscribe("tool_call", ah)

    # Sync publish must return promptly and NOT execute the async body.
    bus.publish(ToolCallEvent())

    assert ran == []


# ─── 8. Backpressure (bounded queue) ────────────────────────────────


def test_backpressure_drops_oldest() -> None:
    """Filling past ``queue_maxlen`` drops older events and increments counter."""
    bus = TypedEventBus(queue_maxlen=3)

    for i in range(10):
        bus.publish(ToolCallEvent(tool_name=f"t{i}"))

    assert bus.queue_size == 3
    assert bus.dropped_count == 7


def test_recent_events_debug_visibility() -> None:
    """``recent_events(limit)`` returns the most-recent events up to limit."""
    bus = TypedEventBus(queue_maxlen=5)
    for i in range(3):
        bus.publish(ToolCallEvent(tool_name=f"t{i}"))
    recent = bus.recent_events(limit=10)
    assert len(recent) == 3
    assert [e.tool_name for e in recent] == ["t0", "t1", "t2"]  # type: ignore[attr-defined]


# ─── 9. Singleton / default bus ─────────────────────────────────────


def test_default_bus_is_singleton() -> None:
    """``get_default_bus()`` returns the same shared instance as the module attr."""
    assert get_default_bus() is default_bus


def test_reset_default_bus_replaces_module_attr() -> None:
    """``reset_default_bus()`` returns + installs a fresh instance.

    This is the fixture hook tests use to isolate per-test state —
    production callers should NEVER call this.
    """
    from opencomputer.ingestion import bus as bus_module

    old = bus_module.default_bus
    new = reset_default_bus()
    assert new is not old
    assert bus_module.default_bus is new
    # Restore so other tests see the same singleton shape.
    bus_module.default_bus = old  # type: ignore[assignment]


# ─── 10. SignalEvent required-field construction ────────────────────


def test_signal_event_required_fields() -> None:
    """Base SignalEvent + all 5 subclasses construct cleanly."""
    base = SignalEvent()
    assert isinstance(base.event_id, str)
    assert isinstance(base.timestamp, float)
    assert base.session_id is None
    assert base.source == ""

    tc = ToolCallEvent(tool_name="Read")
    assert tc.event_type == "tool_call"
    assert tc.tool_name == "Read"
    assert tc.outcome == "success"

    wb = WebObservationEvent(url="https://x", domain="x")
    assert wb.event_type == "web_observation"

    fo = FileObservationEvent(path="/tmp/x", operation="read")
    assert fo.event_type == "file_observation"
    assert fo.size_bytes is None

    me = MessageSignalEvent(role="user", content_length=3)
    assert me.event_type == "message"

    he = HookSignalEvent(hook_name="PreToolUse", decision="block", reason="nope")
    assert he.event_type == "hook"
    assert he.decision == "block"


def test_signal_event_event_id_is_unique() -> None:
    """Two events constructed in succession get distinct UUIDs."""
    a = ToolCallEvent()
    b = ToolCallEvent()
    assert a.event_id != b.event_id


def test_signal_event_is_frozen() -> None:
    """Frozen+slots: mutation raises FrozenInstanceError."""
    import dataclasses

    evt = ToolCallEvent(tool_name="a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        evt.tool_name = "b"  # type: ignore[misc]


def test_signal_event_event_type_is_set_on_subclasses() -> None:
    """Every concrete subclass exposes the documented discriminator."""
    assert ToolCallEvent().event_type == "tool_call"
    assert WebObservationEvent().event_type == "web_observation"
    assert FileObservationEvent().event_type == "file_observation"
    assert MessageSignalEvent().event_type == "message"
    assert HookSignalEvent().event_type == "hook"


# ─── 11. Backpressure policy values are stable (API contract) ───────


def test_backpressure_policy_values() -> None:
    """Policy values are stable strings — downstream serializers rely on this."""
    assert BackpressurePolicy.BLOCK.value == "block"
    assert BackpressurePolicy.DROP.value == "drop"
    assert BackpressurePolicy.LOG_AND_DROP.value == "log_and_drop"
