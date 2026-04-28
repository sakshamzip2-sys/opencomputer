"""Trigger predicates for the v1 learning-moments registry.

Each predicate takes a :class:`Context` and returns ``bool``. All
predicates here are O(N) at most in the user message length — never
DB- or filesystem-bound. The engine builds Context once per turn and
hands it to every predicate in priority order.

False positives are acceptable: the worst case is one harmless reveal
that fires once per profile then never again. False negatives are
also acceptable: a missed teach moment isn't a regression, just a
missed opportunity.
"""
from __future__ import annotations

import re

from opencomputer.awareness.learning_moments.registry import Context

# Word-boundary path-like pattern. Catches:
#   /Users/foo/bar.py
#   ~/Documents/notes.md
#   src/auth/login.ts
# Avoids tiny one-character matches and bare module names like "src".
_PATH_RE = re.compile(
    r"(?:[~/.]|\b[A-Za-z0-9_-]+/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]+"
)


def memory_continuity_first_recall(ctx: Context) -> bool:
    """User's latest message contains a substring also in MEMORY.md.

    Cheapest possible match — substring scan. We pick the longest
    contiguous 3-word window from the user's message and check if it
    appears in memory text. Constraints:

    * Window must be ≥ 12 chars to avoid stopword-coincidence matches
      (e.g. "the user is").
    * User message must be ≥ 4 chars / 3 words; shorter messages are
      too short to confidently match.
    """
    if not ctx.memory_md_text or len(ctx.user_message) < 4:
        return False
    msg_lower = ctx.user_message.lower()
    mem_lower = ctx.memory_md_text.lower()
    words = msg_lower.split()
    if len(words) < 3:
        return False
    for i in range(len(words) - 2):
        window = " ".join(words[i : i + 3])
        if len(window) >= 12 and window in mem_lower:
            return True
    return False


def vibe_first_nonneutral(ctx: Context) -> bool:
    """First time this session has a vibe verdict other than ``calm``.

    Engine reads ``vibe_log_session_count_total`` and
    ``vibe_log_session_count_noncalm`` from the same DB query, so
    this predicate is a constant-time comparison.
    """
    return (
        ctx.vibe_log_session_count_total > 0
        and ctx.vibe_log_session_count_noncalm == 1
    )


def recent_files_paste(ctx: Context) -> bool:
    """User's latest message contains a file-path-shaped string.

    Skip very long messages — a 10kB paste of code shouldn't trigger
    the 'drag files in' nudge; the user already used the right surface
    (just not as efficiently). 5kB cap is generous.
    """
    if len(ctx.user_message) > 5000:
        return False
    return bool(_PATH_RE.search(ctx.user_message))


# ─── v2 predicates (2026-04-28) ──────────────────────────────────────


def user_md_unfilled(ctx: Context) -> bool:
    """Established user (>7 days, >7 sessions) whose USER.md is empty.

    Both gates matter:

    * Days threshold prevents firing on day-3 when the user hasn't had
      a chance to use the agent enough to know what to put in USER.md.
    * Session count gates against "installed it once 8 days ago and
      came back" — an established user has *used* OC, not just had
      it installed.

    Empty = file missing OR file contains only whitespace / a tiny
    template placeholder ("(empty — fill me in)").
    """
    if ctx.days_since_first_session < 7.0:
        return False
    if ctx.sessions_db_total_sessions < 7:
        return False
    text = (ctx.user_md_text or "").strip()
    if not text:
        return True
    return bool(len(text) < 80 and "(empty" in text.lower())


def cross_session_recall(ctx: Context) -> bool:
    """Current user message touches a topic from a recent past session.

    Engine pre-computes ``cross_session_topic_hits`` once per turn by
    scanning episodic events from the last 14 days for substring
    matches against the user message. Predicate is then a
    constant-time check.
    """
    return len(ctx.cross_session_topic_hits) > 0


def confused_session(ctx: Context) -> bool:
    """At session end, ≥30% of vibes were stuck/frustrated.

    Mechanism C — dispatched by the session-end path, not per-turn.
    Gates also on ≥4 turns so a one-off bad opening doesn't trigger.
    """
    return (
        ctx.turn_count >= 4
        and ctx.vibe_stuck_or_frustrated_fraction >= 0.30
    )
