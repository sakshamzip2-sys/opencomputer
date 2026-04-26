"""Layered Awareness MVP — Layer 1 Quick Interview question registry + parser tests."""
from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.quick_interview import (
    QUICK_INTERVIEW_QUESTIONS,
    render_questions,
    parse_answers,
)


def test_default_question_set_has_five():
    assert len(QUICK_INTERVIEW_QUESTIONS) == 5


def test_render_questions_personalizes_with_name():
    facts = IdentityFacts(name="Saksham")
    rendered = render_questions(facts)
    assert "Saksham" in rendered[0]  # greeting includes name


def test_render_questions_anonymous_when_no_name():
    facts = IdentityFacts()
    rendered = render_questions(facts)
    assert "Hi!" in rendered[0] or "Hello" in rendered[0]


def test_parse_answers_returns_dict():
    raw = ["focus: stocks", "concerns: timing", "concise", "no emails", ""]
    parsed = parse_answers(raw)
    assert parsed["current_focus"] == "focus: stocks"
    assert parsed["tone_preference"] == "concise"
    assert "context" not in parsed or parsed["context"] == ""


def test_render_questions_anonymous_when_name_is_whitespace_only():
    """A whitespace-only name should not produce 'Hi    !'."""
    facts = IdentityFacts(name="   ")
    rendered = render_questions(facts)
    # Greeting should NOT contain the literal whitespace name
    assert "Hi    !" not in rendered[0]
    assert "Hi !" not in rendered[0]
    # Should fall through to the anonymous form
    assert "Hi!" in rendered[0]
