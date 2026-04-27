import time
from opencomputer.awareness.life_events.burnout import Burnout


def test_late_night_edit_contributes():
    """Edits at 1 AM (hour=1) accumulate."""
    p = Burnout()
    midnight_ts = time.mktime((2026, 4, 27, 1, 30, 0, 0, 0, -1))
    p.accumulate("file_edit", {"timestamp": midnight_ts})
    assert len(p._evidence) == 1


def test_daytime_edit_ignored():
    p = Burnout()
    noon_ts = time.mktime((2026, 4, 27, 12, 0, 0, 0, 0, -1))
    result = p.accumulate("file_edit", {"timestamp": noon_ts})
    assert result is None
    assert len(p._evidence) == 0
