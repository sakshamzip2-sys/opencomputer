"""Tag extraction — pulls keyword-ish tags out of the user's message.

This is the v0 implementation: simple lowercased word extraction with
stopword filtering. Good enough to wire pre-task lookup end-to-end
against the local-file backend, but the tags it produces aren't
abstract concepts (``#homelab`` etc.) — they're literal words from the
user's request.

The LLM upgrade lands in Phase 8: a Haiku call rewrites the user
message into 3-5 abstract domain tags. This module's API stays the
same so swapping the implementation is a one-file change.

Why not just use the user's words verbatim until then? Because the
local backend's ``score_trace`` function in
``client/local_file.py`` matches by tag set intersection, and we need
SOMETHING in the input ``tags`` tuple for the dev-stub demo to work.
Word extraction with stopword filtering gets us there.
"""

from __future__ import annotations

#: Common English noise words. NOT exhaustive — just enough to keep
#: extracted tags from being dominated by ``the``/``and``/``can you``.
#: Keep this list short; aggressive stopword filtering would defeat the
#: purpose by stripping legitimate domain words. The Phase 8 LLM
#: upgrade replaces this whole function.
_STOPWORDS: frozenset[str] = frozenset({
    "and", "but", "for", "with", "from", "into", "onto", "between",
    "this", "that", "these", "those", "what", "which", "where", "when",
    "while", "have", "has", "had", "does", "did", "will", "would",
    "could", "should", "must", "shall", "been", "being", "are", "were",
    "the", "you", "your", "they", "their", "them", "ours", "yours",
    "please", "help", "want", "need", "needs", "needed", "make",
    "made", "using", "use", "used", "way", "ways", "thing", "things",
    "something", "anything", "everything", "nothing", "much", "many",
    "very", "just", "also", "then", "than", "such", "only", "still",
    "really", "actually", "basically", "quite", "okay", "sure",
    "let", "lets", "let's", "going", "got", "get", "able", "should",
    "want", "wanted", "needs", "look", "looking", "seem", "seems",
    "tell", "told", "give", "gave", "show", "showed", "shown", "say",
    "said", "know", "knew", "known", "think", "thought", "feel",
    "felt", "find", "found", "ask", "asked", "wonder", "wondered",
})

#: Minimum length for a word to qualify as a tag. Drops noise like
#: ``a``, ``of``, ``is`` without needing them in the stopword list.
_MIN_TAG_LEN: int = 4

#: Cap on how many tags we extract per message. Tag-set intersection
#: with the inbox is the matcher in v0; too many tags creates noise
#: matches. Phase 8 with LLM-derived tags can probably go lower (3-5).
_DEFAULT_MAX_TAGS: int = 8


def extract_tags_from_message(
    text: str,
    *,
    max_tags: int = _DEFAULT_MAX_TAGS,
) -> tuple[str, ...]:
    """Return up to ``max_tags`` tag strings extracted from ``text``.

    Algorithm (v0):

    1. Lowercase, replace non-alphanumeric with spaces, split on
       whitespace.
    2. Drop tokens shorter than :data:`_MIN_TAG_LEN`.
    3. Drop pure-numeric tokens.
    4. Drop tokens in :data:`_STOPWORDS`.
    5. Deduplicate while preserving first-occurrence order.
    6. Return the first ``max_tags`` tokens as a tuple.

    Order matters: it makes the tags reproducible across calls with the
    same input, which keeps tests deterministic and lets the network
    side de-correlate views with content (a stable hash of the tag
    tuple is reasonable as a query key).
    """
    if not text or not text.strip():
        return ()

    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)

    seen: set[str] = set()
    out: list[str] = []
    for word in cleaned.split():
        if len(word) < _MIN_TAG_LEN:
            continue
        if word.isdigit():
            continue
        if word in _STOPWORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= max_tags:
            break
    return tuple(out)


__all__ = ["extract_tags_from_message"]
