"""Segmentation unit tests for BM25Index._segment.

Splits MEMORY.md into paragraph-delimited IndexedEntry instances using
1+ blank lines and markdown headings as boundaries.
"""

from opencomputer.agent.memory_index import BM25Index, IndexedEntry


def _segment(text: str) -> list[IndexedEntry]:
    return BM25Index._segment(text)


def test_segment_empty() -> None:
    assert _segment("") == []


def test_segment_only_blank_lines() -> None:
    assert _segment("\n\n\n") == []


def test_segment_single_entry() -> None:
    entries = _segment("hello world")
    assert len(entries) == 1
    assert entries[0].raw == "hello world"
    assert entries[0].line_start == 1
    assert entries[0].line_end == 1


def test_segment_two_entries_blank_line_separated() -> None:
    text = "first entry\n\nsecond entry"
    entries = _segment(text)
    assert len(entries) == 2
    assert entries[0].raw == "first entry"
    assert entries[1].raw == "second entry"


def test_segment_two_entries_multi_blank_separated() -> None:
    text = "first\n\n\n\nsecond"
    entries = _segment(text)
    assert len(entries) == 2


def test_segment_heading_creates_boundary() -> None:
    text = "intro paragraph\n## Section A\nbody of A\n## Section B\nbody of B"
    entries = _segment(text)
    raws = [e.raw for e in entries]
    assert "intro paragraph" in raws
    assert any(r.startswith("## Section A") for r in raws)
    assert any(r.startswith("## Section B") for r in raws)


def test_segment_top_level_heading_also_boundary() -> None:
    text = "before\n# Top\nafter"
    entries = _segment(text)
    assert len(entries) >= 2
    assert any(e.raw.startswith("# Top") for e in entries)


def test_segment_line_numbers_are_one_indexed() -> None:
    text = "\n\nfirst entry\n\nsecond entry"
    entries = _segment(text)
    assert entries[0].line_start == 3
    assert entries[1].line_start >= 5


def test_segment_multiline_entry_preserves_internal_blanks_no_double_blank() -> None:
    text = "line one\nline two\nline three"
    entries = _segment(text)
    assert len(entries) == 1
    assert entries[0].raw == "line one\nline two\nline three"


def test_segment_strips_leading_trailing_blanks_in_entry() -> None:
    text = "\n\n  entry one  \n\n"
    entries = _segment(text)
    assert len(entries) == 1
    assert entries[0].raw.strip() == "entry one"
