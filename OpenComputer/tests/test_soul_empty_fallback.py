"""Hermes v2 parity follow-up: SOUL.md whitespace-only → empty (fallback to built-in).

Hermes spec: "Empty/whitespace-only file → falls back to built-in default identity."
OC's base.j2 already omits the ``## Profile identity`` section when ``soul == ""``,
so falling back is a matter of returning ``""`` from ``read_soul`` for empty
*or whitespace-only* content. PR #510 only handled the missing-file case.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import MemoryManager


def _make_memory(tmp_path: Path) -> MemoryManager:
    """Construct a MemoryManager pointed at a fresh profile dir."""
    return MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "skills",
        soul_path=tmp_path / "SOUL.md",
    )


def test_missing_soul_returns_empty(tmp_path: Path):
    mem = _make_memory(tmp_path)
    assert mem.read_soul() == ""


def test_empty_soul_returns_empty(tmp_path: Path):
    mem = _make_memory(tmp_path)
    mem.soul_path.write_text("", encoding="utf-8")
    assert mem.read_soul() == ""


def test_whitespace_only_soul_returns_empty(tmp_path: Path):
    mem = _make_memory(tmp_path)
    mem.soul_path.write_text("   \n\n\t  \n", encoding="utf-8")
    assert mem.read_soul() == ""


def test_real_soul_content_is_returned(tmp_path: Path):
    mem = _make_memory(tmp_path)
    body = "# SOUL\n\nYou are a senior engineer.\n"
    mem.soul_path.write_text(body, encoding="utf-8")
    assert mem.read_soul() == body
