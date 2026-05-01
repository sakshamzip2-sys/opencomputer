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


# ─── v3 predicates (2026-04-30) — slash-command suggestions ──────────
#
# All predicates are pure functions of Context. Regex constants are
# module-level so the compile cost is paid once at import. Predicates
# err on the side of NOT firing — false negatives are cheap (a missed
# tip), false positives mean a tip the user already knew (and burns
# the per-moment dedup permanently). When in doubt, gate harder.


_MULTISTEP_KEYWORDS = re.compile(
    r"\b(?:step[ -]by[ -]step|first.+then|plan(?:ning)?|"
    r"phases?|milestones?|breakdown|approach|outline|"
    r"strategy|roadmap)\b",
    re.IGNORECASE | re.DOTALL,
)

_LONG_TASK_VERBS = re.compile(
    r"\b(?:build|create|implement|develop|design|set\s*up|"
    r"refactor|migrate|integrate|wire|scaffold|port|"
    r"add\s+(?:a\s+)?(?:new\s+)?(?:feature|module|system|service))\b",
    re.IGNORECASE,
)

_REWRITE_KEYWORDS = re.compile(
    r"\b(?:rewrite|refactor|redo|overhaul|restructure|reorganize|"
    r"clean\s*up|tear\s*out|start\s*over|from\s*scratch)\b",
    re.IGNORECASE,
)

_UNDO_KEYWORDS = re.compile(
    r"\b(?:revert|undo|go\s*back|that's\s*wrong|didn't\s*want|"
    r"not\s*what\s+i|wasn't\s*supposed|broke\s*(?:it|that|"
    r"the)|broken|messed\s*up|mess\s*it\s*up)\b",
    re.IGNORECASE,
)

_LOOKBACK_KEYWORDS = re.compile(
    r"\b(?:what\s*changed|what\s*did\s*you\s*(?:do|change|edit|"
    r"modify)|show\s*me\s*the\s*diff|what\s*did\s*we|"
    r"earlier\s+(?:we|you|in)|previously|"
    r"a\s*(?:few|couple)\s*(?:turns?|messages?)\s*ago)\b",
    re.IGNORECASE,
)

_BTW_KEYWORDS = re.compile(
    r"\b(?:by\s*the\s*way|btw|side\s*note|aside\s*from\s*that|"
    r"on\s*another\s*note|also\s*remember|fyi)\b",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)

_FETCH_VERBS = re.compile(
    r"\b(?:scrape|fetch|read\s*(?:this|that|it)|pull\s*(?:this|that)|"
    r"grab\s*(?:this|that)|download|crawl|extract\s*from)\b",
    re.IGNORECASE,
)

_VOICE_KEYWORDS = re.compile(
    r"\b(?:speak\s*to\s*me|talking|voice\s*(?:mode|chat|call)|"
    r"out\s*loud|say\s*it|narrate|read\s*aloud|"
    r"want\s*to\s*talk|hear\s*you)\b",
    re.IGNORECASE,
)

_EMOTION_ANCHORS = re.compile(
    r"\b(?:lonely|rough\s*day|hard\s*day|burnt?\s*out|"
    r"feeling\s*(?:down|low|stuck|tired|overwhelmed|sad)|"
    r"can't\s*focus|exhausted|stressed\s*out|"
    r"mentally\s*drained|need\s*a\s*break)\b",
    re.IGNORECASE,
)


def suggest_plan_for_complex_task(ctx: Context) -> bool:
    """Long multi-step request submitted outside PLAN mode.

    Length gate (≥200 chars) keeps this off short Q&A. Permission
    gate prevents firing when the user is already in /plan.
    """
    if not ctx.user_message or len(ctx.user_message) < 200:
        return False
    if ctx.permission_mode_str == "PLAN":
        return False
    return bool(_MULTISTEP_KEYWORDS.search(ctx.user_message))


def suggest_auto_mode_for_long_task(ctx: Context) -> bool:
    """Build/create-style request still in DEFAULT mode.

    AUTO and ACCEPT_EDITS users have already opted into reduced
    prompting; PLAN users are intentionally pre-approving. Only
    DEFAULT-mode users benefit from this nudge.
    """
    if not ctx.user_message or len(ctx.user_message) < 60:
        return False
    if ctx.permission_mode_str != "DEFAULT" and ctx.permission_mode_str != "":
        return False
    return bool(_LONG_TASK_VERBS.search(ctx.user_message))


def suggest_checkpoint_before_rewrite(ctx: Context) -> bool:
    """Rewrite-style request when no checkpoint exists yet.

    Once a single checkpoint exists in the session, the user has
    discovered the feature — stop nagging.
    """
    if ctx.checkpoint_count_session > 0:
        return False
    if not ctx.user_message:
        return False
    return bool(_REWRITE_KEYWORDS.search(ctx.user_message))


def suggest_undo_after_unwanted_edits(ctx: Context) -> bool:
    """User signals dissatisfaction right after multiple edits.

    Threshold of 3 edits: a single Edit with a wording change rarely
    needs undo; 3+ edits in one turn is "the assistant did a lot",
    and the user's frustration carries weight.
    """
    if ctx.recent_edit_count_this_turn < 3:
        return False
    if not ctx.user_message:
        return False
    return bool(_UNDO_KEYWORDS.search(ctx.user_message))


def suggest_diff_for_silent_edits(ctx: Context) -> bool:
    """User asks 'what changed' after silent edits.

    Two-edit threshold (lower than undo) because /diff is purely
    informational — wrong-firing costs nothing.
    """
    if ctx.recent_edit_count_this_turn < 2:
        return False
    if not ctx.user_message:
        return False
    return bool(_LOOKBACK_KEYWORDS.search(ctx.user_message))


def suggest_usage_at_token_milestone(ctx: Context) -> bool:
    """Cumulative session tokens crossed 100k.

    Single threshold; per-moment dedup ensures the user sees this
    at most once ever, so no need for tiered milestones.
    """
    return ctx.session_token_total >= 100_000


def suggest_history_for_lookback(ctx: Context) -> bool:
    """User asks about earlier turns.

    Length cap (≤600 chars) skips long messages where lookback
    keywords appear incidentally. Keeps the predicate honest.
    """
    if not ctx.user_message or len(ctx.user_message) > 600:
        return False
    return bool(_LOOKBACK_KEYWORDS.search(ctx.user_message))


def suggest_btw_for_aside(ctx: Context) -> bool:
    """Message contains an aside marker AND has substantive content.

    Length floor (30 chars) prevents firing on a bare "btw" — the
    user must be writing a real aside for /btw to be relevant.
    """
    if not ctx.user_message or len(ctx.user_message) < 30:
        return False
    return bool(_BTW_KEYWORDS.search(ctx.user_message))


def suggest_scrape_for_url(ctx: Context) -> bool:
    """User pasted a URL without explicit fetch verb.

    Skipping when the message already says "scrape this" / "read
    this URL" — the user has discovered or remembered the verb.
    """
    if not ctx.user_message or len(ctx.user_message) > 5000:
        return False
    if not _URL_RE.search(ctx.user_message):
        return False
    return not _FETCH_VERBS.search(ctx.user_message)


# ─── v3 mechanism B / C predicates ────────────────────────────────────


def suggest_voice_for_voice_user(ctx: Context) -> bool:
    """User mentions voice/talk/speak AND has an OpenAI key.

    Gating on the key prevents recommending a feature the user
    can't enable (realtime voice requires OpenAI). Mechanism B —
    delivered as a system-prompt anchor for the LLM to weave.
    """
    if not ctx.has_openai_key or not ctx.user_message:
        return False
    return bool(_VOICE_KEYWORDS.search(ctx.user_message))


def suggest_personality_after_friction(ctx: Context) -> bool:
    """Vibe has gone non-calm 3+ times in this session.

    Three is enough to suggest pattern; one or two could be a
    single bad message. Mechanism B — LLM gets the option to
    suggest /personality or /clear naturally.
    """
    return ctx.vibe_log_session_count_noncalm >= 3


def suggest_persona_for_companion_signals(ctx: Context) -> bool:
    """User shows emotional / companion signals.

    Mechanism B — the LLM is instructed to RESPOND WITH CARE FIRST
    and only mention persona-mode if it fits naturally. The reveal
    string anchors that intent on the LLM side.
    """
    if not ctx.user_message:
        return False
    return bool(_EMOTION_ANCHORS.search(ctx.user_message))


def suggest_skill_save_after_long_session(ctx: Context) -> bool:
    """Long session likely contains a repeatable workflow.

    Mechanism C — fires at session end. 20 turns is the threshold
    where sessions reliably contain enough back-and-forth to be
    "a workflow" rather than "a quick chat".
    """
    return ctx.turn_count >= 20


# ─── v3.1 predicate (2026-04-30) — profile-suggest discovery ─────────


def suggest_profile_suggest_command(ctx: Context) -> bool:
    """Fire when EITHER:

    A. User flipped persona ≥3 times this session (existing trigger), OR
    B. The daily-analysis cache has a fresh non-dismissed suggestion
       (Plan 3, 2026-05-01 — proactive surface).

    Both gates require the user to be on the default profile — users on
    a named profile have already engaged with the profile system, no
    need to re-teach.

    Trigger A is the strongest in-loop signal: the user is doing
    multi-context work right now and might benefit from a specialized
    profile. Trigger B catches longer-horizon patterns the user might
    not have noticed in-session (e.g., "you've coded 18 of 30 sessions
    but don't have a 'work' profile yet").
    """
    if ctx.current_profile_name != "default":
        return False

    # Trigger A: in-session persona-flip thrash (existing).
    if ctx.persona_flips_in_session >= 3:
        return True

    # Trigger B: daily cache has a fresh non-dismissed suggestion.
    try:
        from opencomputer.profile_analysis_daily import is_dismissed, load_cache
    except ImportError:
        return False

    cache = load_cache()
    if not cache:
        return False
    for s in cache.get("suggestions", []):
        name = s.get("name")
        if name and not is_dismissed(name):
            return True
    return False
