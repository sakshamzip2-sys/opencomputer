"""Heuristic post-turn classifier for life-event hint responses.

When a life-event pattern surfaces a gentle hint ("Hope you're doing okay
this week — if you want to talk through anything, I'm here.") the framework
schedules a one-shot follow-up cron. The user's *next* reply tells us whether
that follow-up is still wanted:

- The user clearly says they're fine            → ``"refuted"``  (cancel the cron)
- The user clearly confirms the rough patch     → ``"confirmed"`` (keep the cron)
- Anything else — ambiguous, off-topic, empty   → ``"unclear"``  (keep the cron)

``classify_response`` is a **pure function** — no I/O, no state, no LLM. It is
called by :func:`on_stop_hook`, the post-turn STOP-hook orchestrator, which
owns the cron-cancel side effect.

The surfacing-turn timing problem
---------------------------------
A life-event hint is injected by ``LifeEventInjectionProvider.collect()`` at
turn N. The agent's turn-N reply acknowledges the life event; the user
responds to *that* at turn **N+1**. But the STOP hook fires at the end of
EVERY turn — including turn N's own.

So :func:`on_stop_hook` must NOT classify the surfacing turn's own message:
that message was typed by the user *before* they saw any hint-influenced
reply. Classifying it would clear ``verdict_pending`` before the real
reply-to-the-hint is ever judged — and a coincidental refutation phrase in
that pre-hint message could wrongly cancel the cron.

The fix is a turn-index comparison. ``schedule_followup`` records the turn
the hint surfaced on (``surfaced_turn`` in ``life_event_state.json``); the
STOP :class:`~plugin_sdk.hooks.HookContext` carries the *current* turn index.
:func:`on_stop_hook` classifies a verdict-pending pattern ONLY when the
current turn is STRICTLY LATER than that pattern's ``surfaced_turn`` — i.e.
on turn N+1 or later, never on turn N.

v1 is deliberately heuristic (lowercased substring matching). An LLM-backed
classifier — which would actually *infer* sentiment rather than pattern-match
phrases — is a documented v2 enhancement and is explicitly out of scope here.

Design bias — CONSERVATISM
--------------------------
Only a CLEAR refutation cancels. The failure costs are asymmetric:

- A false ``"refuted"`` cancels a check-in the user actually wanted — bad.
- A missed refutation merely leaves a gentle one-shot cron that fires once
  and then is gone — mild.

So when in doubt the classifier returns ``"unclear"``, never ``"refuted"``.

The negation pitfall
--------------------
A naive ``if phrase in text`` backfires for *positive* refutation phrases:
``"i'm not doing well"`` contains the refutation phrase ``"doing well"``, so a
clearly-distressed reply would be mis-flagged ``"refuted"`` and wrongly cancel
the check-in. Positive refutation phrases are therefore only honoured when no
negator immediately precedes them. Phrases that are *themselves* negations of
distress (``"not stressed"``, ``"nothing's wrong"``) need no such guard — the
negator is part of the phrase.
"""
from __future__ import annotations

import logging
from typing import Literal

from opencomputer.awareness.life_events import actions, state
from plugin_sdk.hooks import HookContext

Verdict = Literal["refuted", "confirmed", "unclear"]

# Negators that flip a positive refutation phrase ("doing well" → "not doing
# well"). Matched as a token-ish suffix immediately before the phrase.
_NEGATORS: tuple[str, ...] = ("not", "n't", "never", "no", "hardly", "barely")

# Positive-statement refutations: the phrase asserts wellbeing, so a preceding
# negator inverts its meaning. Guarded against the negation pitfall.
_POSITIVE_REFUTATIONS: frozenset[str] = frozenset(
    {
        "i'm fine",
        "im fine",
        "i am fine",
        "i'm ok",
        "im ok",
        "i'm okay",
        "im okay",
        "i'm good",
        "im good",
        "i'm alright",
        "im alright",
        "i'm great",
        "im great",
        # Intensifier variants — "totally fine", "perfectly ok", … — common
        # enough that requiring an exact "i'm fine" would miss real
        # refutations like "I'm totally fine".
        "totally fine",
        "totally ok",
        "totally okay",
        "perfectly fine",
        "perfectly ok",
        "perfectly okay",
        "completely fine",
        "really fine",
        "really ok",
        "really okay",
        "absolutely fine",
        "all good",
        "doing well",
        "doing fine",
        "doing okay",
        "doing great",
        "feeling fine",
        "feeling good",
        "feeling great",
        "everything's fine",
        "everything is fine",
        "everything's ok",
    }
)

# Refutations that are themselves negations of distress. The negator is baked
# into the phrase, so NO negation guard applies — guarding these would make
# "not stressed" require a *second* "not" before it, which is wrong.
_NEGATIVE_DISTRESS_REFUTATIONS: frozenset[str] = frozenset(
    {
        "not stressed",
        "not worried",
        "not struggling",
        "not burnt out",
        "not burned out",
        "not overwhelmed",
        "not upset",
        "nothing's wrong",
        "nothing is wrong",
        "nothing wrong",
        "no reason to worry",
        "nothing to worry about",
        "no need to worry",
    }
)

# Direct rebuttals — the user disputes the inference itself. Unguarded: there
# is no plausible distressed reading of "you're wrong" / "you've misread me".
_DIRECT_REBUTTALS: frozenset[str] = frozenset(
    {
        "you're wrong",
        "youre wrong",
        "you are wrong",
        "you're mistaken",
        "youre mistaken",
        "that's not right",
        "thats not right",
        "you've misread",
        "youve misread",
        "you misread",
        "you've got it wrong",
        "youve got it wrong",
    }
)

# Confirmation phrases — lighter set. The user acknowledges a rough patch.
# Conservatism applies in reverse too: this set stays small so we don't
# over-claim "confirmed", but a false "confirmed" is harmless here (it only
# keeps a cron that would fire once anyway).
#
# Bare distress words ("stressed", "struggling") are negation-guarded: in a
# refutation like "i'm not stressed" the word "stressed" must NOT register as
# a confirmation, or it would veto the (correct) refutation verdict.
_BARE_DISTRESS_CONFIRMATIONS: frozenset[str] = frozenset(
    {
        "struggling",
        "stressed",
        "burnt out",
        "burned out",
        "burnout",
        "overwhelmed",
        "exhausted",
        "drained",
        "miserable",
    }
)

# Confirmation phrases that are NOT bare distress words — either neutral
# ("been rough") or already-negated ("not great"). No negation guard applies:
# guarding "not great" would demand a second negator before it.
_PHRASE_CONFIRMATIONS: frozenset[str] = frozenset(
    {
        "been rough",
        "it's rough",
        "its rough",
        "been hard",
        "it's hard",
        "its hard",
        "been tough",
        "it's tough",
        "its tough",
        "been difficult",
        "not great",
        "not doing well",
        "not doing great",
        "not okay",
        "not ok",
        "not good",
        "not fine",
        "having a hard time",
        "rough patch",
        "going through a lot",
        "could be better",
        "barely holding",
    }
)


# How many whole words immediately before a positive refutation phrase to
# scan for a negator. A negator is often separated from the phrase it negates
# by an adverb or filler word ("not REALLY doing well", "not CURRENTLY doing
# well", "not, LIKE, doing well") — checking only the single adjacent word
# misses those and yields a false "refuted". Four words covers up to three
# intervening adverbs/fillers while staying short enough that a negator in an
# unrelated earlier clause does not bleed across and over-trigger the guard.
_NEGATOR_WINDOW: int = 4


def _negated(text: str, start: int) -> bool:
    """Return True if a negator appears just before the phrase at ``start``.

    Scans the last ``_NEGATOR_WINDOW`` whole words preceding ``start``; if any
    is a negator (or ends in ``n't``), the positive phrase is inverted and must
    not count as a refutation. The window — rather than the single adjacent
    word — catches negators split from their phrase by an intervening adverb or
    filler word ("not really doing well", "not, like, doing well").
    """
    before = text[:start].rstrip()
    if not before:
        return False
    # Last few whole words before the phrase, punctuation stripped.
    window = [
        word.strip(".,;:!?\"'()") for word in before.rsplit(None, _NEGATOR_WINDOW)[-_NEGATOR_WINDOW:]
    ]
    for word in window:
        if word in _NEGATORS:
            return True
        # Contraction case: "don't", "doesn't", "isn't", "wasn't", "aren't", …
        if word.endswith("n't"):
            return True
    return False


def _contains_unnegated(text: str, phrases: frozenset[str]) -> bool:
    """True if any phrase appears in ``text`` without a preceding negator."""
    for phrase in phrases:
        idx = text.find(phrase)
        while idx != -1:
            if not _negated(text, idx):
                return True
            idx = text.find(phrase, idx + 1)
    return False


def _contains_any(text: str, phrases: frozenset[str]) -> bool:
    """True if any phrase is a plain substring of ``text`` (no negation guard)."""
    return any(phrase in text for phrase in phrases)


def classify_response(user_text: str, pattern_id: str) -> Verdict:
    """Classify the user's reply to a life-event hint.

    Args:
        user_text: The user's free-text reply to the hint.
        pattern_id: The life-event pattern that surfaced the hint (e.g.
            ``"burnout"``). Part of the signature for Task 7 and the future
            v2 LLM-backed classifier; the v1 heuristic uses global phrase
            sets and does not branch on it.

    Returns:
        ``"refuted"``   — a clear refutation; the follow-up cron should cancel.
        ``"confirmed"`` — the user confirmed the rough patch; keep the cron.
        ``"unclear"``   — ambiguous / off-topic / empty; keep the cron. This
                          is the conservative default whenever there is doubt.
    """
    del pattern_id  # v1 heuristic is pattern-agnostic; kept for v2 / Task 7.

    text = user_text.strip().lower()
    if not text:
        return "unclear"

    # Refutation: any of the three refutation families, with the negation
    # guard applied only to the positive-statement set.
    refuted = (
        _contains_unnegated(text, _POSITIVE_REFUTATIONS)
        or _contains_any(text, _NEGATIVE_DISTRESS_REFUTATIONS)
        or _contains_any(text, _DIRECT_REBUTTALS)
    )

    # A reply can technically contain both a refutation phrase and a
    # confirmation phrase ("I'm fine but it's been rough"). That is genuine
    # ambiguity — and conservatism says an ambiguous reply must NOT cancel
    # the check-in. So a confirmation signal vetoes the refutation.
    #
    # Bare distress words are negation-guarded — "i'm not stressed" must not
    # let "stressed" veto the refutation. Phrase confirmations are matched
    # plainly (they are neutral or already-negated).
    confirmed = _contains_unnegated(text, _BARE_DISTRESS_CONFIRMATIONS) or _contains_any(
        text, _PHRASE_CONFIRMATIONS
    )

    if refuted and not confirmed:
        return "refuted"
    if confirmed:
        return "confirmed"
    return "unclear"


# ── STOP-hook orchestrator ─────────────────────────────────────────────

_log = logging.getLogger(__name__)


def _last_user_text(ctx: HookContext) -> str:
    """Return the most recent user message text from a STOP HookContext.

    The STOP :class:`HookContext` carries ``messages`` — the conversation
    history for the turn. The reply :func:`on_stop_hook` must judge is the
    last ``role == "user"`` message in that list. Returns ``""`` when there
    is no message history or no user message (the classifier treats an
    empty string as ``"unclear"``).
    """
    messages = ctx.messages or []
    for msg in reversed(messages):
        if getattr(msg, "role", None) == "user":
            content = getattr(msg, "content", "")
            return content if isinstance(content, str) else ""
    return ""


async def on_stop_hook(ctx: HookContext) -> None:
    """Post-turn STOP-hook handler — judge the reply to a life-event hint.

    Registered for :data:`~plugin_sdk.hooks.HookEvent.STOP`, so it fires at
    the end of every turn. For each verdict-pending life-event pattern it
    classifies the user's most recent reply and self-corrects the follow-up
    cron:

    - A **refuting** reply → :func:`actions.cancel_followup` (delete the
      cron + clear the entry): the user said they're fine, drop the tooth.
    - A **confirming** / **unclear** reply →
      :func:`state.clear_verdict_pending` (keep the cron + entry, just
      clear the flag): the reply has been judged, the gentle check-in
      still fires.

    The surfacing turn is SKIPPED. ``schedule_followup`` records the turn
    the hint surfaced on (``surfaced_turn``); this handler classifies a
    pattern ONLY when ``ctx.turn_index`` is strictly later than that — the
    user's reply to a hint lands on the turn *after* the one it surfaced
    on, never the same turn. A missing/zero ``surfaced_turn`` (pre-Task-7
    entries) reads as turn 0, so a legacy entry's next reply is still
    judged rather than ignored forever.

    Fail-open: the whole body is wrapped in ``try``/``except``. A classifier
    or state error must NEVER wedge the turn — it is logged at WARNING and
    the cron is LEFT untouched (an error must never mis-cancel a wanted
    check-in). Returns ``None`` always — STOP handlers are observers.
    """
    try:
        pending = state.verdict_pending_patterns()
        if not pending:
            return  # common case — most turns have nothing pending

        current_turn = ctx.turn_index
        full_state = state.load_state()

        for pattern_id in pending:
            entry = full_state.get(pattern_id)
            surfaced_turn = (
                int(entry.get("surfaced_turn", 0))
                if isinstance(entry, dict)
                else 0
            )
            # Skip the surfacing turn itself — the user has not yet replied
            # to the hint. Only a STRICTLY LATER turn carries the reply.
            if current_turn <= surfaced_turn:
                _log.debug(
                    "life-event STOP: skipping %s — current turn %s is not "
                    "past surfaced turn %s (no reply to judge yet)",
                    pattern_id,
                    current_turn,
                    surfaced_turn,
                )
                continue

            verdict = classify_response(_last_user_text(ctx), pattern_id)
            if verdict == "refuted":
                # The user said they're fine — drop the whole tooth.
                actions.cancel_followup(pattern_id)
                _log.info(
                    "life-event STOP: %s refuted by the user's reply — "
                    "cancelled the follow-up cron",
                    pattern_id,
                )
            else:
                # "confirmed" / "unclear" — keep the cron, the reply has
                # been judged so it is no longer verdict-pending.
                state.clear_verdict_pending(pattern_id)
                _log.debug(
                    "life-event STOP: %s reply classified %s — kept the "
                    "follow-up cron, cleared verdict_pending",
                    pattern_id,
                    verdict,
                )
    except Exception:  # noqa: BLE001 — fail-open; never wedge the turn
        _log.warning(
            "life-event STOP hook failed; leaving any follow-up crons "
            "untouched (an error must never mis-cancel a check-in)",
            exc_info=True,
        )


# Process-wide guard — the hook ``engine`` is a singleton, so the STOP
# handler must be registered exactly once. ``AgentLoop`` is constructed
# per session and on every surface (CLI / gateway / wire / webui), so the
# registration call lives in ``AgentLoop.__init__`` but is idempotent via
# this flag.
_STOP_HOOK_REGISTERED: bool = False


def register_life_event_stop_hook() -> None:
    """Register :func:`on_stop_hook` for :data:`HookEvent.STOP`, once.

    Idempotent: a process-wide flag means repeated calls (one per
    ``AgentLoop`` construction) register the handler exactly once against
    the singleton hook engine. The handler is fire-and-forget — STOP is a
    post-turn observer event and :func:`on_stop_hook` never blocks or gates
    the turn.

    Wrapped in a broad ``try``/``except``: a registration failure must not
    break ``AgentLoop`` construction — the life-event self-correction
    feature simply stays dormant.
    """
    global _STOP_HOOK_REGISTERED
    if _STOP_HOOK_REGISTERED:
        return
    try:
        from opencomputer.hooks.engine import engine
        from plugin_sdk.hooks import HookEvent, HookSpec

        engine.register(
            HookSpec(
                event=HookEvent.STOP,
                handler=on_stop_hook,
                fire_and_forget=True,
            )
        )
        _STOP_HOOK_REGISTERED = True
    except Exception:  # noqa: BLE001 — never break AgentLoop construction
        _log.warning(
            "failed to register the life-event STOP hook; life-event "
            "self-correction will be inactive this process",
            exc_info=True,
        )


__all__ = [
    "Verdict",
    "classify_response",
    "on_stop_hook",
    "register_life_event_stop_hook",
]
