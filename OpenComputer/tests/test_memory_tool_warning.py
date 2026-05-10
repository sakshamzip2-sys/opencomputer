"""Tests for the in-band cap-pressure warning prepended to MemoryTool results.

Part of M1 of the 2026-05-10 memory-observability design. The Memory tool's success path
prepends a warning string to ``ToolResult.content`` whenever a write pushes the file past
the warn threshold (or causes compaction). Below threshold, the result is unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_context import MemoryContext
from opencomputer.agent.state import SessionDB
from opencomputer.tools.memory_tool import MemoryTool
from plugin_sdk.core import ToolCall


def _make_ctx(tmp_path, *, memory_limit: int = 4000, user_limit: int = 2000):
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        user_path=tmp_path / "USER.md",
        skills_path=tmp_path / "skills",
        memory_char_limit=memory_limit,
        user_char_limit=user_limit,
    )
    return MemoryContext(
        manager=mm,
        db=SessionDB(tmp_path / "sessions.db"),
        session_id_provider=lambda: "test",
    )


def _call(tool, **args):
    return asyncio.run(tool.execute(ToolCall(id="tc-1", name="Memory", arguments=args)))


class TestNoWarningBelowThreshold:
    def test_add_well_under_threshold_no_warning(self, tmp_path):
        ctx = _make_ctx(tmp_path, memory_limit=4000)
        r = _call(MemoryTool(ctx), action="add", target="memory", content="small entry")
        assert r.is_error is False
        # No warning prefix; content is the plain success string
        assert "MEMORY.md AT" not in r.content
        assert "COMPACTED" not in r.content
        assert "Added entry to MEMORY.md" in r.content

    def test_read_does_not_warn_at_high_pct(self, tmp_path):
        # Even when the file is 95% full, READ should never warn — read is
        # not a write and doesn't shift cap pressure.
        ctx = _make_ctx(tmp_path, memory_limit=200)
        ctx.manager.append_declarative("x" * 180)  # 180 of 200 = 90%
        r = _call(MemoryTool(ctx), action="read", target="memory")
        assert r.is_error is False
        # The read returns file content; that file content is large but
        # the ToolResult.content shouldn't carry a synthetic warning.
        # (We accept that the file body itself contains x's, which doesn't
        # contain "MEMORY.md AT" — that string is only in our warning template.)
        assert "MEMORY.md AT" not in r.content


class TestWarningAtThreshold:
    def test_add_pushing_past_80_pct_warns(self, tmp_path):
        # Use a tiny limit so a single add lands above 80%
        ctx = _make_ctx(tmp_path, memory_limit=100)
        # Pre-fill to 50%
        ctx.manager.append_declarative("x" * 40)
        # Now add an entry that brings file past 80%
        r = _call(MemoryTool(ctx), action="add", target="memory", content="y" * 40)
        assert r.is_error is False
        # Warning prefix present
        assert "MEMORY.md AT" in r.content
        # Plain success message still present
        assert "Added entry to MEMORY.md" in r.content
        # File body itself should be UNCHANGED by the warning (warning is in
        # tool result, not in the file)
        body = ctx.manager.read_declarative()
        assert "MEMORY.md AT" not in body

    def test_user_md_warning_uses_user_md_name(self, tmp_path):
        ctx = _make_ctx(tmp_path, user_limit=100)
        ctx.manager.append_user("x" * 90)  # 90% full
        r = _call(MemoryTool(ctx), action="add", target="user", content="y" * 5)
        # Warning naming the right file
        assert "USER.md" in r.content


class TestCompactionWarning:
    def test_compaction_triggers_escalated_warning(self, tmp_path):
        # Tiny limit forces compaction on the third add
        ctx = _make_ctx(tmp_path, memory_limit=100)
        ctx.manager.append_declarative("x" * 40)
        ctx.manager.append_declarative("y" * 40)
        # This add WILL trigger compaction (drops oldest)
        r = _call(MemoryTool(ctx), action="add", target="memory", content="z" * 40)
        assert r.is_error is False
        # Compaction warning fires (M1 detects pct ≥ 80% even without
        # compaction-delta wired through — that lands in M2). For M1, the
        # post-write pct is what we have.
        # Either AT-warning or COMPACTED should fire here; both are acceptable
        # from M1's perspective.
        assert "MEMORY.md AT" in r.content or "COMPACTED" in r.content


class TestErrorPathUnaffected:
    def test_error_path_no_warning(self, tmp_path):
        # An error result should not carry a warning prefix; we only warn on
        # successful writes.
        ctx = _make_ctx(tmp_path, memory_limit=100)
        # Replace something that doesn't exist
        r = _call(
            MemoryTool(ctx),
            action="replace",
            target="memory",
            old="nonexistent",
            new="z",
        )
        assert r.is_error is True
        assert "not found" in r.content.lower()
        assert "MEMORY.md AT" not in r.content


class TestRemoveCanLowerPctButShouldStillNotWarnIfBelow:
    def test_remove_dropping_below_threshold_no_warning(self, tmp_path):
        ctx = _make_ctx(tmp_path, memory_limit=100)
        ctx.manager.append_declarative("x" * 90)  # 90% full
        # Remove most of it; file goes well below 80%
        r = _call(MemoryTool(ctx), action="remove", target="memory", content="x" * 90)
        assert r.is_error is False
        assert "MEMORY.md AT" not in r.content
