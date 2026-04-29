"""Tests for ActiveMemoryInjector — local FTS5 recall prepend."""
from __future__ import annotations

import pytest

from opencomputer.agent.active_memory import ActiveMemoryConfig, ActiveMemoryInjector


class _StubDB:
    def __init__(self, *, episodic=None, messages=None):
        self._ep = episodic or []
        self._msg = messages or []

    def search_episodic(self, query, *, limit=10):
        return self._ep[:limit]

    def search(self, query, *, limit=10):
        return self._msg[:limit]


def test_disabled_returns_none():
    inj = ActiveMemoryInjector(_StubDB(), config=ActiveMemoryConfig(enabled=False))
    assert inj.recall_block("anything") is None


def test_enabled_with_no_hits_returns_none():
    inj = ActiveMemoryInjector(_StubDB(), config=ActiveMemoryConfig(enabled=True))
    assert inj.recall_block("does not match") is None


def test_query_too_short_returns_none():
    inj = ActiveMemoryInjector(
        _StubDB(episodic=[{"session_id": "abc12345", "turn_index": 0, "summary": "X"}]),
        config=ActiveMemoryConfig(enabled=True, min_query_chars=5),
    )
    assert inj.recall_block("ab") is None


def test_episodic_hit_renders_block():
    db = _StubDB(episodic=[
        {"session_id": "abc12345" * 2, "turn_index": 3, "summary": "User likes tea."},
    ])
    inj = ActiveMemoryInjector(db, config=ActiveMemoryConfig(enabled=True))
    block = inj.recall_block("what do I drink")
    assert block is not None
    assert "<relevant-memories>" in block
    assert "</relevant-memories>" in block
    assert "User likes tea" in block
    assert "abc12345" in block
    assert "/3" in block


def test_message_hit_renders_block():
    db = _StubDB(messages=[
        {"session_id": "ses99876", "role": "user", "snippet": "I prefer green tea."},
    ])
    inj = ActiveMemoryInjector(db, config=ActiveMemoryConfig(enabled=True))
    block = inj.recall_block("tea preferences")
    assert block is not None
    assert "I prefer green tea" in block
    assert "[msg ses99876…" in block


def test_top_n_caps_total_hits():
    db = _StubDB(
        episodic=[{"session_id": "s1", "turn_index": i, "summary": f"E{i}"} for i in range(5)],
        messages=[{"session_id": "s2", "role": "user", "snippet": f"M{i}"} for i in range(5)],
    )
    inj = ActiveMemoryInjector(db, config=ActiveMemoryConfig(enabled=True, top_n=2))
    block = inj.recall_block("query")
    assert block is not None
    # 2 episodic + open header/footer + no messages (because remaining=0 after 2 episodic)
    assert block.count("[ep ") == 2
    assert block.count("[msg ") == 0


def test_db_exception_falls_through_to_none():
    class _BoomDB:
        def search_episodic(self, *a, **kw):
            raise RuntimeError("FTS5 unavailable")

        def search(self, *a, **kw):
            raise RuntimeError("FTS5 unavailable")

    inj = ActiveMemoryInjector(_BoomDB(), config=ActiveMemoryConfig(enabled=True))
    # Both stores fail → no hits → None (graceful fallback)
    assert inj.recall_block("anything") is None
