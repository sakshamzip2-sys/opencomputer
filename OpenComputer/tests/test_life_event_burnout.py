import time

from opencomputer.awareness.life_events.burnout import Burnout


def _recent_ts(hour: int) -> float:
    """A timestamp at ``hour``:30 on a recent day.

    Always lands inside ``Burnout``'s 21-day recency window regardless of
    when the suite runs. The previous hard-coded ``2026-04-27`` dates were
    a time-bomb — they aged out of the window 21 days later (the suite
    started failing on 2026-05-18), even though nothing in the code
    changed. Anchoring to "yesterday" keeps the edit recent and firmly in
    the past on every run.
    """
    lt = time.localtime(time.time() - 86400)  # yesterday
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1))


def test_late_night_edit_contributes():
    """Edits at 1 AM (hour=1) accumulate."""
    p = Burnout()
    p.accumulate("file_edit", {"timestamp": _recent_ts(1)})
    assert len(p._evidence) == 1


def test_daytime_edit_ignored():
    p = Burnout()
    result = p.accumulate("file_edit", {"timestamp": _recent_ts(12)})
    assert result is None
    assert len(p._evidence) == 0
