"""Burnout life-event detector tests.

Burnout keeps a 21-day recency window (``burnout.py`` ``window_days``),
so these tests build timestamps relative to *now*. They previously
hardcoded 2026-04-27 — which silently armed as a time-bomb and started
failing exactly 21 days later once that date aged past the window.
"""
import datetime

from opencomputer.awareness.life_events.burnout import Burnout


def _recent_ts(hour: int, minute: int = 0) -> float:
    """Epoch seconds for ``hour:minute`` *yesterday*.

    Recent enough to stay inside Burnout's 21-day window whenever the
    test runs, and never in the future — so the hour-of-day behaviour is
    what's exercised, not the recency filter.
    """
    day = datetime.datetime.now() - datetime.timedelta(days=1)
    return day.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).timestamp()


def test_late_night_edit_contributes():
    """Edits at 1 AM (hour=1) accumulate."""
    p = Burnout()
    p.accumulate("file_edit", {"timestamp": _recent_ts(1, 30)})
    assert len(p._evidence) == 1


def test_daytime_edit_ignored():
    """A recent noon edit is ignored — daytime, not the late-night signal."""
    p = Burnout()
    result = p.accumulate("file_edit", {"timestamp": _recent_ts(12, 0)})
    assert result is None
    assert len(p._evidence) == 0
