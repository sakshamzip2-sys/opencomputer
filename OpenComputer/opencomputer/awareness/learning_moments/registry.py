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

    # v3 fields (2026-04-30) — slash-command-suggestion support.
    # All optional with safe defaults so v1/v2 callers still work
    # without modification.

    permission_mode_str: str = ""
    """Current effective permission mode as the StrEnum's ``.name``
    identifier ("DEFAULT", "PLAN", "AUTO", "ACCEPT_EDITS"). Used by
    ``suggest_auto_mode_for_long_task`` and ``suggest_plan_for_complex_task``
    to silence themselves when the user is already in the suggested
    mode."""

    recent_edit_count_this_turn: int = 0
    """How many file-mutating tool calls (Edit / MultiEdit / Write)
    ran during the most recent agent turn. Used by /undo and /diff
    suggestions to fire only when the assistant just touched several
    files. Mechanism B / C call sites set this to 0."""

    checkpoint_count_session: int = 0
    """Number of checkpoints persisted for this session. Zero means
    ``suggest_checkpoint_before_rewrite`` is eligible. Sourced from
    the coding-harness checkpoint store (degrades to 0 if absent)."""

    session_token_total: int = 0
    """Cumulative input + output tokens for this session. Drives
    ``suggest_usage_at_token_milestone`` (fires once at >100k)."""

    has_openai_key: bool = False
    """Whether ``OPENAI_API_KEY`` is in the environment. Gates
    ``suggest_voice_for_voice_user`` because realtime voice requires
    an OpenAI key."""

    # v3.1 fields (2026-04-30) — profile-suggest discovery moment.

    persona_flips_in_session: int = 0
    """How many times the active persona changed within this session.
    Used by ``suggest_profile_suggest_command`` to detect multi-context
    usage (≥3 flips ⇒ teach the user that ``/profile-suggest`` exists)."""

    current_profile_name: str = "default"
    """Name of the active profile. ``"default"`` when unset."""


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
    """Return the v1 + v2 + v3 registry. Stable ordering for tests."""
    from opencomputer.awareness.learning_moments.predicates import (
        confused_session,
        cross_session_recall,
        memory_continuity_first_recall,
        recent_files_paste,
        suggest_auto_mode_for_long_task,
        suggest_btw_for_aside,
        suggest_checkpoint_before_rewrite,
        suggest_diff_for_silent_edits,
        suggest_history_for_lookback,
        suggest_persona_for_companion_signals,
        suggest_personality_after_friction,
        suggest_plan_for_complex_task,
        suggest_profile_suggest_command,
        suggest_scrape_for_url,
        suggest_skill_save_after_long_session,
        suggest_undo_after_unwanted_edits,
        suggest_usage_at_token_milestone,
        suggest_voice_for_voice_user,
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
        # ── v3 (2026-04-30) — slash-command suggestions ───────────────
        # Inline-tail (mechanism A) tips. Priorities 70-150 so they
        # always run AFTER v1/v2 (10-60) — feature discovery should
        # cede to memory / vibe / cross-session moments when both are
        # eligible on the same turn.
        LearningMoment(
            id="suggest_plan_for_complex_task",
            predicate=suggest_plan_for_complex_task,
            reveal=(
                "(Heads up — for multi-step work like this, `/plan` "
                "lets you review the approach before I touch any code.)"
            ),
            priority=70,
        ),
        LearningMoment(
            id="suggest_auto_mode_for_long_task",
            predicate=suggest_auto_mode_for_long_task,
            reveal=(
                "(If you don't want to keep approving each step, "
                "`/auto` (or Shift+Tab Shift+Tab) runs with fewer "
                "interruptions.)"
            ),
            priority=80,
        ),
        LearningMoment(
            id="suggest_checkpoint_before_rewrite",
            predicate=suggest_checkpoint_before_rewrite,
            reveal=(
                "(`/checkpoint` saves state before I rewrite — "
                "easy rollback via `/rollback` if it goes sideways.)"
            ),
            priority=90,
        ),
        LearningMoment(
            id="suggest_undo_after_unwanted_edits",
            predicate=suggest_undo_after_unwanted_edits,
            reveal=(
                "(`/undo` reverts my most recent edit; `/rollback` "
                "resets to the last checkpoint.)"
            ),
            priority=100,
        ),
        LearningMoment(
            id="suggest_diff_for_silent_edits",
            predicate=suggest_diff_for_silent_edits,
            reveal=(
                "(`/diff` shows a clean diff of what I just edited "
                "— useful when edits stream past quickly.)"
            ),
            priority=110,
        ),
        LearningMoment(
            id="suggest_usage_at_token_milestone",
            predicate=suggest_usage_at_token_milestone,
            reveal=(
                "(This session has used over 100k tokens — `/usage` "
                "shows the cost so far.)"
            ),
            priority=120,
        ),
        LearningMoment(
            id="suggest_history_for_lookback",
            predicate=suggest_history_for_lookback,
            reveal=(
                "(`/history` lists every turn in this session — "
                "easier than scrolling.)"
            ),
            priority=130,
        ),
        LearningMoment(
            id="suggest_btw_for_aside",
            predicate=suggest_btw_for_aside,
            reveal=(
                "(For asides like that, `/btw <note>` saves it to "
                "memory without breaking the current task.)"
            ),
            priority=140,
        ),
        LearningMoment(
            id="suggest_scrape_for_url",
            predicate=suggest_scrape_for_url,
            reveal=(
                "(If you want me to read that URL, `/scrape <url>` "
                "pulls it as text.)"
            ),
            priority=150,
        ),
        # ── v3 mechanism B (system-prompt overlays) ───────────────────
        # The LLM gets the option to weave the suggestion in naturally
        # — never forced. Anchors are written as second-person
        # instructions to the model.
        LearningMoment(
            id="suggest_voice_for_voice_user",
            predicate=suggest_voice_for_voice_user,
            reveal=(
                "Context anchor: user mentioned voice/speech and has "
                "an OpenAI API key in their environment. If natural "
                "and useful, you may mention `oc voice realtime` for "
                "two-way streaming voice. Don't force the suggestion."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=160,
        ),
        LearningMoment(
            id="suggest_personality_after_friction",
            predicate=suggest_personality_after_friction,
            reveal=(
                "Context anchor: the user has shown frustration / "
                "non-calm vibes multiple times this session. If "
                "natural, you may suggest `/personality` to switch "
                "tone or `/clear` to reset the conversation. Don't "
                "be patronizing — only mention if it fits."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=170,
        ),
        LearningMoment(
            id="suggest_persona_for_companion_signals",
            predicate=suggest_persona_for_companion_signals,
            reveal=(
                "Context anchor: user is showing emotional / "
                "companion signals (lonely / rough day / overwhelmed). "
                "Respond with care first. If it fits naturally later, "
                "you may mention `/persona-mode auto` (I adapt tone) "
                "or `/personality` (manual). Never force this."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=180,
        ),
        # ── v3 mechanism C (session-end reflection) ──────────────────
        LearningMoment(
            id="suggest_skill_save_after_long_session",
            predicate=suggest_skill_save_after_long_session,
            reveal=(
                "(That was a long session. If it's a workflow you'll "
                "repeat, `oc skills new` captures the pattern as a "
                "reusable skill — same agent, less re-explaining.)"
            ),
            surface=Surface.SESSION_END,
            priority=190,
        ),
        # ── v3.1 (2026-04-30) — profile-suggest discovery ────────────
        # Surfaces /profile-suggest once when a session shows multi-
        # context usage on the default profile. The slash command is
        # the actual feature; this moment teaches that it exists.
        LearningMoment(
            id="suggest_profile_suggest_command",
            predicate=suggest_profile_suggest_command,
            reveal=(
                "(You've been switching contexts a lot this session — "
                "`/profile-suggest` analyzes your usage and tells you "
                "if a specialized profile would help.)"
            ),
            priority=200,
        ),
    )
