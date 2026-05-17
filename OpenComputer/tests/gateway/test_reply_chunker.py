"""Tests for the generic outgoing-reply chunker (M3 / #3 fix).

``chunk_text`` replaces the content-dropping ``truncate_smart`` on the
outgoing-drainer path: a body over the platform cap is split into
ordered ``(i/N)``-marked messages instead of being cut.
"""
from opencomputer.gateway.reply_chunker import chunk_text


def test_short_text_is_one_chunk_unmarked() -> None:
    assert chunk_text("hello", cap=4096) == ["hello"]


def test_text_exactly_at_cap_is_not_split() -> None:
    body = "x" * 100
    assert chunk_text(body, cap=100) == [body]


def test_long_text_splits_into_marked_ordered_chunks() -> None:
    body = "\n".join(f"line {i}" for i in range(400))  # ~3KB+
    chunks = chunk_text(body, cap=200)
    assert len(chunks) > 1
    # every chunk fits the cap
    assert all(len(c) <= 200 for c in chunks)
    # every chunk is marked (i/N), in order
    for i, c in enumerate(chunks, start=1):
        assert c.startswith(f"({i}/{len(chunks)})")
    # no content lost — strip markers, rejoin, compare
    rejoined = "".join(
        c.split("\n", 1)[1] if c.startswith("(") else c for c in chunks
    )
    assert rejoined == body


def test_single_overlong_line_is_hard_split() -> None:
    body = "z" * 5000  # one line, no break points
    chunks = chunk_text(body, cap=500)
    assert all(len(c) <= 500 for c in chunks)
    rejoined = "".join(c.split("\n", 1)[1] for c in chunks)
    assert rejoined == body


def test_empty_text() -> None:
    assert chunk_text("", cap=4096) == [""]


def test_tiny_cap_below_marker_width_still_splits_without_crash() -> None:
    chunks = chunk_text("abcdefghij", cap=4)
    assert all(len(c) <= 4 for c in chunks)
    assert "".join(chunks) == "abcdefghij"  # no markers when cap too small


def test_chunk_count_is_stable() -> None:
    """N in the (i/N) marker matches the actual chunk count."""
    body = "paragraph\n" * 500
    chunks = chunk_text(body, cap=300)
    n = len(chunks)
    for c in chunks:
        assert f"/{n})" in c.split("\n", 1)[0]
