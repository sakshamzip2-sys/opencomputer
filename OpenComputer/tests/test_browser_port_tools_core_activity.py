"""Unit tests for ``tools_core.activity``."""

from __future__ import annotations

import time

from extensions.browser_control.tools_core.activity import (
    clear_activity,
    known_target_ids,
    last_action_time,
    record_action,
    seconds_since_last_action,
)


def test_record_then_read_back() -> None:
    clear_activity()
    record_action("t1")
    t = last_action_time("t1")
    assert t is not None
    assert t > 0
    assert "t1" in known_target_ids()


def test_unknown_target_returns_none() -> None:
    clear_activity()
    assert last_action_time("never") is None
    assert seconds_since_last_action("never") is None


def test_clear_one() -> None:
    record_action("a")
    record_action("b")
    clear_activity("a")
    assert last_action_time("a") is None
    assert last_action_time("b") is not None
    clear_activity()


def test_seconds_since_last_action_monotonic() -> None:
    clear_activity()
    record_action("z")
    s1 = seconds_since_last_action("z")
    time.sleep(0.01)
    s2 = seconds_since_last_action("z")
    assert s1 is not None and s2 is not None
    assert s2 > s1


def test_empty_target_id_no_op() -> None:
    clear_activity()
    record_action("")
    assert last_action_time("") is None
    assert known_target_ids() == []
