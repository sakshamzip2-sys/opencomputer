"""Active-session ContextVar pattern + aux LLM call recording.

Hermes-followup 2026-05-07. Verifies:
- ``set_active_session`` / ``clear_active_session`` round-trip.
- ``record_response_in_active_session`` is no-op outside a session.
- ``record_response_in_active_session`` writes a row inside one.
- Daemon-thread inheritance via ``copy_context()`` works (the title-gen
  pattern).
- ``record_response_for_provider`` extracts a sensible provider name.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.agent.usage_pricing import (
    _provider_name,
    active_db,
    active_session_id,
    clear_active_session,
    record_response_for_provider,
    record_response_in_active_session,
    set_active_session,
)


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    usage: _FakeUsage


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "aux.db")
    d.ensure_session("aux-1", platform="cli", model="claude-haiku-4-5")
    return d


def test_no_active_session_is_noop(db: SessionDB) -> None:
    clear_active_session()
    out = record_response_in_active_session(
        provider="anthropic",
        model="claude-haiku-4-5",
        response=_FakeResponse(_FakeUsage(input_tokens=10, output_tokens=5)),
    )
    assert out is None
    assert active_session_id() == ""
    assert active_db() is None
    rows = db.query_llm_calls(days=None)
    assert rows == []


def test_active_session_records(db: SessionDB) -> None:
    set_active_session("aux-1", db)
    try:
        # Cost may be None if the model lacks pricing data — what matters
        # is that a row gets inserted with the correct token counts.
        record_response_in_active_session(
            provider="anthropic",
            model="claude-haiku-4-5",
            response=_FakeResponse(_FakeUsage(input_tokens=200, output_tokens=50)),
        )
        rows = db.query_llm_calls(days=None)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 200
        assert rows[0]["output_tokens"] == 50
    finally:
        clear_active_session()


def test_record_for_provider_extracts_name(db: SessionDB) -> None:
    """Helper extracts provider.name OR falls back to class-name minus suffix."""

    class _NamedProvider:
        name = "fancy"

    class FakeProvider:
        # No name attr — class-name fallback
        pass

    set_active_session("aux-1", db)
    try:
        record_response_for_provider(
            provider=_NamedProvider(),
            model="claude-haiku-4-5",
            response=_FakeResponse(_FakeUsage(10, 5)),
        )
        record_response_for_provider(
            provider=FakeProvider(),
            model="claude-haiku-4-5",
            response=_FakeResponse(_FakeUsage(20, 10)),
        )
        rows_by_provider = db.query_llm_calls(
            days=None, group_by="provider"
        )
        names = {r["key"] for r in rows_by_provider}
        assert "fancy" in names
        # FakeProvider → "fake" after stripping 'provider' suffix
        assert "fake" in names
    finally:
        clear_active_session()


def test_provider_name_helper_directly() -> None:
    class _Named:
        name = "openrouter"

    class AnthropicProvider:
        pass

    class _NoneName:
        name = None

    assert _provider_name(_Named()) == "openrouter"
    assert _provider_name(AnthropicProvider()) == "anthropic"
    # name=None → fallback
    assert _provider_name(_NoneName()) == "_nonename"


def test_daemon_thread_inherits_context(db: SessionDB) -> None:
    """The title-gen pattern — daemon thread spawned via copy_context()
    inherits the active-session ContextVars.
    """
    set_active_session("aux-1", db)
    captured: dict = {}

    def in_thread() -> None:
        captured["sid"] = active_session_id()
        captured["db_present"] = active_db() is not None
        record_response_in_active_session(
            provider="thread-prov",
            model="claude-haiku-4-5",
            response=_FakeResponse(_FakeUsage(7, 3)),
        )

    try:
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(in_thread))
        t.start()
        t.join(timeout=2.0)
        assert captured["sid"] == "aux-1"
        assert captured["db_present"] is True
        rows = db.query_llm_calls(days=None, group_by="provider")
        provider_names = {r["key"] for r in rows}
        assert "thread-prov" in provider_names
    finally:
        clear_active_session()


def test_no_usage_attribute_is_noop(db: SessionDB) -> None:
    set_active_session("aux-1", db)
    try:

        class _NoUsage:
            pass  # no .usage attr

        out = record_response_in_active_session(
            provider="x", model="y", response=_NoUsage()
        )
        assert out is None
        assert db.query_llm_calls(days=None) == []
    finally:
        clear_active_session()
