"""Tests for :class:`opencomputer.user_model.drift_store.DriftStore`.

Covers the round-trip + filtered-list + retention behaviours described
in the Phase 3.D plan.
"""

from __future__ import annotations

import time
from pathlib import Path

from opencomputer.user_model.drift_store import DriftStore
from plugin_sdk.decay import DriftReport


def _store(tmp_path: Path) -> DriftStore:
    return DriftStore(db_path=tmp_path / "drift.sqlite")


def _report(
    *,
    total_kl: float = 0.1,
    significant: bool = False,
    age_seconds: float = 0.0,
) -> DriftReport:
    return DriftReport(
        window_seconds=604800.0,
        total_kl_divergence=total_kl,
        per_kind_drift={"temporal": total_kl / 2.0},
        recent_distribution={"temporal/Read": 3},
        lifetime_distribution={"temporal/Read": 5},
        top_changes=({"label": "temporal/Read", "recent_count": 3, "lifetime_count": 5, "delta_ratio": 1.2},),
        significant=significant,
        created_at=time.time() - age_seconds,
    )


def test_insert_and_get_report_round_trip(tmp_path: Path) -> None:
    """Insert a report, fetch it back — every field survives."""
    store = _store(tmp_path)
    original = _report(total_kl=0.4, significant=True)
    store.insert(original)
    fetched = store.get(original.report_id)
    assert fetched is not None
    assert fetched.report_id == original.report_id
    assert fetched.total_kl_divergence == 0.4
    assert fetched.significant is True
    assert dict(fetched.per_kind_drift) == {"temporal": 0.2}
    assert dict(fetched.recent_distribution) == {"temporal/Read": 3}
    assert dict(fetched.lifetime_distribution) == {"temporal/Read": 5}
    assert len(fetched.top_changes) == 1
    tc = fetched.top_changes[0]
    assert tc["label"] == "temporal/Read"
    assert tc["recent_count"] == 3


def test_list_filters_significant_only(tmp_path: Path) -> None:
    """``significant_only=True`` skips unflagged rows."""
    store = _store(tmp_path)
    a = _report(total_kl=0.1, significant=False)
    b = _report(total_kl=0.9, significant=True)
    store.insert(a)
    store.insert(b)
    rows = store.list(significant_only=True)
    ids = {r.report_id for r in rows}
    assert ids == {b.report_id}


def test_list_filters_by_since(tmp_path: Path) -> None:
    """``since=...`` skips rows older than the threshold."""
    store = _store(tmp_path)
    # One old (2 hours ago), one fresh.
    old = _report(total_kl=0.1, age_seconds=7200.0)
    new = _report(total_kl=0.2, age_seconds=0.0)
    store.insert(old)
    store.insert(new)
    rows = store.list(since=time.time() - 3600.0)
    ids = {r.report_id for r in rows}
    assert ids == {new.report_id}


def test_delete_older_than_removes_old(tmp_path: Path) -> None:
    """Old rows are purged; fresh ones survive."""
    store = _store(tmp_path)
    old = _report(age_seconds=7200.0)
    new = _report(age_seconds=0.0)
    store.insert(old)
    store.insert(new)
    deleted = store.delete_older_than(3600.0)
    assert deleted == 1
    remaining = {r.report_id for r in store.list(limit=100)}
    assert remaining == {new.report_id}
