import time
from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, PatternFiring,
)


class _DummyPattern(LifeEventPattern):
    pattern_id = "dummy"
    surface_threshold = 0.5

    def consider_event(self, event_type, metadata):
        if event_type == "test_event":
            return EvidenceItem(
                timestamp=time.time(),
                weight=float(metadata.get("weight", 0.3)),
                source="test",
            )
        return None


def test_no_evidence_yields_no_firing():
    p = _DummyPattern()
    assert p.accumulate("unrelated_event", {}) is None


def test_below_threshold_does_not_fire():
    p = _DummyPattern()
    result = p.accumulate("test_event", {"weight": 0.2})
    assert result is None


def test_above_threshold_fires():
    p = _DummyPattern()
    p.accumulate("test_event", {"weight": 0.3})
    result = p.accumulate("test_event", {"weight": 0.3})
    assert result is not None
    assert isinstance(result, PatternFiring)
    assert result.pattern_id == "dummy"
    assert result.confidence >= 0.5


def test_old_evidence_pruned():
    p = _DummyPattern()
    # Inject ancient evidence
    p._evidence.append(EvidenceItem(
        timestamp=time.time() - (30 * 86400),  # 30 days ago
        weight=1.0, source="test",
    ))
    p.accumulate("test_event", {"weight": 0.1})  # triggers _prune
    # Old evidence should be gone
    assert all(e.timestamp > time.time() - 15 * 86400 for e in p._evidence)
