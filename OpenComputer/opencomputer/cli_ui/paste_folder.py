"""Paste-fold for the chat input — Claude-Code-style.

When the user pastes a chunk longer than ``LINE_THRESHOLD`` lines, the
input buffer shows a compact placeholder ``[Pasted text #N +M lines]``
instead of the full content. The actual text is held in memory and
substituted back in when the message is submitted to the LLM.

Two affordances:

- **Submit-time expansion** — :meth:`expand_all` runs over the final
  buffer text right before send, so the model sees the full content.
- **"Paste again to expand"** — if the user pastes the same content a
  second time, :meth:`is_same_as_last` matches and the caller replaces
  the placeholder in the buffer with the full text (so the user can
  edit before submitting).

Per-session lifetime: create one :class:`PasteFolder` per chat session,
call :meth:`clear` on ``/clear`` to reset the counter.
"""

from __future__ import annotations

import re

LINE_THRESHOLD = 5  # Pastes with > this many lines get folded.

# Match the placeholder format. Group 1 = blob id, group 2 = +M lines.
PLACEHOLDER_RE = re.compile(r"\[Pasted text #(\d+) \+(\d+) lines\]")


def _line_count(text: str) -> int:
    """Number of lines in a piece of text (1 for ``"foo"``, 2 for ``"foo\\n"``)."""
    if not text:
        return 0
    n = text.count("\n")
    # Trailing newline doesn't add a "next" line; count the visible lines.
    if text.endswith("\n"):
        return n
    return n + 1


class PasteFolder:
    """Per-session paste-fold storage."""

    def __init__(self, *, threshold: int = LINE_THRESHOLD) -> None:
        self._blobs: dict[int, str] = {}
        self._counter: int = 0
        self._last_fold_id: int | None = None
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        return self._threshold

    def fold(self, text: str) -> tuple[str, int | None]:
        """Maybe fold ``text`` into a placeholder.

        Returns ``(displayed_text, blob_id)``. ``blob_id`` is ``None`` if
        the text was below the threshold and is being passed through
        verbatim. When folded, the displayed text is the placeholder
        string and the full content is stored under ``blob_id``.
        """
        n = _line_count(text)
        if n <= self._threshold:
            return (text, None)
        self._counter += 1
        bid = self._counter
        self._blobs[bid] = text
        self._last_fold_id = bid
        extra = n - 1  # "+M lines" excludes the visible first line
        return (f"[Pasted text #{bid} +{extra} lines]", bid)

    def expand_all(self, text: str) -> str:
        """Substitute every placeholder back to its full content.

        Unknown blob ids (e.g. orphaned placeholders from a previous
        session) are left as-is — they round-trip harmlessly.
        """

        def repl(m: re.Match[str]) -> str:
            bid = int(m.group(1))
            return self._blobs.get(bid, m.group(0))

        return PLACEHOLDER_RE.sub(repl, text)

    def is_same_as_last(self, text: str) -> bool:
        """True if ``text`` is byte-identical to the most recently folded blob.

        Caller uses this to detect "paste again to expand" — when the same
        clipboard content arrives a second time, the caller can replace
        the placeholder in the buffer with the full text.
        """
        if self._last_fold_id is None:
            return False
        return self._blobs.get(self._last_fold_id) == text

    def placeholder_for(self, blob_id: int) -> str | None:
        """Reconstruct the placeholder string for a given blob id, or
        ``None`` if the id is unknown."""
        text = self._blobs.get(blob_id)
        if text is None:
            return None
        n = _line_count(text)
        extra = n - 1
        return f"[Pasted text #{blob_id} +{extra} lines]"

    def placeholder_for_last(self) -> str | None:
        """Placeholder string for the most recently folded blob."""
        if self._last_fold_id is None:
            return None
        return self.placeholder_for(self._last_fold_id)

    def has_active_fold(self, buffer_text: str) -> bool:
        """True if the buffer currently contains a placeholder we know
        about. Used to gate the "paste again to expand" hint visibility."""
        for m in PLACEHOLDER_RE.finditer(buffer_text):
            bid = int(m.group(1))
            if bid in self._blobs:
                return True
        return False

    def clear(self) -> None:
        """Drop all stored blobs + reset the counter (called on ``/clear``)."""
        self._blobs.clear()
        self._counter = 0
        self._last_fold_id = None


__all__ = [
    "LINE_THRESHOLD",
    "PLACEHOLDER_RE",
    "PasteFolder",
]
