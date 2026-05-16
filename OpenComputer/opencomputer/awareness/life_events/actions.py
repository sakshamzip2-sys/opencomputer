"""Proactive check-in cron lifecycle for life-event "teeth".

When a life-event pattern fires and surfaces a hint to the user, this
module schedules a gentle, one-shot check-in cron a few days out — the
agent circles back to ask how the user is doing, without nagging. When a
later verdict refutes the inference, the scheduled follow-up is cancelled.

Two public functions:

- :func:`schedule_followup` — dedup-guarded; schedules the one-shot cron
  N days out (per :data:`_FOLLOWUP_DELAY_DAYS`) and records the returned
  ``cron_id`` in ``life_event_state.json``.
- :func:`cancel_followup` — deletes the scheduled cron (if any) and clears
  the pattern's state entry. A pattern with no active follow-up is a safe
  no-op.

The cron itself is created via :func:`opencomputer.cron.jobs.create_job`.
A bare duration schedule string (``"3d"``) is parsed by
:func:`~opencomputer.cron.jobs.parse_schedule` as a ONE-SHOT job — kind
``"once"`` with ``run_at = now + N days`` — and ``create_job`` auto-sets
``repeat=1`` for one-shot schedules, so the check-in fires exactly once.

``schedule_followup`` accepts an optional ``origin`` mapping carrying the
user's active-channel coordinates (``platform`` / ``chat_id`` /
``thread_id``); when present they are threaded into ``create_job`` so the
check-in is delivered back to that chat (``notify="origin"``). When absent
the cron is still created — it just has no channel targeting.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opencomputer.awareness.life_events import state
from opencomputer.awareness.life_events.pattern import PatternFiring
from opencomputer.cron.jobs import create_job, remove_job

_log = logging.getLogger(__name__)


# Days after a hint surfaces before the gentle check-in fires. Tuned per
# pattern: travel resolves fast (back from the trip), exam prep is the
# longest arc (wait until after the exam window).
_FOLLOWUP_DELAY_DAYS: dict[str, int] = {
    "burnout": 3,
    "exam_prep": 7,
    "job_change": 5,
    "travel": 2,
}


# The message the check-in cron delivers N days later. Companion voice
# (docs/superpowers/specs/2026-04-27-companion-voice-examples.md): warm,
# brief, a real anchor, one question back — NOT nagging, no over-cheer.
_CHECKIN_PROMPT: dict[str, str] = {
    "burnout": (
        "Circling back gently — a few days ago your work rhythm looked "
        "like it was running hot. How are you holding up now? No agenda, "
        "just checking in."
    ),
    "exam_prep": (
        "Quick check-in — the exam you were prepping for should be around "
        "now or just behind you. How did it go? Hope you got some rest "
        "after."
    ),
    "job_change": (
        "Thinking of you — it looked like something was shifting on the "
        "work front recently. How's it settling? Happy to help with "
        "anything, or just listen."
    ),
    "travel": (
        "Welcome back, if you've landed — looked like you were travelling "
        "the last few days. How was the trip? Anything you want to pick "
        "back up?"
    ),
}


def schedule_followup(
    firing: PatternFiring,
    *,
    origin: Mapping[str, Any] | None = None,
    surfaced_turn: int = 0,
) -> None:
    """Schedule a one-shot gentle check-in cron for a fired life-event hint.

    DEDUP FIRST: if ``life_event_state.json`` already holds a ``cron_id``
    for ``firing.pattern_id``, this is a no-op — a hint that re-fires while
    its follow-up is still active never schedules a second cron.

    Otherwise a one-shot cron is created ``N`` days out (per
    :data:`_FOLLOWUP_DELAY_DAYS`) carrying the pattern's
    :data:`_CHECKIN_PROMPT` text, and the returned ``cron_id`` is recorded
    via :func:`state.mark_surfaced`.

    Args:
        firing: A :class:`~opencomputer.awareness.life_events.pattern.PatternFiring`
            (only ``pattern_id`` is read).
        origin: Optional mapping of the user's active-channel coordinates
            (``platform`` / ``chat_id`` / ``thread_id``). When supplied,
            the check-in is delivered back to that chat; when ``None`` the
            cron is created without channel targeting.
        surfaced_turn: The 1-indexed turn number the hint surfaced on.
            Threaded straight into :func:`state.mark_surfaced` so the
            STOP-hook classifier can skip the surfacing turn's own STOP.
            Optional (defaults to ``0``) — callers that don't have a turn
            index keep working; ``0`` means "always judge the next reply".
    """
    pattern_id = firing.pattern_id

    # --- Dedup: an active follow-up already exists for this pattern. ------
    existing = state.load_state().get(pattern_id)
    if isinstance(existing, dict) and existing.get("cron_id"):
        _log.debug(
            "life-event follow-up already scheduled for %s (cron %s); skipping",
            pattern_id,
            existing["cron_id"],
        )
        return

    delay_days = _FOLLOWUP_DELAY_DAYS.get(pattern_id)
    prompt = _CHECKIN_PROMPT.get(pattern_id)
    if delay_days is None or prompt is None:
        _log.warning(
            "no follow-up delay/prompt configured for life-event pattern %r; "
            "skipping check-in cron",
            pattern_id,
        )
        return

    # A bare duration string is parsed as a one-shot ("once") schedule;
    # create_job then auto-sets repeat=1 so the check-in fires exactly once.
    schedule = f"{delay_days}d"

    # Thread the active-channel coords through so notify="origin" can route
    # the check-in back to the user's chat. Absent origin → no targeting.
    origin_platform: str | None = None
    origin_chat_id: str | None = None
    origin_thread_id: str | None = None
    notify: str | None = None
    if origin:
        origin_platform = origin.get("platform")
        origin_chat_id = origin.get("chat_id")
        origin_thread_id = origin.get("thread_id")
        # Only request origin-delivery when there is actually a chat to
        # route to — notify="origin" without origin_* fails delivery.
        if origin_platform and origin_chat_id:
            notify = "origin"

    job = create_job(
        schedule=schedule,
        name=f"life-event check-in: {pattern_id}",
        prompt=prompt,
        notify=notify,
        origin_platform=origin_platform,
        origin_chat_id=origin_chat_id,
        origin_thread_id=origin_thread_id,
    )

    cron_id = job["id"]
    state.mark_surfaced(pattern_id, cron_id, surfaced_turn)
    _log.info(
        "scheduled life-event check-in for %s in %dd (cron %s)",
        pattern_id,
        delay_days,
        cron_id,
    )


def cancel_followup(pattern_id: str) -> None:
    """Cancel a scheduled life-event check-in cron.

    Reads the ``cron_id`` recorded for ``pattern_id`` in
    ``life_event_state.json``; if one is present the cron job is deleted
    via :func:`~opencomputer.cron.jobs.remove_job`. The pattern's state
    entry is then cleared either way.

    A pattern with no active follow-up (no entry, or an entry without a
    ``cron_id``) is a safe no-op — no deletion, no raise.

    Args:
        pattern_id: The life-event pattern whose follow-up to cancel.
    """
    entry = state.load_state().get(pattern_id)
    cron_id = entry.get("cron_id") if isinstance(entry, dict) else None

    if cron_id:
        try:
            removed = remove_job(cron_id)
        except Exception:  # pragma: no cover - defensive; cron I/O failure
            _log.warning(
                "failed to delete life-event check-in cron %s for %s",
                cron_id,
                pattern_id,
                exc_info=True,
            )
        else:
            if not removed:
                _log.debug(
                    "life-event check-in cron %s for %s already gone",
                    cron_id,
                    pattern_id,
                )

    # Clear the state entry regardless — the tooth is dropped. clear() is
    # itself a no-op when pattern_id is absent.
    state.clear(pattern_id)


__all__ = ["cancel_followup", "schedule_followup"]
