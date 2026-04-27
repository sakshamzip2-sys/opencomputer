import time

from opencomputer.awareness.life_events.exam_prep import ExamPrep


def test_khanacademy_visits_fire():
    p = ExamPrep()
    now = time.time()
    for i in range(4):
        p.accumulate("browser_visit", {
            "url": "https://khanacademy.org/calculus",
            "title": "Lesson",
            "visit_time": now + i * 60,
        })
    p.accumulate("browser_visit", {
        "url": "https://leetcode.com/practice-test",
        "title": "Practice Test - Arrays",
        "visit_time": now + 300,
    })
    # 4 weight=0.2 + 1 weight=0.3 = 1.1 (capped at 1.0) >> 0.7
    # Final accumulate should fire.
    result = p.accumulate("browser_visit", {
        "url": "https://leetcode.com/practice-test",
        "title": "Practice Test - DP",
        "visit_time": now + 400,
    })
    assert result is not None
    assert "study" in result.hint_text.lower() or "concepts" in result.hint_text.lower()


def test_unrelated_url_no_evidence():
    p = ExamPrep()
    result = p.accumulate("browser_visit", {
        "url": "https://news.com/article", "title": "x", "visit_time": time.time(),
    })
    assert result is None
