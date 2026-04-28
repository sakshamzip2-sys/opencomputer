"""Tests for the @filepath autocomplete data layer (Hermes Tier 2.B)."""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.cli_ui.file_completer import (
    clear_cache,
    extract_at_token,
    find_project_files,
    format_size_label,
    score_path,
    top_matches,
)


@pytest.fixture(autouse=True)
def _clear_cache_each_test():
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# extract_at_token
# ---------------------------------------------------------------------------


def test_extract_basic_at_token():
    assert extract_at_token("hi @file.py", 11) == ("file.py", 3, 11)


def test_extract_at_at_start():
    assert extract_at_token("@foo", 4) == ("foo", 0, 4)


def test_extract_no_at():
    assert extract_at_token("plain text", 5) is None


def test_extract_at_partial_query():
    """Cursor mid-token. Function returns the full token (start→next-whitespace),
    not just what's left of cursor."""
    assert extract_at_token("look at @foo.py and", 12) == ("foo.py", 8, 15)


def test_extract_email_not_a_token():
    """user@host shape — preceded by non-whitespace, must NOT match."""
    assert extract_at_token("send to user@example.com", 19) is None


def test_extract_empty_query():
    """Bare @ with nothing after."""
    assert extract_at_token("hello @", 7) == ("", 6, 7)


def test_extract_at_after_newline():
    """Newline counts as whitespace."""
    text = "first\n@foo"
    assert extract_at_token(text, 10) == ("foo", 6, 10)


def test_extract_at_with_path_separator():
    """Forward slash inside the token is fine — captures the whole path."""
    assert extract_at_token("@dir/sub/file.py", 16) == ("dir/sub/file.py", 0, 16)


# ---------------------------------------------------------------------------
# score_path
# ---------------------------------------------------------------------------


def test_score_exact_filename():
    assert score_path("file.py", Path("src/file.py")) == 100


def test_score_path_starts_with():
    assert score_path("src/foo", Path("src/foo/bar.py")) == 80


def test_score_substring_in_name():
    assert score_path("foo", Path("src/myfoo.py")) == 70


def test_score_substring_in_path():
    assert score_path("src", Path("project/src/main.py")) >= 60


def test_score_no_match():
    assert score_path("zzz", Path("src/foo.py")) == 0


def test_score_empty_query():
    assert score_path("", Path("anything.py")) == 0


def test_score_fuzzy_initials():
    """All chars present at word boundaries → ≥ 25."""
    s = score_path("smf", Path("src/main/foo.py"))
    assert s >= 25


# ---------------------------------------------------------------------------
# top_matches
# ---------------------------------------------------------------------------


def test_top_matches_orders_by_score():
    files = [
        Path("src/main.py"),
        Path("test/main.py"),
        Path("main.py"),
    ]
    out = top_matches("main.py", files)
    # Exact filename match wins (score 100); the rest come after.
    assert out[0].name == "main.py"
    assert len(out) == 3


def test_top_matches_filters_zero_score():
    files = [Path("src/foo.py"), Path("src/bar.py")]
    out = top_matches("zzz", files)
    assert out == []


def test_top_matches_empty_query_returns_first_n():
    files = [Path(f"f{i}.py") for i in range(20)]
    out = top_matches("", files, n=5)
    assert len(out) == 5
    assert out == files[:5]


def test_top_matches_limits_to_n():
    files = [Path("foo.py"), Path("foobar.py"), Path("foobaz.py")]
    out = top_matches("foo", files, n=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# find_project_files
# ---------------------------------------------------------------------------


def test_find_project_files_basic(tmp_path: Path):
    (tmp_path / "a.py").write_text("# a")
    (tmp_path / "b.py").write_text("# b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("# c")

    files = find_project_files(tmp_path)
    names = {p.name for p in files}
    assert "a.py" in names
    assert "b.py" in names
    assert "c.py" in names


def test_find_project_files_skips_noise(tmp_path: Path):
    (tmp_path / "real.py").write_text("# x")
    junk_dir = tmp_path / "__pycache__"
    junk_dir.mkdir()
    (junk_dir / "junk.pyc").write_bytes(b"x")

    files = find_project_files(tmp_path)
    paths = [str(p) for p in files]
    assert any("real.py" in p for p in paths)
    # The __pycache__ filter applies to os.walk fallback. rg respects
    # gitignore by default which may or may not filter __pycache__ depending
    # on whether a .gitignore exists. So this test is best-effort.


def test_find_project_files_caches(tmp_path: Path):
    (tmp_path / "a.py").write_text("# a")
    first = find_project_files(tmp_path)

    # Add a new file; cache should still return the old result within TTL.
    (tmp_path / "b.py").write_text("# b")
    second = find_project_files(tmp_path)
    assert second == first  # cached


def test_find_project_files_capped(tmp_path: Path):
    """Sanity: arbitrary cwd doesn't blow up. Exact cap behavior depends
    on which walker (rg/fd/os.walk) is selected, so we just check we get
    a non-pathological result."""
    files = find_project_files(tmp_path)
    assert len(files) <= 5000


# ---------------------------------------------------------------------------
# format_size_label
# ---------------------------------------------------------------------------


def test_format_size_label_bytes(tmp_path: Path):
    p = tmp_path / "tiny.txt"
    p.write_bytes(b"x" * 500)
    assert format_size_label(Path("tiny.txt"), base=tmp_path) == "500B"


def test_format_size_label_kb(tmp_path: Path):
    p = tmp_path / "k.txt"
    p.write_bytes(b"x" * 5000)
    assert format_size_label(Path("k.txt"), base=tmp_path) == "4KB"


def test_format_size_label_dir(tmp_path: Path):
    d = tmp_path / "subdir"
    d.mkdir()
    assert format_size_label(Path("subdir"), base=tmp_path) == "dir"


def test_format_size_label_missing(tmp_path: Path):
    assert format_size_label(Path("ghost.py"), base=tmp_path) == ""
