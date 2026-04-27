import time

from opencomputer.awareness.life_events.job_change import JobChange


def test_linkedin_jobs_url_contributes():
    p = JobChange()
    result = p.accumulate("browser_visit", {
        "url": "https://www.linkedin.com/jobs/search",
        "title": "Software Engineer Jobs",
        "visit_time": time.time(),
    })
    # One hit alone (weight 0.4) is below 0.7 threshold
    assert result is None


def test_two_linkedin_visits_fire():
    p = JobChange()
    now = time.time()
    p.accumulate("browser_visit", {
        "url": "https://linkedin.com/jobs", "title": "x", "visit_time": now,
    })
    result = p.accumulate("browser_visit", {
        "url": "https://glassdoor.com/jobs", "title": "y",
        "visit_time": now + 60.0,
    })
    assert result is not None
    assert "rhythm" in result.hint_text


def test_unrelated_url_ignored():
    p = JobChange()
    result = p.accumulate("browser_visit", {
        "url": "https://github.com/saksham/repo", "title": "code", "visit_time": time.time(),
    })
    assert result is None
