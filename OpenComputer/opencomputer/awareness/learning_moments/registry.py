"""Hand-curated registry of learning moments + dataclasses (2026-04-28).

A :class:`LearningMoment` encodes ONE behavioral trigger + ONE inline
reveal. The registry is intentionally small — bigger means more
cognitive load on the user, not more value. v1 ships 3 moments;
instrumentation will tell us which to add (or remove) for v2.

Design spec:
``docs/superpowers/specs/2026-04-28-passive-education-design.md``

Plan:
``docs/superpowers/plans/2026-04-28-passive-education.md``
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    """Whether a moment respects the ``learning-off`` flag.

    ``tip`` — informational reveals; suppressed when the user runs
    ``oc memory learning-off``. The default for surfaceable trivia.

    ``load_bearing`` — prompts that must fire regardless of the
    flag. The smart-fallback prompt for missing Ollama (PR #209) is
    the canonical example: skipping it leaves the user staring at a
    silent failure.
    """

    TIP = "tip"
    LOAD_BEARING = "load_bearing"


class Surface(str, Enum):
    """Which mechanism delivers the reveal.

    v1 only implements ``INLINE_TAIL``. The other two values are
    declared up-front so v2 can dispatch by surface without changing
    this file. Adding a new value is a non-breaking change as long as
    the dispatcher routes unknown surfaces to a graceful no-op.
    """

    INLINE_TAIL = "inline_tail"
    SYSTEM_PROMPT = "system_prompt"   # v2 — soft / observational moments
    SESSION_END = "session_end"       # v2 — continuity / summary moments


@dataclass(frozen=True, slots=True)
class Context:
    """Snapshot of state passed to predicates each turn.

    The engine builds this once per turn (lazily — only when at least
    one moment is eligible to fire). Predicates read off it without
    further DB or filesystem access. Keeping predicates pure-on-Context
    is what makes the hot path cheap: O(N_moments) Python comparisons,
    no DB.
    """

    session_id: str
    profile_home: Path
    user_message: str
    memory_md_text: str
    vibe_log_session_count_total: int
    vibe_log_session_count_noncalm: int
    sessions_db_total_sessions: int

    # v2 fields (2026-04-28). All optional with safe defaults so v1
    # callers that don't set them still work.

    user_md_text: str = ""
    """USER.md content — used by ``user_md_unfilled`` to detect the
    "I've been guessing about you" trigger."""

    days_since_first_session: float = 0.0
    """How long since the user first ran OC — gates moments that
    should only surface to established users (e.g. ``user_md_unfilled``
    fires only after 7 days)."""

    cross_session_topic_hits: tuple[tuple[str, str], ...] = ()
    """Pre-computed (topic, episodic_session_id) pairs where a
    substring of the current user message also appears in an episodic
    event from a different session in the last 14 days. Engine
    pre-computes once per turn. Empty tuple if no hits."""

    vibe_stuck_or_frustrated_fraction: float = 0.0
    """Fraction (0..1) of this session's vibe_log entries with
    ``stuck`` or ``frustrated`` labels. Used by ``confused_session``
    at session-end time."""

    turn_count: int = 0
    """How many user→assistant turns have occurred. Mechanism C
    moments use this to skip very short sessions where reflection
    would be premature."""


@dataclass(frozen=True, slots=True)
class LearningMoment:
    """One reveal definition.

    ``id`` is the persistence key — renaming an id is a soft-breaking
    change (it re-fires for users who already saw the previous id).
    ``predicate`` MUST be cheap on the hot path (called every turn);
    expensive computations precompute or run async, not in here.
    ``priority`` ties when multiple moments fire on the same turn —
    lower = higher priority.
    """

    id: str
    predicate: Callable[[Context], bool]
    reveal: str
    severity: Severity = Severity.TIP
    surface: Surface = Surface.INLINE_TAIL
    min_oc_version: str = "0.0.0"
    priority: int = 50


def all_moments() -> tuple[LearningMoment, ...]:
    """Return the v1 + v2 registry. Stable ordering for tests."""
    from opencomputer.awareness.learning_moments.predicates import (
        confused_session,
        cross_session_recall,
        memory_continuity_first_recall,
        recent_files_paste,
        user_md_unfilled,
        vibe_first_nonneutral,
    )

    return (
        # ── v1: inline-tail tips ──────────────────────────────────────
        LearningMoment(
            id="memory_continuity_first_recall",
            predicate=memory_continuity_first_recall,
            reveal="(I had this noted from last time — yell if it's stale.)",
            priority=10,
        ),
        LearningMoment(
            id="vibe_first_nonneutral",
            predicate=vibe_first_nonneutral,
            reveal=(
                "(I keep a small log of how each chat feels — "
                "`oc memory show vibe` if you want to see it.)"
            ),
            priority=20,
        ),
        LearningMoment(
            id="recent_files_paste",
            predicate=recent_files_paste,
            reveal=(
                "(You can drag files in directly — "
                "or just say 'show me X.py'.)"
            ),
            priority=30,
        ),
        # ── v2: more inline-tail tips ─────────────────────────────────
        LearningMoment(
            id="user_md_unfilled",
            predicate=user_md_unfilled,
            reveal=(
                "(I've been guessing about you from context — "
                "`oc memory edit user` if you want to fill out USER.md "
                "so I'm not winging it.)"
            ),
            priority=40,
        ),
        # ── v2: system-prompt overlay (mechanism B) ───────────────────
        # Reveal text becomes a system-prompt context line for the
        # next turn — the LLM may weave it in naturally if it fits.
        # Format mimics the existing companion overlay's anchor lines.
        LearningMoment(
            id="cross_session_recall",
            predicate=cross_session_recall,
            reveal=(
                "Context anchor: a topic the user is touching now "
                "also came up in a recent past session. If natural, "
                "you may reference the continuity (e.g. \"we touched "
                "on this earlier this week — want me to recall what "
                "we landed on?\"). Don't force it."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=50,
        ),
        # ── v2: session-end reflection (mechanism C) ──────────────────
        LearningMoment(
            id="confused_session",
            predicate=confused_session,
            reveal=(
                "(That session felt stuck. If you come back to this, "
                "`/clear` resets context — old confusion sometimes "
                "sticks around in the conversation history.)"
            ),
            surface=Surface.SESSION_END,
            priority=60,
        ),
    )
