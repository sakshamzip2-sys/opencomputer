"""Heuristic post-turn classifier for life-event hint responses.

When a life-event pattern surfaces a gentle hint ("Hope you're doing okay
this week — if you want to talk through anything, I'm here.") the framework
schedules a one-shot follow-up cron. The user's *next* reply tells us whether
that follow-up is still wanted:

- The user clearly says they're fine            → ``"refuted"``  (cancel the cron)
- The user clearly confirms the rough patch     → ``"confirmed"`` (keep the cron)
- Anything else — ambiguous, off-topic, empty   → ``"unclear"``  (keep the cron)

``classify_response`` is a **pure function** — no I/O, no state, no LLM. It is
called by the STOP hook (Task 7), which owns the cron-cancel side effect.

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

from typing import Literal

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
