"""Tests for the gateway parity-telemetry probe (M1 / T1.2b–T1.7).

``opencomputer.gateway.parity_probe`` is the single seam through which
the dispatcher records which of the 10 parity-affecting mechanisms fired
on each gateway turn, and the reader functions ``oc gateway diagnose``
uses to render them.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.state import apply_migrations
from opencomputer.gateway.parity_probe import (
    MECHANISMS,
    ParityProbe,
    mechanism_label,
    query_parity_log,
    record_parity_observations,
    rollup_parity_log,
)


def _audit_db() -> Path:
    return Path(tempfile.mkdtemp()) / "audit.db"


# ── mechanism catalogue ──────────────────────────────────────────────


def test_ten_mechanisms_defined() -> None:
    assert len(MECHANISMS) == 10
    ids = [m.id for m in MECHANISMS]
    assert len(set(ids)) == 10  # all unique
    assert "prompt_override" in ids
    assert "reply_truncation" in ids
    assert "runtime_footer_off" in ids


def test_severity_weights_in_range() -> None:
    for m in MECHANISMS:
        assert 1 <= m.severity <= 4


def test_prompt_override_is_highest_severity() -> None:
    by_id = {m.id: m for m in MECHANISMS}
    assert by_id["prompt_override"].severity == 4


def test_mechanism_label_lookup() -> None:
    assert "PromptBuilder" in mechanism_label("prompt_override")
    assert mechanism_label("does_not_exist") == "does_not_exist"


# ── ParityProbe ──────────────────────────────────────────────────────


def test_probe_flush_writes_all_ten_rows() -> None:
    db = _audit_db()
    probe = ParityProbe(session_id="s1", turn_id=1, platform="telegram")
    probe.observe("prompt_override", True, {"override_len": 200})
    written = probe.flush(db)
    assert written == 10  # every mechanism emits a row, fired or not
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT mechanism_id, fired FROM gateway_parity_log"
    ).fetchall()
    assert len(rows) == 10
    fired = {mid: f for mid, f in rows}
    assert fired["prompt_override"] == 1
    assert fired["tool_allowlist"] == 0  # unobserved → not fired


def test_probe_detail_is_json_roundtrip() -> None:
    db = _audit_db()
    probe = ParityProbe(session_id="s1", turn_id=2, platform="discord")
    probe.observe("profile_rebind", True, {"from": "default", "to": "stocks"})
    probe.flush(db)
    rows = query_parity_log(db, session_id="s1")
    rebind = next(r for r in rows if r["mechanism_id"] == "profile_rebind")
    assert rebind["detail"]["to"] == "stocks"


def test_probe_rejects_unknown_mechanism() -> None:
    probe = ParityProbe(session_id="s1", turn_id=1, platform="telegram")
    with pytest.raises(ValueError):
        probe.observe("not_a_real_mechanism", True)


def test_probe_flush_swallows_db_errors() -> None:
    """A bad audit-db path must never raise — telemetry never wedges dispatch."""
    probe = ParityProbe(session_id="s1", turn_id=1, platform="telegram")
    probe.observe("prompt_override", True)
    # A directory path is not a writable sqlite file.
    written = probe.flush(Path("/nonexistent_dir_xyz/sub/audit.db"))
    assert written == 0


def test_record_parity_observations_direct() -> None:
    db = _audit_db()
    n = record_parity_observations(
        db,
        session_id="s2",
        turn_id=5,
        platform="slack",
        observations={"reply_truncation": (True, {"cut": 1200})},
    )
    assert n == 10
    rows = query_parity_log(db, session_id="s2")
    trunc = next(r for r in rows if r["mechanism_id"] == "reply_truncation")
    assert trunc["fired"] is True
    assert trunc["detail"]["cut"] == 1200


# ── readers ──────────────────────────────────────────────────────────


def test_query_filters_by_session() -> None:
    db = _audit_db()
    ParityProbe(session_id="a", turn_id=1, platform="telegram").flush(db)
    ParityProbe(session_id="b", turn_id=1, platform="discord").flush(db)
    assert all(r["session_id"] == "a" for r in query_parity_log(db, session_id="a"))
    assert len(query_parity_log(db, session_id="a")) == 10


def test_query_on_missing_db_returns_empty() -> None:
    assert query_parity_log(Path("/no/such/audit.db")) == []


def test_rollup_computes_fire_rate_and_priority() -> None:
    db = _audit_db()
    # turn 1: prompt_override fires.  turn 2: it does not.
    p1 = ParityProbe(session_id="s", turn_id=1, platform="telegram")
    p1.observe("prompt_override", True)
    p1.flush(db)
    p2 = ParityProbe(session_id="s", turn_id=2, platform="telegram")
    p2.flush(db)

    rollup = rollup_parity_log(db)
    by_id = {r["mechanism_id"]: r for r in rollup}
    po = by_id["prompt_override"]
    assert po["turns"] == 2
    assert po["fired_count"] == 1
    assert po["fire_rate"] == pytest.approx(0.5)
    # priority = fire_rate * severity (4 for prompt_override)
    assert po["priority_score"] == pytest.approx(2.0)
    # rollup is ordered by priority_score descending
    scores = [r["priority_score"] for r in rollup]
    assert scores == sorted(scores, reverse=True)


def test_rollup_on_empty_db_returns_all_mechanisms_at_zero() -> None:
    db = _audit_db()
    apply_migrations(sqlite3.connect(db))  # table exists, no rows
    rollup = rollup_parity_log(db)
    assert len(rollup) == 10
    assert all(r["fire_rate"] == 0.0 for r in rollup)
    assert all(r["turns"] == 0 for r in rollup)
