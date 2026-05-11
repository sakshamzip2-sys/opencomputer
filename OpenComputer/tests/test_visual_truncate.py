"""Tests for ``opencomputer/cli_ui/visual_truncate.py`` — display-side
truncation utilities ported from pi's ``core/tools/truncate.ts`` +
``modes/interactive/components/visual-truncate.ts``.

These are distinct from ``opencomputer/agent/tokenjuice.py``, which
rewrites tool-result *content* before it goes to the model. The
visual-truncate path runs at *display* time — the model still sees
the full result; only the chat-rendered string is shortened.

Why both: model-side compaction trades correctness for tokens (model
loses detail). Display-side truncation only trades user attention
(everything is on disk via ``oc session show``). Mixing the two would
either over-prune or surface a full 50KB blob in the user's terminal.
"""

from __future__ import annotations

import pytest

from opencomputer.cli_ui.visual_truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    truncate_head,
    truncate_middle,
    truncate_tail,
)


class TestFormatSize:
    def test_under_1kb(self) -> None:
        assert format_size(0) == "0B"
        assert format_size(500) == "500B"
        assert format_size(1023) == "1023B"

    def test_kilobytes(self) -> None:
        assert format_size(1024) == "1.0KB"
        assert format_size(51200) == "50.0KB"

    def test_megabytes(self) -> None:
        assert format_size(1024 * 1024) == "1.0MB"


class TestTruncateHead:
    def test_no_truncation_when_short(self) -> None:
        text = "line1\nline2\nline3"
        result = truncate_head(text)
        assert result.truncated is False
        assert result.content == text
        assert result.truncated_by is None

    def test_truncates_by_lines(self) -> None:
        text = "\n".join(f"line{i}" for i in range(100))
        result = truncate_head(text, max_lines=10)
        assert result.truncated is True
        assert result.truncated_by == "lines"
        assert result.output_lines == 10
        assert "line0" in result.content
        assert "line99" not in result.content

    def test_truncates_by_bytes(self) -> None:
        text = "header\n" + ("x" * 50 + "\n") * 100
        result = truncate_head(text, max_bytes=200)
        assert result.truncated is True
        assert result.truncated_by == "bytes"
        # Header is kept whole (head truncation = preserve start).
        assert result.content.startswith("header")
        # Content fits under the byte cap.
        assert len(result.content.encode("utf-8")) <= 200
        # No partial lines — every kept line is either fully present or absent.
        for line in result.content.split("\n"):
            # Each non-empty line is either "header" or 50 x's, never a fragment.
            assert line in ("header", "x" * 50)

    def test_first_line_exceeds_limit(self) -> None:
        text = "x" * 1000
        result = truncate_head(text, max_bytes=100)
        assert result.first_line_exceeds_limit is True
        assert result.content == ""

    def test_empty_input(self) -> None:
        result = truncate_head("")
        assert result.truncated is False
        assert result.content == ""
        assert result.total_lines == 1  # split("\n") on "" gives [""]
        assert result.total_bytes == 0


class TestTruncateTail:
    def test_keeps_last_n_lines(self) -> None:
        text = "\n".join(f"line{i}" for i in range(100))
        result = truncate_tail(text, max_lines=5)
        assert result.truncated is True
        assert result.truncated_by == "lines"
        assert "line99" in result.content
        assert "line0" not in result.content
        assert result.output_lines == 5

    def test_no_truncation_when_short(self) -> None:
        text = "only line"
        result = truncate_tail(text)
        assert result.truncated is False
        assert result.content == text

    def test_truncates_by_bytes(self) -> None:
        text = ("hello\n") * 1000
        result = truncate_tail(text, max_bytes=100)
        assert result.truncated is True
        # Tail-truncated content should fit under the byte cap.
        assert len(result.content.encode("utf-8")) <= 100


class TestTruncateMiddle:
    """OC-specific extension — show head + tail with an explicit
    elision marker in between. PI doesn't ship this; we add it because
    bash output often has the useful info on both ends (the command
    and the final error/result).
    """

    def test_no_truncation_when_short(self) -> None:
        text = "a\nb\nc"
        result = truncate_middle(text, max_lines=10)
        assert result.truncated is False
        assert result.content == text

    def test_keeps_head_and_tail_with_marker(self) -> None:
        text = "\n".join(f"line{i}" for i in range(100))
        result = truncate_middle(text, max_lines=10)
        assert result.truncated is True
        assert result.truncated_by == "lines"
        # Both ends preserved.
        assert "line0" in result.content
        assert "line99" in result.content
        # Middle elided with a marker.
        assert "[" in result.content and "lines omitted" in result.content
        # Marker exposes how many lines were dropped.
        # 100 lines, keeping 5 head + 5 tail = 90 omitted.
        assert "90" in result.content

    def test_odd_max_lines(self) -> None:
        text = "\n".join(f"line{i}" for i in range(20))
        result = truncate_middle(text, max_lines=7)
        assert result.truncated is True
        # 7 lines = 3 head + marker + 4 tail (head <= tail when odd).
        assert "line0" in result.content
        assert "line19" in result.content

    def test_max_lines_at_or_below_4_falls_back_to_tail(self) -> None:
        # Below 4 there's no useful elision — degrade gracefully to tail.
        text = "\n".join(f"line{i}" for i in range(20))
        result = truncate_middle(text, max_lines=2)
        assert result.truncated is True
        # No marker — head/tail split makes no sense with <2 lines per side.
        assert "lines omitted" not in result.content


class TestDefaults:
    def test_defaults_match_pi(self) -> None:
        # Document the limits so future PRs can't silently change them.
        assert DEFAULT_MAX_LINES == 2000
        assert DEFAULT_MAX_BYTES == 50 * 1024
