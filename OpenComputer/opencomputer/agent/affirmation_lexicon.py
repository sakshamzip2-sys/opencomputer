"""Phase 0 affirmation/correction detector.

Cheap regex on the user's NEXT message after an assistant turn. Used to
populate ``turn_outcomes.affirmation_present`` and
``turn_outcomes.correction_present``.

Bias: lean conservative. Better to under-detect than over-detect, since
the LLM judge in Phase 1 catches semantically equivalent cases the regex
misses. False positives degrade the composite score and hurt the user;
false negatives just leave a Phase 1 signal unused.
"""
from __future__ import annotations

import re

_AFFIRMATION_PATTERNS = [
    r"\bthanks?(?:\s+you)?\b",
    r"\bthank\s+you\b",
    r"\bperfect\b",
    r"\bexactly\b",
    r"\bthat\s*(?:'s|s)?\s*works?(?:ed)?\b",
    r"\b(?:yes|yep|yeah)\s+(?:that(?:'s|s)?)\s+(?:right|correct|it)\b",
    r"\bappreciate\s+(?:it|that)\b",
    r"\bnice(?:\s+work)?\b",
    r"\b(?:works?\s+great|great\s+work)\b",
]

_CORRECTION_PATTERNS = [
    r"\bno(?:t|pe)?\s+(?:that(?:'s|s)?)\s+(?:wrong|not|incorrect)\b",
    r"\bactually\s+i\b",
    r"\bundo\b",
    r"\bincorrect\b",
    r"\bthat(?:'s|s)?\s+not\s+(?:what|right|it)\b",
    r"\bnot\s+(?:quite|really)\s+(?:right|correct)\b",
    r"\bwrong\b",
    r"\bdon'?t\s+(?:do|want)\s+that\b",
]

_AFFIRMATION_RE = re.compile("|".join(_AFFIRMATION_PATTERNS), re.IGNORECASE)
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


def detect_affirmation(message: str) -> bool:
    if not message:
        return False
    return _AFFIRMATION_RE.search(message) is not None


def detect_correction(message: str) -> bool:
    if not message:
        return False
    return _CORRECTION_RE.search(message) is not None
