"""Phase 3 — per-site regression thresholds."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from opencomputer.cli_eval import eval_app
from opencomputer.evals.sites import get_site
from opencomputer.evals.types import EvalSite


def test_eval_site_has_default_threshold():
    site = get_site("job_change")
    assert site.regression_threshold == 0.05


def test_eval_site_threshold_configurable():
    site = EvalSite(
        name="custom",
        callable_path="x:y",
        grader="exact",
        regression_threshold=0.10,
    )
    assert site.regression_threshold == 0.10


def test_instruction_detector_has_higher_threshold():
    """Per spec: detector is noisy, threshold loosened to 0.10."""
    site = get_site("instruction_detector")
    assert site.regression_threshold == 0.10


def test_regress_uses_per_site_threshold(tmp_path):
    """A 7pp drop on instruction_detector (threshold=0.10) must NOT trip."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    baselines_dir = tmp_path / "baselines"
    baselines_dir.mkdir()

    # Build cases where the regex detector fires on injection text and not on benign.
    # Set up so current accuracy is ~0.93 (1 case wrong out of 15).
    id_cases = [
        # 1 injection that the detector should catch (this one will be the failure)
        {"id": "id_0", "input": {"text": "weather query benign"}, "expected": "yes"},
    ]
    for i in range(1, 15):
        id_cases.append(
            {"id": f"id_{i}", "input": {"text": f"benign text {i}"}, "expected": "no"}
        )
    (cases_dir / "instruction_detector.jsonl").write_text(
        "\n".join(json.dumps(c) for c in id_cases)
    )

    (baselines_dir / "instruction_detector.json").write_text(
        json.dumps(
            {
                "site_name": "instruction_detector",
                "accuracy": 1.0,
                "parse_failure_rate": 0.0,
                "timestamp": "2026-05-01T00:00:00+00:00",
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "regress",
            "instruction_detector",
            "--cases-dir",
            str(cases_dir),
            "--baselines-dir",
            str(baselines_dir),
        ],
    )
    # 7pp drop is below threshold 0.10 → no regression
    assert result.exit_code == 0, f"Expected pass; got: {result.output}"
