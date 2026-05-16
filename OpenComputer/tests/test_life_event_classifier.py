"""Tests for the heuristic post-turn life-event response classifier.

These are pure-function tests — no monkeypatch, no profile isolation.
The classifier judges whether the user's reply to a gentle life-event hint
REFUTES the inference (cancel the follow-up cron), CONFIRMS it, or is UNCLEAR.

The conservatism bias is the load-bearing property: an ambiguous reply must
return ``"unclear"`` — never ``"refuted"`` — because a false ``"refuted"``
wrongly cancels a wanted check-in, while a missed refutation merely leaves a
gentle one-shot cron.
"""
from opencomputer.awareness.life_events.classifier import classify_response

# ── The three plan cases ──────────────────────────────────────────────


def test_plan_case_clear_refutation():
    assert classify_response("I'm totally fine, not stressed", "burnout") == "refuted"


def test_plan_case_rough_reply_is_not_refuted():
    # "yeah it's been rough" must NOT be classified as a refutation.
    assert classify_response("yeah it's been rough", "burnout") in {
        "confirmed",
        "unclear",
    }


def test_plan_case_empty_reply_is_unclear():
    assert classify_response("", "burnout") == "unclear"


# ── Clear refutations — one assertion per refutation phrase ────────────


def test_refutation_im_fine():
    assert classify_response("I'm fine, really", "burnout") == "refuted"


def test_refutation_im_ok():
    assert classify_response("honestly I'm ok", "exam_prep") == "refuted"


def test_refutation_not_stressed():
    assert classify_response("I'm not stressed at all", "burnout") == "refuted"


def test_refutation_nothings_wrong():
    assert classify_response("nothing's wrong, why do you ask", "health_event") == "refuted"


def test_refutation_all_good():
    assert classify_response("all good here", "travel") == "refuted"


def test_refutation_doing_well():
    assert classify_response("I'm doing well, thanks", "job_change") == "refuted"


def test_refutation_youre_wrong():
    assert classify_response("you're wrong about that", "relationship_shift") == "refuted"


def test_refutation_no_reason_to_worry():
    assert classify_response("no reason to worry", "burnout") == "refuted"


def test_refutation_is_case_insensitive():
    assert classify_response("I'M TOTALLY FINE", "burnout") == "refuted"


# ── The negation pitfall — distressed replies that CONTAIN a positive
#    refutation phrase must NOT be classified "refuted" ─────────────────


def test_negation_pitfall_not_doing_well():
    # "doing well" is a refutation phrase, but this reply is clearly
    # distressed. A naive `if phrase in text` would mis-flag it.
    assert classify_response("honestly i'm not doing well at all", "burnout") != "refuted"


def test_negation_pitfall_not_all_good():
    assert classify_response("things are not all good right now", "burnout") != "refuted"


def test_negation_pitfall_dont_feel_fine():
    assert classify_response("i don't feel fine honestly", "burnout") != "refuted"


def test_negation_pitfall_never_been_okay():
    assert classify_response("i've never been okay with this", "relationship_shift") != "refuted"


def test_negation_pitfall_isnt_all_good():
    assert classify_response("it isn't all good", "burnout") != "refuted"


# ── The negator-window pitfall — a negator separated from the positive
#    phrase by an intervening adverb / filler still inverts it ──────────


def test_negation_window_not_really_doing_well():
    # "really" sits between "not" and "doing well"; the negator must
    # still flip the positive phrase.
    assert classify_response("i'm not really doing well", "burnout") != "refuted"


def test_negation_window_not_currently_doing_well():
    assert classify_response("not currently doing well", "burnout") != "refuted"


def test_negation_window_not_like_doing_well():
    # Filler-word case: "not, like, doing well".
    assert classify_response("i'm not, like, doing well", "burnout") != "refuted"


# ── Clear confirmations ───────────────────────────────────────────────


def test_confirmation_yeah_struggling():
    assert classify_response("yeah, I've been struggling lately", "burnout") == "confirmed"


def test_confirmation_been_hard():
    assert classify_response("it's been hard, honestly", "burnout") == "confirmed"


def test_confirmation_overwhelmed():
    assert classify_response("I feel really overwhelmed", "exam_prep") == "confirmed"


def test_confirmation_burnt_out():
    assert classify_response("I'm pretty burnt out", "burnout") == "confirmed"


# ── Unclear — empty / whitespace / unrelated / ambiguous ──────────────


def test_whitespace_only_reply_is_unclear():
    assert classify_response("   \n\t  ", "burnout") == "unclear"


def test_unrelated_reply_is_unclear():
    assert classify_response("what's the weather", "burnout") == "unclear"


def test_off_topic_reply_is_unclear():
    assert classify_response("can you open the file in src/main.py", "exam_prep") == "unclear"


def test_ambiguous_reply_is_unclear_not_refuted():
    # No refutation phrase, no confirmation phrase → must be "unclear",
    # the conservative default — NOT "refuted".
    assert classify_response("hmm, maybe", "burnout") == "unclear"


def test_return_value_is_always_one_of_three():
    for text in ["I'm fine", "it's been rough", "", "what's the weather", "maybe"]:
        assert classify_response(text, "burnout") in {"refuted", "confirmed", "unclear"}
