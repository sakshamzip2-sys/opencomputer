"""Phase 5 — SQLite run history with retention policy."""

from __future__ import annotations

from opencomputer.evals.history import (
    list_sites_with_history,
    load_recent_runs,
    prune_to_limit,
    record_run,
)
from opencomputer.evals.runner import CaseRun, RunReport


def _make_report(site: str = "x", correct: int = 10) -> RunReport:
    return RunReport(
        site_name=site,
        total=10,
        correct=correct,
        parse_failures=0,
        infra_failures=0,
        case_runs=[
            CaseRun(case_id=f"c{i}", correct=i < correct, parse_error=None)
            for i in range(10)
        ],
    )


def test_record_and_load(tmp_path):
    db_path = tmp_path / "history.db"
    record_run(_make_report(), db_path=db_path, model="m", provider="p")
    rows = load_recent_runs("x", db_path=db_path, limit=10)
    assert len(rows) == 1
    assert rows[0]["accuracy"] == 1.0
    assert rows[0]["site_name"] == "x"


def test_prune_keeps_only_limit(tmp_path):
    db_path = tmp_path / "history.db"
    for i in range(105):
        record_run(_make_report(correct=i % 10), db_path=db_path, model="m", provider="p")
    # Each record_run already prunes; verify direct prune is also idempotent
    prune_to_limit("x", limit=100, db_path=db_path)
    rows = load_recent_runs("x", db_path=db_path, limit=200)
    assert len(rows) == 100


def test_load_recent_orders_by_timestamp_desc(tmp_path):
    db_path = tmp_path / "history.db"
    for i in range(3):
        record_run(_make_report(correct=i), db_path=db_path, model="m", provider="p")
    rows = load_recent_runs("x", db_path=db_path, limit=10)
    # Most recent first — last recorded had correct=2
    assert rows[0]["correct"] == 2


def test_list_sites_with_history(tmp_path):
    db_path = tmp_path / "history.db"
    record_run(_make_report(site="a"), db_path=db_path, model="m", provider="p")
    record_run(_make_report(site="b"), db_path=db_path, model="m", provider="p")
    record_run(_make_report(site="a"), db_path=db_path, model="m", provider="p")

    sites = list_sites_with_history(db_path)
    assert sites == ["a", "b"]


def test_load_returns_empty_when_db_missing(tmp_path):
    db_path = tmp_path / "missing.db"
    assert load_recent_runs("anything", db_path=db_path, limit=10) == []
    assert list_sites_with_history(db_path) == []


def test_record_run_persists_case_runs_json(tmp_path):
    db_path = tmp_path / "history.db"
    record_run(_make_report(correct=5), db_path=db_path, model="m", provider="p")
    rows = load_recent_runs("x", db_path=db_path, limit=10)
    import json

    payload = json.loads(rows[0]["case_runs_json"])
    assert len(payload) == 10
    assert payload[5]["correct"] is False
    assert payload[4]["correct"] is True
