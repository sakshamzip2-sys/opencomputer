import time

from opencomputer.awareness.life_events.health_event import HealthEvent


def test_silent_surfacing_policy():
    """HealthEvent must default to silent — never auto-surface."""
    p = HealthEvent()
    assert p.surfacing == "silent"


def test_webmd_visit_contributes():
    p = HealthEvent()
    result = p.accumulate("browser_visit", {
        "url": "https://www.webmd.com/symptoms/headache",
        "visit_time": time.time(),
    })
    # Single 0.3 weight is below 0.6 threshold
    assert result is None
    assert len(p._evidence) == 1


def test_two_health_visits_fire_silent():
    p = HealthEvent()
    now = time.time()
    p.accumulate("browser_visit", {
        "url": "https://www.mayoclinic.org/diseases-conditions",
        "visit_time": now,
    })
    p.accumulate("browser_visit", {
        "url": "https://www.healthline.com/nutrition",
        "visit_time": now + 60.0,
    })
    # 2 hits @ weight=0.3 -> ~0.6 = threshold
    result = p.accumulate("browser_visit", {
        "url": "https://www.drugs.com/condition/anxiety.html",
        "visit_time": now + 120.0,
    })
    assert result is not None
    assert result.surfacing == "silent"


def test_unrelated_url_no_evidence():
    p = HealthEvent()
    result = p.accumulate("browser_visit", {
        "url": "https://news.ycombinator.com",
        "visit_time": time.time(),
    })
    assert result is None
