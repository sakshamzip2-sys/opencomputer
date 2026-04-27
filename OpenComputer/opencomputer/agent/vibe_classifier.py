"""A.4 — Mood classifier for the companion thread.

Per-turn classification of the user's apparent emotional state from
the most recent ~3 messages. Heuristic regex; zero LLM cost,
deterministic, sub-millisecond.

2026-04-28 refactor: implementation now uses
:class:`plugin_sdk.classifier.RegexClassifier` so the per-pattern
table and the FIRST_MATCH evaluation loop live in one shared
abstraction. Public API (``classify_vibe(messages) -> str`` and
``VALID_VIBES``) is unchanged.

Vibes:
    frustrated — "doesn't work", "frustrating", "nothing works"
    excited    — "amazing!", "let's", "love this", multi-!
    tired      — "tired", "exhausted", "going to sleep"
    curious    — "?" patterns, "why", "what if", "tell me more"
    calm       — neutral / default
    stuck      — "I'm stuck", "no idea", "tried everything"

Priority order is encoded in rule order (FIRST_MATCH policy):
stuck > frustrated > excited > tired > curious. Calm is the implicit
fallback when nothing else fires.
"""
from __future__ import annotations

import re

from plugin_sdk.classifier import AggregationPolicy, RegexClassifier, Rule

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


# ── Classifier — order encodes priority ──────────────────────────────


_VIBE_CLASSIFIER: RegexClassifier[str] = RegexClassifier(
    rules=[
        Rule(pattern=_STUCK_RE, label="stuck"),
        Rule(pattern=_FRUSTRATED_RE, label="frustrated"),
        Rule(pattern=_EXCITED_RE, label="excited"),
        Rule(pattern=_TIRED_RE, label="tired"),
        Rule(pattern=_CURIOUS_RE, label="curious"),
    ],
    policy=AggregationPolicy.FIRST_MATCH,
)


def classify_vibe(messages: list[str]) -> str:
    """Return one of :data:`VALID_VIBES` based on the most recent messages.

    Examines up to the last 3 user messages, joins them, and runs the
    heuristic classifier. Falls back to ``calm`` when no pattern fires.
    Empty input returns ``calm``.
    """
    if not messages:
        return "calm"
    blob = "\n".join(m for m in messages[-3:] if isinstance(m, str))
    if not blob.strip():
        return "calm"
    verdict = _VIBE_CLASSIFIER.classify(blob)
    return verdict.top_label or "calm"
