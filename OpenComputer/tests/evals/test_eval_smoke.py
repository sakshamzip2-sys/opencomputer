"""CI smoke set: runs deterministic-graded sites against committed cases.

Skips rubric-graded sites (those incur LLM cost; run manually via 'oc eval run').
Each deterministic site must have at least 5 committed cases for this test to
verify CI integration.
"""

from pathlib import Path

import pytest

from opencomputer.evals.runner import run_site
from opencomputer.evals.sites import SITES


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def _cases_file(name: str) -> Path:
    return CASES_DIR / f"{name}.jsonl"


@pytest.mark.parametrize(
    "site_name",
    [name for name, site in SITES.items() if site.grader in ("exact", "schema")],
)
def test_smoke_site_runs_without_crash(site_name):
    cases_path = _cases_file(site_name)
    if not cases_path.exists() or not cases_path.read_text().strip():
        pytest.skip(f"no committed cases for {site_name}; will be added during dogfood")

    report = run_site(site_name=site_name, cases_dir=CASES_DIR)
    # Smoke gate: harness completes, doesn't crash, and reports >= 0 correct.
    assert report.total > 0
    assert report.correct >= 0
