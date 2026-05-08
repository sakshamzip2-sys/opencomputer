"""Tests for the chat-reply approval-keyword classifier."""
from __future__ import annotations

import pytest

from opencomputer.security.approval_keywords import classify_reply


@pytest.mark.parametrize(
    "text",
    ["yes", "y", "Yes", "Y", "YES", " yes ", "approve", "approved", "ok",
     "OK", "okay", "go", "allow", "permit", "yes!", "yes."],
)
def test_approve_keywords(text: str):
    assert classify_reply(text) == "approve"


@pytest.mark.parametrize(
    "text",
    ["no", "n", "No", "N", "NO", " no ", "deny", "denied", "cancel",
     "stop", "block", "refuse", "no!", "no."],
)
def test_deny_keywords(text: str):
    assert classify_reply(text) == "deny"


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "yes please",  # multi-token
        "no thanks",  # multi-token
        "what?",  # punctuation but not an approval token
        "ya",  # slang, not exact
        "nah",  # slang, not exact
        "k",  # too ambiguous
        "yeah",  # not in list
        "hello",  # benign
        "yes/no",  # ambiguous
        "do it",  # phrase
        "deny that",  # multi-token
    ],
)
def test_no_match(text: str):
    assert classify_reply(text) is None
