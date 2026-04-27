import time

from opencomputer.awareness.life_events.travel import Travel


def test_hint_surfacing_policy():
    p = Travel()
    assert p.surfacing == "hint"


def test_single_travel_visit_below_threshold():
    p = Travel()
    result = p.accumulate("browser_visit", {
        "url": "https://www.booking.com/searchresults",
        "visit_time": time.time(),
    })
    # Single 0.3 weight is below 0.7 threshold
    assert result is None


def test_three_travel_visits_fire():
    p = Travel()
    now = time.time()
    p.accumulate("browser_visit", {
        "url": "https://www.booking.com/searchresults",
        "visit_time": now,
    })
    p.accumulate("browser_visit", {
        "url": "https://www.expedia.com/Flights",
        "visit_time": now + 60.0,
    })
    result = p.accumulate("browser_visit", {
        "url": "https://www.makemytrip.com/hotels",
        "visit_time": now + 120.0,
    })
    # 3 hits @ ~0.3 = ~0.9 > 0.7
    assert result is not None
    assert result.surfacing == "hint"
    assert "trip" in result.hint_text.lower() or "destination" in result.hint_text.lower()


def test_unrelated_url_ignored():
    p = Travel()
    result = p.accumulate("browser_visit", {
        "url": "https://github.com/saksham/repo",
        "visit_time": time.time(),
    })
    assert result is None
