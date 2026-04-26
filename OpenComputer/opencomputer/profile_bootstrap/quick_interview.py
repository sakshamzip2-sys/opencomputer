"""Layer 1 — Quick Interview.

Renders 5 install-time questions personalized using Layer 0 identity,
parses the user's answers back into a structured dict that
:func:`opencomputer.profile_bootstrap.persistence.write_interview_answers_to_graph`
can persist.

The CLI orchestration lives in :mod:`opencomputer.cli_profile`; this
module is testable in isolation.

Index contract for :func:`render_questions` return value
---------------------------------------------------------
- ``rendered[0]`` — greeting string (includes name if ``facts.name`` is set)
- ``rendered[1]`` through ``rendered[5]`` — question prompts in registry order

This index contract is stable: the CLI implementer depends on index 0
being the greeting and indices 1–5 being questions.
"""
from __future__ import annotations

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts

#: Ordered tuple of (key, prompt-template) pairs. Order matters — the
#: CLI presents them sequentially. ``parse_answers`` uses
#: ``zip(QUICK_INTERVIEW_QUESTIONS, raw_answers)``, so positional order
#: IS the key-to-answer mapping. Adding, removing, or reordering changes
#: the contract — update the count assertion in
#: ``test_default_question_set_has_five`` AND audit any callers of
#: ``parse_answers`` for shape assumptions.
QUICK_INTERVIEW_QUESTIONS: tuple[tuple[str, str], ...] = (
    (
        "current_focus",
        "What are you working on this week? (one sentence is fine)",
    ),
    (
        "current_concerns",
        "Anything on your mind right now I should know?",
    ),
    (
        "tone_preference",
        "How do you prefer responses — concise/action-first or thorough?",
    ),
    (
        "do_not",
        "Anything I should NOT do without asking? (e.g. \"never send emails without confirming\")",
    ),
    (
        "context",
        "Anything else about you that would help me help you?",
    ),
)


def render_questions(facts: IdentityFacts) -> list[str]:
    """Return [greeting, q1, q2, ...] strings ready for the CLI to present.

    Index 0 is the personalized (or anonymous) greeting; indices 1-5
    are the question prompts in :data:`QUICK_INTERVIEW_QUESTIONS` order.
    """
    name = (facts.name or "").strip()
    if name:
        greeting = (
            f"Hi {name}! I'm OpenComputer — your local agent.\n"
            "Five quick questions so I can be useful from the get-go:"
        )
    else:
        greeting = (
            "Hi! I'm OpenComputer — your local agent.\n"
            "Five quick questions so I can be useful from the get-go:"
        )
    return [greeting, *(q for _, q in QUICK_INTERVIEW_QUESTIONS)]


def parse_answers(raw_answers: list[str]) -> dict[str, str]:
    """Map raw answer strings (in question order) back to a keyed dict.

    Empty answers (blank or whitespace-only) are dropped — they are not
    included in the returned dict at all. Extra answers beyond the
    registry length are discarded; fewer answers than registry entries
    causes ``zip`` to stop at the shorter iterable, so unanswered
    questions simply produce no entry.

    The caller (CLI) is the contract enforcer for length — this function
    only maps what it receives.
    """
    parsed: dict[str, str] = {}
    for (key, _), answer in zip(QUICK_INTERVIEW_QUESTIONS, raw_answers):
        cleaned = answer.strip()
        if cleaned:
            parsed[key] = cleaned
    return parsed
