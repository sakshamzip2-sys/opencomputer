"""Tests for the usage_pricing wiring (Hermes B4).

Covers the new ``llm_calls`` schema migration, ``SessionDB.record_llm_call``
+ ``query_llm_calls`` methods, and the ``usage_pricing.record_call_*``
helpers that compose them with cost_guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from opencomputer.agent.state import SCHEMA_VERSION, SessionDB
from opencomputer.agent.usage_pricing import (
    record_call,
    record_call_from_usage,
)


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "test.db")
    # Create a session row so the FK insert works.
    d.ensure_session("s-1", platform="cli", model="claude-opus-4-7")
    return d


def test_schema_v13_exists(db: SessionDB) -> None:
    """After init, schema_version is at least 13 (this PR's bump)."""
    with db._connect() as conn:  # noqa: SLF001 — test reaches in
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 13


def test_llm_calls_table_present(db: SessionDB) -> None:
    """Table created and has expected columns."""
    with db._connect() as conn:  # noqa: SLF001
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    assert {
        "id",
        "session_id",
        "ts",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "batch",
    }.issubset(cols)


def test_record_llm_call_persists(db: SessionDB) -> None:
    db.record_llm_call(
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.025,
        batch=False,
        ts=1_000_000.0,
    )
    rows = db.query_llm_calls(days=None, group_by="model")
    assert len(rows) == 1
    assert rows[0]["key"] == "claude-opus-4-7"
    assert rows[0]["calls"] == 1
    assert rows[0]["input_tokens"] == 1000
    assert rows[0]["output_tokens"] == 500
    assert rows[0]["cost_usd"] == pytest.approx(0.025)


def test_record_llm_call_with_null_cost(db: SessionDB) -> None:
    db.record_llm_call(
        session_id="s-1",
        provider="local",
        model="some-unknown-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=None,  # pricing unknown
    )
    rows = db.query_llm_calls(days=None, group_by="model")
    assert rows[0]["all_cost_missing"] is True
    # SUM over a single NULL is NULL in SQLite, so cost_usd may be None.
    assert rows[0]["cost_usd"] is None


def test_record_llm_call_aggregates_across_calls(db: SessionDB) -> None:
    for _ in range(3):
        db.record_llm_call(
            session_id="s-1",
            provider="anthropic",
            model="claude-opus-4-7",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
        )
    rows = db.query_llm_calls(days=None)
    assert rows[0]["calls"] == 3
    assert rows[0]["input_tokens"] == 300
    assert rows[0]["output_tokens"] == 150
    assert rows[0]["cost_usd"] == pytest.approx(0.003)


def test_query_llm_calls_pre_v13_db_returns_empty(db: SessionDB) -> None:
    """If the table is missing somehow, return [] not a crash."""
    with db._connect() as conn:  # noqa: SLF001
        conn.execute("DROP TABLE IF EXISTS llm_calls")
    rows = db.query_llm_calls(days=None)
    assert rows == []


def test_record_llm_call_swallows_op_error(tmp_path: Path) -> None:
    """OperationalError on insert must not crash caller (telemetry)."""

    class _BadDB(SessionDB):
        def _txn(self):  # type: ignore[override]
            raise __import__("sqlite3").OperationalError("forced")

    bad = _BadDB(tmp_path / "bad.db")
    # Should not raise
    bad.record_llm_call(
        session_id="s-1",
        provider="x",
        model="y",
        input_tokens=1,
        output_tokens=1,
    )


# ─── usage_pricing module tests ─────────────────────────────────────────────


def test_record_call_with_known_pricing(db: SessionDB) -> None:
    """``record_call`` computes cost via cost_guard.compute_call_cost."""
    cost = record_call(
        db=db,
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Should be > 0 because Opus has a known pricing entry
    assert cost is not None
    assert cost > 0
    rows = db.query_llm_calls(days=None)
    assert rows[0]["cost_usd"] == pytest.approx(cost)


def test_record_call_with_unknown_model_records_null_cost(db: SessionDB) -> None:
    """Unknown model → cost is None but row is still recorded."""
    cost = record_call(
        db=db,
        session_id="s-1",
        provider="vendor-x",
        model="completely-made-up-model-name",
        input_tokens=10,
        output_tokens=5,
    )
    assert cost is None
    rows = db.query_llm_calls(days=None)
    assert len(rows) == 1
    assert rows[0]["all_cost_missing"] is True


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


def test_record_call_from_usage_dataclass(db: SessionDB) -> None:
    """Accepts a Usage dataclass (Provider contract shape)."""
    record_call_from_usage(
        db=db,
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        usage=_FakeUsage(input_tokens=200, output_tokens=100),
    )
    rows = db.query_llm_calls(days=None)
    assert rows[0]["input_tokens"] == 200
    assert rows[0]["output_tokens"] == 100


def test_record_call_from_usage_dict(db: SessionDB) -> None:
    """Also accepts a dict (e.g. from JSON-decoded telemetry)."""
    record_call_from_usage(
        db=db,
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        usage={"input_tokens": 50, "output_tokens": 25, "extra": "ignored"},
    )
    rows = db.query_llm_calls(days=None)
    assert rows[0]["input_tokens"] == 50
    assert rows[0]["output_tokens"] == 25


def test_record_call_from_usage_none(db: SessionDB) -> None:
    """None usage is a no-op (some providers omit usage)."""
    out = record_call_from_usage(
        db=db,
        session_id="s-1",
        provider="x",
        model="y",
        usage=None,
    )
    assert out is None
    rows = db.query_llm_calls(days=None)
    assert rows == []


def test_record_call_batch_discount(db: SessionDB) -> None:
    """batch=True applies provider discount."""
    full = record_call(
        db=db,
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        batch=False,
    )
    discounted = record_call(
        db=db,
        session_id="s-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        batch=True,
    )
    assert full is not None and discounted is not None
    assert discounted == pytest.approx(full * 0.5, rel=1e-6)
