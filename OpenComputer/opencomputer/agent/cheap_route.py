"""Heuristic classifier for 'this is a cheap prompt' → route to cheaper model.

Borrowed from Hermes's cheap-route pattern. Pure function, no I/O, no
provider calls. Applies ONLY to the first turn of a conversation — once
tool-calls start, we're in a multi-turn flow where a cheap model's
capability gap could cascade into failures.
"""

from __future__ import annotations

import re

#: Default character threshold. Short-prompt-land.
DEFAULT_CHEAP_MAX_CHARS = 160

#: Keywords that disqualify a prompt from the cheap route. If ANY matches,
#: we stay on the main model. Regex-based case-insensitive word match so
#: "fix" matches but "prefix" doesn't.
_DISQUALIFYING_KEYWORDS = (
    "code",
    "implement",
    "debug",
    "review",
    "refactor",
    "test",
    "write",
    "edit",
    "fix",
    "create",
    "build",
    "compile",
    "deploy",
    "run",
    "execute",
    "analyze",
    "explain code",
    "design",
)

#: Regex: matches a URL.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

#: Regex: matches any triple-backtick fence (code block start/end).
_CODE_FENCE_RE = re.compile(r"```")

#: Regex: matches a keyword as a whole word.
_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _DISQUALIFYING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def should_route_cheap(
    user_message: str,
    *,
    max_chars: int = DEFAULT_CHEAP_MAX_CHARS,
) -> bool:
    """Return True if the user's first-turn message is simple enough for
    the cheap model.

    Disqualifiers (any one → False):
    - Length > max_chars
    - Contains a URL
    - Contains a triple-backtick code fence
    - Contains one of the disqualifying keywords (whole-word match)
    """
    if len(user_message) > max_chars:
        return False
    if _URL_RE.search(user_message):
        return False
    if _CODE_FENCE_RE.search(user_message):
        return False
    return not _KEYWORD_RE.search(user_message)


__all__ = ["should_route_cheap", "DEFAULT_CHEAP_MAX_CHARS"]
