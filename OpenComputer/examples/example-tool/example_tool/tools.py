"""WordCount — count words / sentences / chars in a text snippet.

The simplest possible useful tool. Replace this with whatever your
plugin actually does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")
_WORD_RE = re.compile(r"\b\w+\b")


@dataclass(frozen=True)
class WordCountResult:
    chars: int
    words: int
    sentences: int


def count(text: str) -> WordCountResult:
    """Count chars / words / sentences in ``text``."""
    chars = len(text)
    words = len(_WORD_RE.findall(text))
    # Treat any non-empty text as at least one sentence.
    sentences = max(1, len(_SENTENCE_RE.findall(text))) if text.strip() else 0
    return WordCountResult(chars=chars, words=words, sentences=sentences)


__all__ = ["WordCountResult", "count"]
