import time

from opencomputer.awareness.life_events.relationship_shift import RelationshipShift


def test_silent_surfacing_policy():
    """RelationshipShift must default to silent — never auto-surface."""
    p = RelationshipShift()
    assert p.surfacing == "silent"


def test_low_magnitude_no_evidence():
    p = RelationshipShift()
    result = p.accumulate("messaging.contact_drop", {
        "magnitude": 0.1,
        "timestamp": time.time(),
        "contact_id": "alice",
    })
    assert result is None


def test_high_magnitude_fires_silent():
    """High-magnitude drop accumulates but firing has surfacing='silent'."""
    p = RelationshipShift()
    now = time.time()
    # Single 0.7-magnitude event >= 0.6 threshold, fires immediately.
    result = p.accumulate("messaging.contact_drop", {
        "magnitude": 0.7,
        "timestamp": now,
        "contact_id": "alice",
    })
    assert result is not None
    assert result.surfacing == "silent"
    assert result.hint_text == ""  # silent → no hint text


def test_unrelated_event_ignored():
    p = RelationshipShift()
    result = p.accumulate("browser_visit", {
        "url": "https://example.com",
        "visit_time": time.time(),
    })
    assert result is None
