"""A.4 — Mood classifier for the companion thread.

After each turn, classify the user's apparent emotional state from the
last ~3 messages. Uses heuristic keyword matching for offline/zero-cost
operation; a future iteration could swap in a cheap-route LLM call for
nuanced cases.

Vibes:
    frustrated — short, capital, repeated negation, "doesn't work", "stuck"
    excited    — exclamations, "let's", "amazing", "love this"
    tired      — "tired", "sleepy", late-night timestamp + short messages
    curious    — "?" patterns, "why", "what if", "tell me more"
    calm       — neutral declarative + acknowledgments
    stuck      — "I'm stuck", "no idea", repeated similar questions

The classification is BEST-EFFORT and ADVISORY. Wrong values give the
companion bad anchors — they don't break anything else.
"""
from __future__ import annotations

import re

#: The supported vibe vocabulary. Caller must use one of these.
VALID_VIBES: tuple[str, ...] = (
    "frustrated",
    "excited",
    "tired",
    "curious",
    "calm",
    "stuck",
)


# ── Heuristic patterns ────────────────────────────────────────────────


_FRUSTRATED_RE = re.compile(
    r"\b("
    r"doesn'?t\s+work|won'?t\s+work|broken|wtf|fuck|shit|damn|"
    r"why\s+isn'?t|why\s+won'?t|"
    r"keep\s+(getting|seeing|hitting)|still\s+(failing|broken|wrong)|"
    r"can'?t\s+(get|figure|make|find)|nothing\s+works|"
    r"frustrating|annoying"
    r")\b",
    re.IGNORECASE,
)

_EXCITED_RE = re.compile(
    r"("
    r"\b(amazing|awesome|fantastic|incredible|love\s+(this|it)|"
    r"let'?s\s+(go|do|build|ship)|finally|works!|that'?s\s+great|"
    r"this\s+is\s+(great|awesome)|so\s+(cool|good))\b"
    r"|!{2,}"
    r")",
    re.IGNORECASE,
)

_TIRED_RE = re.compile(
    r"\b("
    r"tired|exhausted|sleepy|burnt\s+out|burned\s+out|wiped|"
    r"long\s+day|tomorrow|going\s+to\s+sleep|gonna\s+sleep"
    r")\b",
    re.IGNORECASE,
)

_CURIOUS_RE = re.compile(
    r"\b("
    r"why\s+do(es)?|how\s+come|what\s+if|interesting|tell\s+me\s+more|"
    r"does\s+(this|it)\s+work|how\s+does|wondering|curious"
    r")\b",
    re.IGNORECASE,
)

_STUCK_RE = re.compile(
    r"\b("
    r"i'?m\s+stuck|completely\s+stuck|no\s+idea|don'?t\s+know\s+(what|how|where)|"
    r"i'?ve\s+tried\s+everything|been\s+at\s+this|hours\s+(now|on\s+this)"
    r")\b",
    re.IGNORECASE,
)


def classify_vibe(messages: list[str]) -> str:
    """Return one of :data:`VALID_VIBES` based on the most recent messages.

    Examines up to the last 3 user messages, joins them, and runs the
    heuristic regexes in priority order. Falls back to ``calm`` when no
    pattern fires. Empty input returns ``calm``.

    Priority order is deliberate:
    - ``stuck`` and ``frustrated`` are URGENT signals — the companion
      should know first.
    - ``excited`` next (positive surprise).
    - ``tired`` next (worth flagging).
    - ``curious`` next (default for engaged learning).
    - ``calm`` is the last resort.
    """
    if not messages:
        return "calm"
    blob = "\n".join(m for m in messages[-3:] if isinstance(m, str))
    if not blob.strip():
        return "calm"
    if _STUCK_RE.search(blob):
        return "stuck"
    if _FRUSTRATED_RE.search(blob):
        return "frustrated"
    if _EXCITED_RE.search(blob):
        return "excited"
    if _TIRED_RE.search(blob):
        return "tired"
    if _CURIOUS_RE.search(blob):
        return "curious"
    return "calm"
