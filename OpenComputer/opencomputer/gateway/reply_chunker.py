"""Generic outgoing-reply chunker — split, don't truncate.

M3 fix for parity mechanism #3 (``reply_truncation``). The outgoing
drainer used to call ``truncate_smart`` — cutting a long notification
body to the platform cap + ``…[truncated]`` and silently dropping the
rest. :func:`chunk_text` instead splits the body into ordered
``(i/N)``-marked messages, each within the cap, losing nothing.

Platform-agnostic — every channel adapter exposes ``max_message_length``
and the drainer sends one cap-sized message per chunk. The Telegram
adapter has its own UTF-16-aware ``_chunk_for_telegram`` for the
synchronous chat-reply path; this helper covers the drainer
(notification) path for all 18 adapters uniformly.
"""

from __future__ import annotations

#: Characters reserved at the head of each chunk for the ``(i/N)\n``
#: marker. ``(99/99)\n`` is 8 chars; 12 leaves headroom for 3-digit N.
_MARKER_RESERVE = 12


def chunk_text(text: str, *, cap: int) -> list[str]:
    """Split ``text`` into ordered messages each ``<= cap`` characters.

    Returns ``[text]`` unchanged when it already fits. Otherwise returns
    N>=2 chunks, each prefixed with an ``(i/N)`` marker line so the
    reader can see the ordering. No content is dropped — concatenating
    the post-marker bodies reproduces the input exactly.

    Splitting prefers line boundaries; a single line longer than the
    usable width is hard-split. When ``cap`` is too small to fit even
    the marker (``<= _MARKER_RESERVE``), chunks are emitted unmarked —
    correctness (every chunk within ``cap``) wins over the marker.
    """
    if len(text) <= cap:
        return [text]

    marked = cap > _MARKER_RESERVE
    width = (cap - _MARKER_RESERVE) if marked else cap

    raw: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > width:
            # Flush what we have, then hard-split the overlong line.
            if current:
                raw.append(current)
                current = ""
            for i in range(0, len(line), width):
                piece = line[i : i + width]
                if len(piece) == width:
                    raw.append(piece)
                else:
                    current = piece  # tail — let following lines join it
        elif len(current) + len(line) > width:
            raw.append(current)
            current = line
        else:
            current += line
    if current:
        raw.append(current)

    if not marked:
        return raw

    n = len(raw)
    return [f"({i}/{n})\n{body}" for i, body in enumerate(raw, start=1)]


__all__ = ["chunk_text"]
