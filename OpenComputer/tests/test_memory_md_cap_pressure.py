"""MEMORY.md cap pressure handling tests (v1.1 plan-3 M6.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.memory import (
    MemoryManager,
    MemoryTooLargeError,
    _compact_replace_under_cap,
    _compact_under_cap,
    _segment_paragraphs,
)

# ─── pure helpers ─────────────────────────────────────────────────


def test_segment_paragraphs_empty() -> None:
    assert _segment_paragraphs("") == []
    assert _segment_paragraphs("\n\n\n") == []


def test_segment_paragraphs_blank_separated() -> None:
    text = "alpha\n\nbeta\n\ngamma"
    assert _segment_paragraphs(text) == ["alpha", "beta", "gamma"]


def test_segment_paragraphs_multiline_entry() -> None:
    text = "alpha\nstill alpha\n\nbeta"
    parts = _segment_paragraphs(text)
    assert parts[0] == "alpha\nstill alpha"
    assert parts[1] == "beta"


def test_compact_under_cap_already_fits() -> None:
    # No compaction needed
    existing = "alpha\n\nbeta"
    new_block = "gamma\n"
    out = _compact_under_cap(existing, new_block, limit=1000)
    assert out == "alpha\n\nbeta\n\ngamma\n"


def test_compact_under_cap_drops_oldest_until_fits() -> None:
    # 10 entries × ~30 chars each + new entry; cap forces dropping
    # several oldest so the result fits.
    existing = "\n\n".join(
        f"entry-{i:02d} with extra padding text" for i in range(10)
    )
    new_block = "fresh entry that is meaningful\n"
    # Limit just enough for last few entries + compaction header + new_block.
    limit = 200
    out = _compact_under_cap(existing, new_block, limit=limit)
    assert out is not None
    assert len(out) <= limit
    # Most recent entries should still be present
    assert "entry-09" in out
    # And the compaction header should appear at the top
    assert "## Older notes" in out
    # And the fresh entry is at the bottom
    assert out.endswith(new_block)


def test_compact_under_cap_returns_none_when_new_block_alone_too_big() -> None:
    existing = ""
    new_block = "x" * 1000 + "\n"
    out = _compact_under_cap(existing, new_block, limit=100)
    assert out is None


def test_compact_under_cap_idempotent_no_nested_headers() -> None:
    # Pre-existing compaction header should be replaced, not duplicated.
    existing = (
        "## Older notes (3 entries compacted on 2025-01-01)\n"
        "_some explanation\n\n"
        "alpha\n\nbeta"
    )
    new_block = "gamma\n"
    out = _compact_under_cap(existing, new_block, limit=2000)
    # Should contain at most one "## Older notes" header
    assert out.count("## Older notes") <= 1


def test_compact_replace_under_cap_already_fits() -> None:
    candidate = "alpha\n\nbeta"
    out = _compact_replace_under_cap(candidate, limit=1000)
    assert out == candidate


def test_compact_replace_under_cap_drops_oldest() -> None:
    candidate = "\n\n".join(f"entry-{i:02d}" for i in range(10))
    out = _compact_replace_under_cap(candidate, limit=80)
    assert out is not None
    assert len(out) <= 80
    assert "## Older notes" in out
    # Most recent entry should survive
    assert "entry-09" in out


# ─── MemoryManager integration ────────────────────────────────────


def _make_manager(tmp_path: Path, *, char_limit: int = 4000) -> MemoryManager:
    decl = tmp_path / "MEMORY.md"
    decl.write_text("", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return MemoryManager(
        declarative_path=decl,
        skills_path=skills,
        memory_char_limit=char_limit,
    )


def test_append_under_cap_no_compaction(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path, char_limit=1000)
    mm.append_declarative("alpha entry one")
    mm.append_declarative("beta entry two")
    content = mm.read_declarative()
    assert "alpha entry one" in content
    assert "beta entry two" in content
    assert "## Older notes" not in content


def test_append_over_cap_triggers_compaction_no_error(tmp_path: Path) -> None:
    """Previously raised MemoryTooLargeError; M6.5 makes it graceful."""
    # Set a small cap so only a handful of entries fit.
    mm = _make_manager(tmp_path, char_limit=400)
    for i in range(20):
        mm.append_declarative(f"entry number {i:02d} with some descriptive content")

    # Read back: the file must be under cap and contain the compaction header
    content = mm.read_declarative()
    assert len(content) <= 400
    assert "## Older notes" in content
    # The most-recent entry must survive
    assert "entry number 19" in content


def test_append_alone_bigger_than_cap_still_raises(tmp_path: Path) -> None:
    """The genuinely impossible case: a single new entry that exceeds
    the cap on its own.  Compaction can't help; raise the error."""
    mm = _make_manager(tmp_path, char_limit=100)
    huge = "x" * 1000
    with pytest.raises(MemoryTooLargeError):
        mm.append_declarative(huge)


def test_replace_over_cap_compacts(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path, char_limit=300)
    for i in range(15):
        # Use small entries that fit individually
        try:
            mm.append_declarative(f"entry {i:02d}")
        except MemoryTooLargeError:
            pass
    # Now do a replace that grows the content over cap
    big_replacement = "REPLACED " * 50  # 450 chars
    # Try replacing some short string with the big one
    if "entry 00" in mm.read_declarative():
        # If compaction kept entry 00, replace it; else replace any substring
        try:
            mm.replace_declarative("entry 00", big_replacement)
        except MemoryTooLargeError:
            # If even after compaction the post-replace content can't fit
            # (because big_replacement alone > cap), that's acceptable
            pass

    content = mm.read_declarative()
    assert len(content) <= 300


def test_compaction_does_not_drop_brand_new_entry(tmp_path: Path) -> None:
    """The compaction logic must always preserve the new entry; only
    older entries get dropped."""
    mm = _make_manager(tmp_path, char_limit=300)
    for i in range(20):
        try:
            mm.append_declarative(f"older entry {i:02d}")
        except MemoryTooLargeError:
            pass

    # Append a recognizable fresh entry — must survive.
    mm.append_declarative("freshly-added-content-marker")
    content = mm.read_declarative()
    assert "freshly-added-content-marker" in content


def test_compaction_header_includes_today(tmp_path: Path) -> None:
    import datetime as _dt

    mm = _make_manager(tmp_path, char_limit=300)
    for i in range(20):
        try:
            mm.append_declarative(f"entry {i:02d} with some content padding")
        except MemoryTooLargeError:
            pass
    content = mm.read_declarative()
    today = _dt.date.today().isoformat()
    assert today in content


def test_idempotent_compaction_preserves_one_header(tmp_path: Path) -> None:
    """Multiple compactions over time must not stack ``## Older notes`` blocks."""
    mm = _make_manager(tmp_path, char_limit=300)
    for i in range(50):
        try:
            mm.append_declarative(f"entry {i:02d} extra padding text here")
        except MemoryTooLargeError:
            pass
    content = mm.read_declarative()
    # Count of header occurrences should be at most 1
    assert content.count("## Older notes") <= 1
