"""Tests for MemoryWriteEvent compaction-delta + dropped_paragraphs fields.

Part of M2 of the 2026-05-10 memory-observability design. The new fields are additive on a
frozen-with-defaults dataclass so they're BC-safe per `plugin_sdk/CLAUDE.md` §1.4.

These tests verify:
  - The event carries the new fields.
  - On non-compacting writes, both fields are 0.
  - On compacting appends, both fields reflect what was dropped.
  - The Memory tool's success result escalates to a COMPACTED warning when drops occur.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_context import MemoryContext
from opencomputer.agent.state import SessionDB
from opencomputer.tools.memory_tool import MemoryTool
from plugin_sdk.core import ToolCall
from plugin_sdk.ingestion import MemoryWriteEvent

# ─── BC test on the dataclass shape ────────────────────────────────


def test_memory_write_event_accepts_new_fields() -> None:
    ev = MemoryWriteEvent(
        session_id=None,
        source="test",
        action="append",
        target="MEMORY.md",
        content_size=100,
        compaction_delta=42,
        dropped_paragraphs=2,
    )
    assert ev.compaction_delta == 42
    assert ev.dropped_paragraphs == 2


def test_memory_write_event_old_call_signature_still_works() -> None:
    """Existing callers that don't pass the new fields must continue to construct
    a valid event (defaults apply)."""
    ev = MemoryWriteEvent(
        session_id=None,
        source="test",
        action="append",
        target="MEMORY.md",
        content_size=100,
    )
    assert ev.compaction_delta == 0
    assert ev.dropped_paragraphs == 0


# ─── MemoryManager publishes the right values ──────────────────────


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace default_bus.publish with a capture list. Returns the list."""
    captured: list[Any] = []

    from opencomputer.ingestion import bus as bus_mod

    original = bus_mod.default_bus.publish

    def fake_publish(event: Any) -> None:
        captured.append(event)
        return original(event)

    monkeypatch.setattr(bus_mod.default_bus, "publish", fake_publish)
    return captured


def _make_mm(tmp_path, *, char_limit: int = 4000) -> MemoryManager:
    return MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        user_path=tmp_path / "USER.md",
        skills_path=tmp_path / "skills",
        memory_char_limit=char_limit,
        user_char_limit=char_limit,
    )


def test_non_compacting_append_publishes_zero_drops(tmp_path, monkeypatch) -> None:
    captured = _capture_events(monkeypatch)
    mm = _make_mm(tmp_path, char_limit=4000)

    mm.append_declarative("small entry")

    memory_events = [e for e in captured if isinstance(e, MemoryWriteEvent)]
    assert len(memory_events) >= 1
    last = memory_events[-1]
    assert last.dropped_paragraphs == 0
    assert last.compaction_delta == 0


def test_compacting_append_publishes_drops_and_delta(tmp_path, monkeypatch) -> None:
    mm = _make_mm(tmp_path, char_limit=200)
    # Pre-fill so the next append must compact
    for i in range(20):
        try:
            mm.append_declarative(f"entry-{i:02d} with extra padding")
        except Exception:
            pass

    captured = _capture_events(monkeypatch)
    # This append SHOULD trigger compaction (file is full of 200 chars)
    mm.append_declarative("fresh entry that needs space")

    memory_events = [e for e in captured if isinstance(e, MemoryWriteEvent)]
    assert len(memory_events) >= 1
    last = memory_events[-1]
    # Compaction must have dropped at least 1 paragraph and freed >= 1 byte.
    assert last.dropped_paragraphs >= 1
    assert last.compaction_delta >= 1


# ─── Tool warning escalates on compaction ──────────────────────────


def _make_ctx(tmp_path, *, memory_limit: int = 4000):
    mm = _make_mm(tmp_path, char_limit=memory_limit)
    return MemoryContext(
        manager=mm,
        db=SessionDB(tmp_path / "sessions.db"),
        session_id_provider=lambda: "test",
    )


def _call(tool, **args):
    return asyncio.run(tool.execute(ToolCall(id="tc-1", name="Memory", arguments=args)))


def test_tool_warning_escalates_to_compacted_on_drop(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, memory_limit=200)
    # Pre-fill the file
    for i in range(20):
        try:
            ctx.manager.append_declarative(f"older-{i:02d} with padding text")
        except Exception:
            pass

    # This add MUST trigger compaction
    r = _call(MemoryTool(ctx), action="add", target="memory", content="fresh entry needs room")
    assert r.is_error is False
    # The COMPACTED variant should fire (escalated form)
    assert "COMPACTED" in r.content
    assert "DROPPED" in r.content
