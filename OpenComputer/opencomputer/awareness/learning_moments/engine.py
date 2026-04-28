"""Engine — selects at most one moment to fire per turn.

Public entry: :func:`select_reveal`. Called from the agent loop
post-turn. Returns a formatted reveal string to append, or ``None``.

Cap policy
----------

* ≤ 1 reveal per UTC-day (across all moments)
* ≤ 3 reveals per UTC-week (across all moments)
* Per-moment dedup: once fired, never again on this profile

Severity policy
---------------

* ``tip`` — suppressed by ``learning-off`` AND respects caps
* ``load_bearing`` — bypasses both ``learning-off`` and caps; MUST
  fire when the predicate matches because skipping leaves the user
  staring at silent failure (cf. PR #209's smart-fallback prompt)
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable
from pathlib import Path

from opencomputer.awareness.learning_moments.registry import (
    Context,
    LearningMoment,
    Severity,
    all_moments,
)
from opencomputer.awareness.learning_moments.store import (
    StoreState,
    load,
    save,
    seed_returning_user,
)

_log = logging.getLogger("opencomputer.awareness.learning_moments")


def _is_learning_off(profile_home: Path) -> bool:
    """``oc memory learning-off`` writes a marker file. Existence of
    that file means tip-severity reveals are suppressed."""
    return (profile_home / ".learning_off").exists()


def _today_utc() -> str:
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d")


def _week_utc() -> str:
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-W%V")


def _cap_hit(state: StoreState) -> bool:
    """Return True if the daily (1) or weekly (3) cap is reached.

    "Daily" = same UTC date (calendar day). "Weekly" = trailing 7 days
    (rolling window, not ISO week — an ISO-week cap would reset every
    Monday and let 3 fires Sun + 3 fires Mon = 6 in 24 hours).
    """
    today = _today_utc()
    now = time.time()
    week_window = 7 * 24 * 3600
    fired_today = 0
    fired_week = 0
    for entry in state.fire_log:
        ts = float(entry.get("fired_at", 0))
        d = _dt.datetime.fromtimestamp(ts, tz=_dt.UTC)
        if d.strftime("%Y-%m-%d") == today:
            fired_today += 1
        if (now - ts) <= week_window:
            fired_week += 1
    return fired_today >= 1 or fired_week >= 3


def select_reveal(
    *,
    ctx_builder: Callable[[], Context] | None = None,
    profile_home: Path,
) -> str | None:
    """Return a formatted reveal clause to append, or ``None``.

    ``ctx_builder`` is a zero-arg callable that builds the Context.
    Lazy so the cost (DB reads, MEMORY.md read) is only paid when at
    least one moment is eligible to fire after cap + dedup checks.

    If any predicate raises, that moment is skipped and the engine
    moves on. The function never raises to the caller — passive
    education must not break the agent loop.
    """
    state = load(profile_home)
    learning_off = _is_learning_off(profile_home)
    cap_hit = _cap_hit(state)

    moments = sorted(all_moments(), key=lambda m: m.priority)

    eligible: list[LearningMoment] = []
    for m in moments:
        if m.id in state.moments_fired:
            continue
        if m.severity == Severity.TIP and (learning_off or cap_hit):
            continue
        eligible.append(m)

    if not eligible:
        return None
    if ctx_builder is None:
        return None

    try:
        ctx = ctx_builder()
    except Exception:  # noqa: BLE001
        _log.debug("learning_moments: context build failed", exc_info=True)
        return None

    for m in eligible:
        try:
            fired = bool(m.predicate(ctx))
        except Exception:  # noqa: BLE001
            _log.debug(
                "learning_moments: predicate %s raised", m.id, exc_info=True,
            )
            continue
        if not fired:
            continue
        # Mark fired BEFORE returning so a concurrent loop call can't
        # double-fire the same moment.
        now = time.time()
        state.moments_fired[m.id] = now
        state.fire_log.append({"id": m.id, "fired_at": now})

        reveal_text = m.reveal
        if (
            not state.first_reveal_appended
            and m.severity == Severity.TIP
        ):
            reveal_text = (
                reveal_text
                + "\n  (turn these off: `oc memory learning-off`)"
            )
            state.first_reveal_appended = True
        save(profile_home, state)
        return _format_inline_tail(reveal_text)

    return None


def _format_inline_tail(reveal: str) -> str:
    """Two-space indent on each line + leading blank line.

    The agent loop appends this directly to the assistant message
    content after streaming has flushed. Italic/dim formatting is the
    renderer's job; the wire format is plain text + indentation.
    """
    indented = "\n".join(
        ("  " + line) if line else "" for line in reveal.splitlines()
    )
    return "\n" + indented


def maybe_seed_returning_user(profile_home: Path, total_sessions: int) -> None:
    """Idempotent seeding for users with prior sessions but no
    ``learning_moments.json`` yet. Called once at agent loop start.
    See :func:`store.seed_returning_user` for the threshold rationale.
    """
    seed_returning_user(profile_home, total_sessions)
