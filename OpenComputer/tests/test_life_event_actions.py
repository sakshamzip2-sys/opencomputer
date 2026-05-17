"""Tests for the life-event proactive check-in cron lifecycle.

``actions.schedule_followup`` schedules a one-shot "gentle check-in" cron
N days after a life-event hint surfaces; ``actions.cancel_followup`` deletes
that cron when a verdict refutes the inference.

Profile isolation mirrors ``tests/test_life_event_state.py``:
``OPENCOMPUTER_HOME`` is monkey-patched to ``tmp_path`` so the real profile
is never touched. ``create_job`` and ``remove_job`` are monkey-patched so no
real cron job is ever written to disk.
"""
from __future__ import annotations

import logging
import time

from opencomputer.awareness.life_events import actions, state
from opencomputer.awareness.life_events.pattern import PatternFiring


def _firing(pattern_id: str = "burnout") -> PatternFiring:
    return PatternFiring(
        pattern_id=pattern_id,
        confidence=0.82,
        evidence_count=4,
        surfacing="hint",
        hint_text="noticed your work rhythm shifted",
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_followup_delay_days_covers_every_pattern():
    """Each of the four life-event patterns has a follow-up delay configured."""
    assert set(actions._FOLLOWUP_DELAY_DAYS) == {
        "burnout",
        "exam_prep",
        "job_change",
        "travel",
    }
    assert all(d > 0 for d in actions._FOLLOWUP_DELAY_DAYS.values())


def test_checkin_prompt_covers_every_pattern():
    """Each pattern has a non-empty gentle check-in message."""
    assert set(actions._CHECKIN_PROMPT) == {
        "burnout",
        "exam_prep",
        "job_change",
        "travel",
    }
    assert all(msg.strip() for msg in actions._CHECKIN_PROMPT.values())


# ---------------------------------------------------------------------------
# schedule_followup
# ---------------------------------------------------------------------------


def test_schedule_followup_creates_oneshot_cron(tmp_path, monkeypatch):
    """A firing schedules a one-shot cron N days out with the check-in prompt."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    captured: dict = {}

    def fake_create_job(**kwargs):
        captured.update(kwargs)
        return {"id": "cron-xyz", "name": kwargs.get("name")}

    monkeypatch.setattr(actions, "create_job", fake_create_job)

    actions.schedule_followup(_firing("burnout"))

    # The cron name references the pattern so it's identifiable.
    assert "burnout" in captured["name"]
    # The delivered prompt is the gentle check-in text for this pattern.
    assert captured["prompt"] == actions._CHECKIN_PROMPT["burnout"]
    # One-shot schedule reflects the right N-day delay (parse_schedule treats
    # a bare duration as a one-shot job, repeat auto-set to 1).
    assert captured["schedule"] == f"{actions._FOLLOWUP_DELAY_DAYS['burnout']}d"
    # The returned cron_id was recorded into life_event_state.
    assert state.load_state()["burnout"]["cron_id"] == "cron-xyz"


def test_schedule_followup_uses_per_pattern_delay(tmp_path, monkeypatch):
    """Different patterns produce different one-shot delays."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    captured: dict = {}
    monkeypatch.setattr(
        actions,
        "create_job",
        lambda **kw: captured.update(kw) or {"id": "cron-1"},
    )

    actions.schedule_followup(_firing("exam_prep"))
    assert captured["schedule"] == "7d"
    assert captured["prompt"] == actions._CHECKIN_PROMPT["exam_prep"]


def test_schedule_followup_dedups_active_pattern(tmp_path, monkeypatch):
    """A second schedule for the same pattern while one is active is a no-op."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    calls: list[dict] = []

    def fake_create_job(**kwargs):
        calls.append(kwargs)
        return {"id": f"cron-{len(calls)}"}

    monkeypatch.setattr(actions, "create_job", fake_create_job)

    actions.schedule_followup(_firing("travel"))
    actions.schedule_followup(_firing("travel"))  # dedup — no second cron

    assert len(calls) == 1, "create_job must be called exactly once (dedup)"
    # The first cron_id is preserved, untouched by the second call.
    assert state.load_state()["travel"]["cron_id"] == "cron-1"


def test_schedule_followup_threads_origin_into_create_job(tmp_path, monkeypatch):
    """When an origin is supplied its channel coords reach create_job."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    captured: dict = {}
    monkeypatch.setattr(
        actions,
        "create_job",
        lambda **kw: captured.update(kw) or {"id": "cron-o"},
    )

    origin = {"platform": "telegram", "chat_id": "12345", "thread_id": "67"}
    actions.schedule_followup(_firing("job_change"), origin=origin)

    assert captured["origin_platform"] == "telegram"
    assert captured["origin_chat_id"] == "12345"
    assert captured["origin_thread_id"] == "67"
    # With a routable origin, delivery is targeted back to that chat.
    assert captured["notify"] == "origin"


def test_schedule_followup_no_origin_omits_channel_targeting(tmp_path, monkeypatch):
    """origin=None still creates the cron, just without channel targeting."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    captured: dict = {}
    monkeypatch.setattr(
        actions,
        "create_job",
        lambda **kw: captured.update(kw) or {"id": "cron-n"},
    )

    actions.schedule_followup(_firing("burnout"))  # origin defaults to None

    assert captured.get("origin_platform") is None
    assert captured.get("origin_chat_id") is None
    assert captured.get("origin_thread_id") is None
    # No origin → no "origin" notify target (would fail notify validation).
    assert captured.get("notify") != "origin"
    # The cron is still created and recorded.
    assert state.load_state()["burnout"]["cron_id"] == "cron-n"


# ---------------------------------------------------------------------------
# cancel_followup
# ---------------------------------------------------------------------------


def test_cancel_followup_deletes_cron_and_clears_state(tmp_path, monkeypatch):
    """Cancelling deletes the cron job and removes the state entry."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    monkeypatch.setattr(actions, "create_job", lambda **_: {"id": "cron-del"})
    actions.schedule_followup(_firing("burnout"))
    assert "burnout" in state.load_state()

    deleted: list[str] = []
    monkeypatch.setattr(actions, "remove_job", lambda job_id: deleted.append(job_id) or True)

    actions.cancel_followup("burnout")

    assert deleted == ["cron-del"], "the recorded cron_id must be passed to remove_job"
    assert "burnout" not in state.load_state(), "state entry must be cleared"


def test_cancel_followup_no_active_followup_is_noop(tmp_path, monkeypatch):
    """Cancelling a pattern with no active follow-up is a safe no-op."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    deleted: list[str] = []
    monkeypatch.setattr(actions, "remove_job", lambda job_id: deleted.append(job_id) or True)

    # No raise, no delete call.
    actions.cancel_followup("exam_prep")

    assert deleted == [], "remove_job must not be called when no follow-up exists"
    assert state.load_state() == {}


def test_schedule_followup_unknown_pattern_logs_warning_and_skips_cron(tmp_path, monkeypatch, caplog):
    """A firing whose pattern_id has no delay/prompt config logs WARNING and skips create_job."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    calls: list[dict] = []
    monkeypatch.setattr(actions, "create_job", lambda **kw: calls.append(kw) or {"id": "should-not-appear"})

    # relationship_shift is surfacing="silent" and intentionally absent from
    # both _FOLLOWUP_DELAY_DAYS and _CHECKIN_PROMPT.
    firing = _firing("relationship_shift")

    with caplog.at_level(logging.WARNING, logger="opencomputer.awareness.life_events.actions"):
        actions.schedule_followup(firing)

    assert calls == [], "create_job must NOT be called for an unconfigured pattern"
    assert any(
        r.levelno >= logging.WARNING and "relationship_shift" in r.message
        for r in caplog.records
    ), "a WARNING mentioning the pattern_id must be emitted"
