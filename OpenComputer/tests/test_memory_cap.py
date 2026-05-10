"""Tests for opencomputer.agent.memory_cap — pure helpers for cap status + warning text.

Part of M1 of the 2026-05-10 memory-observability design. The `_cap_status` helper is the
single source of truth for "how full is this memory file?" and `warning_for` formats the
human/agent-facing string.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.memory_cap import CapStatus, cap_status, warning_for


class TestCapStatus:
    def test_empty_file(self) -> None:
        s = cap_status("", limit=4000, file_name="MEMORY.md")
        assert s.bytes_used == 0
        assert s.bytes_limit == 4000
        assert s.pct == pytest.approx(0.0)
        assert s.paragraph_count == 0
        assert s.file_name == "MEMORY.md"

    def test_half_full(self) -> None:
        s = cap_status("x" * 2000, limit=4000, file_name="MEMORY.md")
        assert s.bytes_used == 2000
        assert s.pct == pytest.approx(0.5)

    def test_just_under_warn_threshold(self) -> None:
        # 79% is below the 80% warn threshold
        s = cap_status("x" * 3160, limit=4000, file_name="MEMORY.md")
        assert s.pct == pytest.approx(0.79)

    def test_at_warn_threshold(self) -> None:
        s = cap_status("x" * 3200, limit=4000, file_name="MEMORY.md")
        assert s.pct == pytest.approx(0.80)

    def test_overflow_pct_can_exceed_one(self) -> None:
        # mid-compaction the candidate may be > limit briefly; CapStatus must
        # represent that honestly rather than clamp.
        s = cap_status("x" * 5000, limit=4000, file_name="MEMORY.md")
        assert s.pct > 1.0

    def test_paragraph_count(self) -> None:
        text = "alpha\n\nbeta\n\ngamma"
        s = cap_status(text, limit=4000, file_name="MEMORY.md")
        assert s.paragraph_count == 3

    def test_paragraph_count_with_compaction_header(self) -> None:
        text = "## Older notes (5 entries compacted on 2026-04-01)\n\nalpha\n\nbeta"
        s = cap_status(text, limit=4000, file_name="MEMORY.md")
        # Header should not inflate the paragraph count of "real" entries
        assert s.paragraph_count == 2

    def test_zero_limit_does_not_divide_by_zero(self) -> None:
        # Defensive: limit of 0 returns pct of 0.0 (or inf-replacement) rather than ZDE.
        s = cap_status("hello", limit=0, file_name="MEMORY.md")
        assert s.pct >= 0.0  # any non-crashing finite value is acceptable


class TestWarningFor:
    def _status(self, pct: float, *, file_name: str = "MEMORY.md") -> CapStatus:
        bytes_used = int(pct * 4000)
        return CapStatus(
            file_name=file_name,
            bytes_used=bytes_used,
            bytes_limit=4000,
            pct=pct,
            paragraph_count=10,
        )

    def test_no_warning_below_threshold(self) -> None:
        assert warning_for(self._status(0.50), dropped=0) is None
        assert warning_for(self._status(0.79), dropped=0) is None

    def test_warns_at_threshold(self) -> None:
        msg = warning_for(self._status(0.80), dropped=0)
        assert msg is not None
        assert "MEMORY.md" in msg
        assert "80" in msg or "0.80" in msg

    def test_warns_above_threshold(self) -> None:
        msg = warning_for(self._status(0.95), dropped=0)
        assert msg is not None
        assert "MEMORY.md" in msg
        assert "95" in msg

    def test_compaction_escalates_regardless_of_pct(self) -> None:
        # After a compaction event, the post-write pct may be 30% (we just
        # freed up space) — but the user STILL needs to know an entry was
        # dropped. Compaction warning fires regardless of pct.
        msg = warning_for(self._status(0.30), dropped=2)
        assert msg is not None
        assert "DROPPED" in msg.upper() or "COMPACT" in msg.upper()
        assert "2" in msg  # the count

    def test_compaction_at_high_pct_still_mentions_drops(self) -> None:
        msg = warning_for(self._status(0.90), dropped=3)
        assert msg is not None
        assert "3" in msg

    def test_user_md_file_name_carries_through(self) -> None:
        msg = warning_for(self._status(0.85, file_name="USER.md"), dropped=0)
        assert msg is not None
        assert "USER.md" in msg

    def test_singular_vs_plural_drops(self) -> None:
        msg_one = warning_for(self._status(0.50), dropped=1)
        msg_many = warning_for(self._status(0.50), dropped=5)
        assert msg_one is not None and msg_many is not None
        # Both should be readable; we don't enforce exact grammar but both
        # should mention the count.
        assert "1" in msg_one
        assert "5" in msg_many
