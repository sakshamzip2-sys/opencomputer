"""Diff format renderer tests."""
from opencomputer.tools.edit_diff_format import MAX_DIFF_LINES, render_unified_diff


def test_renders_simple_diff():
    diff = render_unified_diff(before="hello\n", after="world\n", file_path="/x")
    assert "-hello" in diff
    assert "+world" in diff


def test_caps_long_diffs():
    before = "\n".join(f"line {i}" for i in range(2000)) + "\n"
    after = "\n".join(f"DIFFERENT {i}" for i in range(2000)) + "\n"
    diff = render_unified_diff(before=before, after=after, file_path="/x")
    assert "more lines truncated" in diff
    assert diff.count("\n") <= MAX_DIFF_LINES + 5


def test_no_diff_when_identical():
    diff = render_unified_diff(before="x\n", after="x\n", file_path="/y")
    assert diff == ""


def test_includes_filename_in_header():
    diff = render_unified_diff(before="a\n", after="b\n", file_path="/path/to/file.py")
    assert "/path/to/file.py" in diff


def test_handles_no_trailing_newline():
    diff = render_unified_diff(before="a", after="b", file_path="/x")
    assert "-a" in diff
    assert "+b" in diff
