"""PersonaClassifier — heuristic mapping from context to persona id.

Reads (foreground_app, time_of_day, recent_files, last_3_messages) and
returns one of the registered persona ids. Heuristic-based for V2.C — V2.D
may swap in an LLM-based classifier.

Path A.1 (2026-04-27): companion-state-query detector. When the user's
most recent message is a state-query ("how are you", "how are you
feeling", "what's up", etc.) and no STRONG domain signal fires, prefer
the ``companion`` persona over the legacy admin/coding default. The
companion overlay's reflective register is the right form for these
questions; the action-oriented personas suppress warmth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    foreground_app: str = ""
    time_of_day_hour: int = 12
    recent_file_paths: tuple[str, ...] = ()
    last_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    persona_id: str
    confidence: float
    reason: str


_CODING_APPS = ("code", "cursor", "pycharm", "iterm", "terminal", "warp", "neovim")
_TRADING_APPS = ("zerodha", "groww", "kite", "tradingview", "screener", "marketsmojo")
_RELAXED_APPS = ("animepahe", "youtube", "spotify", "netflix", "reddit", "instagram")

#: Regex that detects state-query openings. Anchored at start (after
#: optional punctuation/whitespace) so "how are you doing in this codebase"
#: only matches if the message LEADS with the greeting — random
#: occurrences mid-coding-question don't trigger.
_STATE_QUERY_PATTERN = re.compile(
    r"^[\s\W]*"
    r"(how\s+are\s+you|how\s+(are\s+)?u|"
    r"how\s+(have\s+)?you\s+been|how's\s+it\s+going|how('?s|s)\s+life|"
    r"what'?s\s+up|whats\s+up|sup\b|"
    r"hey\s*(claude|oc|computer)?\b|hi\s*(claude|oc|computer)?\b|hello\b|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"you\s+(doing|feeling)\s+(ok|alright|good)|"
    r"how('?re|\s+are)\s+you\s+holding\s+up|"
    r"(are\s+you\s+)?ok\??\s*$)",
    re.IGNORECASE,
)


def is_state_query(text: str) -> bool:
    """True iff *text* leads with a state-query / greeting / "how are you" pattern.

    Splits on newlines and checks each line independently — a multi-line
    paste like ``source .venv/bin/activate\\nhi`` should match because
    line 2 leads with a greeting.

    Exposed for tests; used internally by :func:`classify`.
    """
    if not text:
        return False
    for line in text.split("\n"):
        if _STATE_QUERY_PATTERN.match(line):
            return True
    return False


def classify(ctx: ClassificationContext) -> ClassificationResult:
    # Path A.1 — state-query detector. Runs FIRST so a "how are you" while
    # in VS Code still goes to companion (the user is engaging socially,
    # not asking about code). Strong app signals (trading, relaxed) still
    # win because those are explicit user-context choices, but the default
    # coding signal yields to companion when the actual message is a
    # state-query.
    # Scan the last up-to-3 messages. State-query in any one of them
    # signals social register. Latest-message-only was too brittle —
    # users often open with "hi" then ask a follow-up like "ok cool".
    state_query = any(is_state_query(m) for m in ctx.last_messages[-3:])
    last_msg = ctx.last_messages[-1] if ctx.last_messages else ""

    app_lower = ctx.foreground_app.lower()
    if any(a in app_lower for a in _TRADING_APPS):
        return ClassificationResult("trading", 0.85, f"foreground app '{ctx.foreground_app}' suggests trading")
    if any(a in app_lower for a in _RELAXED_APPS):
        return ClassificationResult("relaxed", 0.8, f"foreground app '{ctx.foreground_app}' suggests relaxed mode")
    if state_query:
        return ClassificationResult(
            "companion", 0.9,
            f"state-query / greeting detected in last message: {last_msg[:40]!r}",
        )
    if any(a in app_lower for a in _CODING_APPS):
        return ClassificationResult("coding", 0.85, f"foreground app '{ctx.foreground_app}' suggests coding")

    # File-based fallback
    py_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".py"))
    md_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".md"))
    if py_files >= 3:
        return ClassificationResult("coding", 0.7, f"{py_files} recent .py files")
    if md_files >= 3:
        return ClassificationResult("learning", 0.6, f"{md_files} recent .md files")

    # Time-of-day fallback
    if ctx.time_of_day_hour >= 21 or ctx.time_of_day_hour < 6:
        return ClassificationResult("relaxed", 0.5, f"hour={ctx.time_of_day_hour} (evening/late)")
    if 9 <= ctx.time_of_day_hour < 12:
        return ClassificationResult("coding", 0.4, "morning hours, default to coding")

    # Path A.1 — companion as the new default fallback (was admin).
    # State-query detector handles greetings; this catches everything else
    # without strong signal. The companion overlay is warm-but-honest;
    # admin's overlay is action-only.
    return ClassificationResult("companion", 0.3, "no strong signal — default companion")
