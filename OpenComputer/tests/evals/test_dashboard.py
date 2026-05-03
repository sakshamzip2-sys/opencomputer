"""Phase 6 — static HTML dashboard renderer."""

from __future__ import annotations

from pathlib import Path

from opencomputer.evals.dashboard import render_dashboard
from opencomputer.evals.history import record_run
from opencomputer.evals.runner import CaseRun, RunReport


def _seed(db_path: Path) -> None:
    for site in ["job_change", "instruction_detector"]:
        for i in range(5):
            record_run(
                RunReport(
                    site_name=site,
                    total=10,
                    correct=9 - (i % 2),
                    parse_failures=0,
                    infra_failures=0,
                    case_runs=[
                        CaseRun(
                            case_id="c1",
                            correct=False,
                            parse_error=None,
                            error_category="incorrect",
                            input={"x": 1},
                            expected="yes",
                            actual="no",
                        )
                    ],
                ),
                db_path=db_path,
                model="m",
                provider="p",
            )


def test_render_dashboard_writes_html(tmp_path):
    db_path = tmp_path / "history.db"
    _seed(db_path)
    out_path = tmp_path / "dashboard.html"

    render_dashboard(db_path=db_path, out_path=out_path, limit=20)

    assert out_path.exists()
    html = out_path.read_text()
    assert "OpenComputer eval dashboard" in html
    assert "job_change" in html
    assert "instruction_detector" in html
    assert "<svg" in html  # sparkline


def test_render_dashboard_handles_empty_history(tmp_path):
    db_path = tmp_path / "history.db"
    out_path = tmp_path / "dashboard.html"
    render_dashboard(db_path=db_path, out_path=out_path, limit=20)
    html = out_path.read_text()
    assert "No history yet" in html


def test_render_dashboard_shows_failing_cases(tmp_path):
    db_path = tmp_path / "history.db"
    record_run(
        RunReport(
            site_name="x",
            total=2,
            correct=1,
            parse_failures=0,
            infra_failures=0,
            case_runs=[
                CaseRun(case_id="c1", correct=True, parse_error=None),
                CaseRun(
                    case_id="c2_fails",
                    correct=False,
                    parse_error=None,
                    error_category="incorrect",
                    input={"text": "abc"},
                    expected="yes",
                    actual="no",
                ),
            ],
        ),
        db_path=db_path,
        model="m",
        provider="p",
    )

    out_path = tmp_path / "dashboard.html"
    render_dashboard(db_path=db_path, out_path=out_path)
    html = out_path.read_text()
    assert "c2_fails" in html
    # Passing case should NOT appear in failing-cases dropdown
    # (it might appear in summary metrics; for now just confirm c2_fails shows)
    assert "incorrect" in html.lower()
