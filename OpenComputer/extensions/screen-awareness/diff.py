"""OCR-text line diff for pre/post tool capture pairs.

Returns a ScreenDelta dataclass with frozen tuples of added + removed
lines. Whitespace-only lines are normalized away so OCR jitter doesn't
look like a real change. Order is preserved: added/removed reflect the
natural order in their respective screens.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScreenDelta:
    """Immutable line-level diff result."""

    added: tuple[str, ...]
    removed: tuple[str, ...]


def _normalize(text: str) -> tuple[str, ...]:
    """Split into lines, strip each, drop empties."""
    return tuple(
        stripped
        for line in text.splitlines()
        if (stripped := line.strip())
    )


def compute_screen_delta(pre_text: str, post_text: str) -> ScreenDelta:
    """Compute added/removed lines between pre and post OCR text.

    Each "line" is whitespace-stripped; empty-after-strip lines are
    dropped before diffing. So `"  Login  "` and `"Login"` compare equal,
    and `"\n\n"` between them adds nothing to either side.
    """
    pre_lines = _normalize(pre_text)
    post_lines = _normalize(post_text)
    pre_set = set(pre_lines)
    post_set = set(post_lines)
    added = tuple(line for line in post_lines if line not in pre_set)
    removed = tuple(line for line in pre_lines if line not in post_set)
    return ScreenDelta(added=added, removed=removed)


__all__ = ["ScreenDelta", "compute_screen_delta"]
