import time

from opencomputer.awareness.life_events.registry import (
    DEFAULT_PATTERNS,
    LifeEventRegistry,
    subscribe_to_bus,
)


def test_default_registry_has_six_patterns():
    reg = LifeEventRegistry()
    pattern_ids = {p[0] for p in reg.list_patterns()}
    assert len(pattern_ids) == 6
    assert "job_change" in pattern_ids
    assert "exam_prep" in pattern_ids
    assert "burnout" in pattern_ids
    assert "relationship_shift" in pattern_ids
    assert "health_event" in pattern_ids
    assert "travel" in pattern_ids


def test_default_patterns_tuple_has_six():
    assert len(DEFAULT_PATTERNS) == 6


def test_mute_unmute_round_trip():
    reg = LifeEventRegistry()
    reg.mute("burnout")
    assert reg.is_muted("burnout")
    reg.unmute("burnout")
    assert not reg.is_muted("burnout")


def test_unmute_idempotent():
    reg = LifeEventRegistry()
    # Unmuting something that was never muted is fine.
    reg.unmute("burnout")
    assert not reg.is_muted("burnout")


def test_muted_pattern_does_not_accumulate():
    """If a pattern is muted, on_event must skip it."""
    reg = LifeEventRegistry()
    reg.mute("job_change")
    now = time.time()
    reg.on_event("browser_visit", {"url": "https://linkedin.com/jobs", "visit_time": now})
    reg.on_event("browser_visit", {"url": "https://glassdoor.com/jobs", "visit_time": now + 60.0})
    # Should NOT have queued firings (muted)
    assert reg.drain_pending() == []


def test_silent_firings_not_queued_for_chat():
    """HealthEvent has surfacing='silent' — should never appear in drain_pending."""
    reg = LifeEventRegistry()
    now = time.time()
    for i in range(5):
        reg.on_event("browser_visit", {
            "url": "https://webmd.com/symptoms",
            "visit_time": now + i * 60.0,
        })
    pending = reg.drain_pending()
    assert all(f.pattern_id != "health_event" for f in pending)


def test_relationship_shift_silent_not_queued():
    """RelationshipShift fires silently and must not appear in drain_pending."""
    reg = LifeEventRegistry()
    reg.on_event("messaging.contact_drop", {
        "magnitude": 0.8,
        "timestamp": time.time(),
        "contact_id": "alice",
    })
    pending = reg.drain_pending()
    assert all(f.pattern_id != "relationship_shift" for f in pending)


def test_hint_firings_queued_for_chat():
    """JobChange (hint) firings end up in drain_pending."""
    reg = LifeEventRegistry()
    now = time.time()
    reg.on_event("browser_visit", {"url": "https://linkedin.com/jobs", "visit_time": now})
    reg.on_event("browser_visit", {"url": "https://glassdoor.com/jobs", "visit_time": now + 60.0})
    pending = reg.drain_pending()
    assert any(f.pattern_id == "job_change" for f in pending)
    # drain twice → second time empty
    assert reg.drain_pending() == []


def test_per_pattern_exception_isolated():
    """A pattern whose accumulate raises must not take the registry down."""
    from opencomputer.awareness.life_events.pattern import (
        EvidenceItem,
        LifeEventPattern,
        PatternFiring,
    )

    class _BadPattern(LifeEventPattern):
        pattern_id = "bad_pattern"
        surface_threshold = 0.1

        def consider_event(self, event_type, metadata):
            raise RuntimeError("boom")

    class _GoodPattern(LifeEventPattern):
        pattern_id = "good_pattern"
        surface_threshold = 0.1
        surfacing = "hint"

        def consider_event(self, event_type, metadata):
            return EvidenceItem(
                timestamp=time.time(), weight=0.5, source="test",
            )

        def hint_text(self) -> str:
            return "ok"

    reg = LifeEventRegistry(patterns=[_BadPattern(), _GoodPattern()])
    # This must not raise even though _BadPattern blows up
    reg.on_event("anything", {})
    pending = reg.drain_pending()
    # Good pattern should still have fired
    assert any(f.pattern_id == "good_pattern" for f in pending)


def test_subscribe_to_bus_round_trip():
    """Bus integration: publishing on the bus should dispatch to registry."""
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import SignalEvent
    bus = TypedEventBus()
    reg = LifeEventRegistry()
    unsub = subscribe_to_bus(reg, bus)
    now = time.time()
    try:
        bus.publish(SignalEvent(
            event_type="browser_visit", source="test",
            metadata={"url": "https://linkedin.com/jobs", "visit_time": now},
        ))
        bus.publish(SignalEvent(
            event_type="browser_visit", source="test",
            metadata={"url": "https://glassdoor.com/jobs", "visit_time": now + 60.0},
        ))
    finally:
        unsub()
    pending = reg.drain_pending()
    assert any(f.pattern_id == "job_change" for f in pending)


def test_subscribe_to_bus_returns_unsub_callable():
    """Returned unsubscribe callable must actually unsubscribe."""
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import SignalEvent
    bus = TypedEventBus()
    reg = LifeEventRegistry()
    unsub = subscribe_to_bus(reg, bus)
    unsub()
    # After unsub, publishing must not reach the registry.
    bus.publish(SignalEvent(
        event_type="browser_visit", source="test",
        metadata={"url": "https://linkedin.com/jobs", "visit_time": time.time()},
    ))
    bus.publish(SignalEvent(
        event_type="browser_visit", source="test",
        metadata={"url": "https://glassdoor.com/jobs", "visit_time": time.time()},
    ))
    assert reg.drain_pending() == []
