"""P0-2: regex affirmation/correction detector tests.

Conservative bias: better to under-detect than over-detect, since the
LLM judge in Phase 1 catches semantically equivalent cases the regex
misses. False positives degrade the composite score and hurt the user;
false negatives just leave a Phase 1 signal unused.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.affirmation_lexicon import (
    detect_affirmation,
    detect_correction,
)


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("thanks!", True),
        ("thank you so much", True),
        ("perfect, that works", True),
        ("exactly what I wanted", True),
        ("yes that's right", True),
        ("yes thats right", True),
        ("appreciate it", True),
        ("nice work", True),
        ("hmm interesting", False),
        ("can you do X", False),
        ("what time is it", False),
        ("", False),
    ],
)
def test_affirmation(msg, expected):
    assert detect_affirmation(msg) is expected


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("no that's wrong", True),
        ("no thats wrong", True),
        ("actually I meant Y", True),
        ("undo that", True),
        ("incorrect", True),
        ("that's not what I asked", True),
        ("not quite right", True),
        ("don't do that", True),
        ("ok cool", False),
        ("thanks", False),
        ("", False),
        ("you weren't wrong", True),  # honest false positive — 'wrong' alone hits
    ],
)
def test_correction(msg, expected):
    assert detect_correction(msg) is expected


def test_neither_signal_in_neutral_message():
    msg = "what time is the meeting"
    assert detect_affirmation(msg) is False
    assert detect_correction(msg) is False


def test_both_signals_can_co_occur():
    """Composite scorer treats them additively. The lexicons are
    independent — a message can be both affirming AND correcting."""
    msg = "thanks but actually that's wrong"
    assert detect_affirmation(msg) is True
    assert detect_correction(msg) is True


def test_case_insensitive():
    assert detect_affirmation("THANK YOU") is True
    assert detect_correction("THAT'S WRONG") is True
