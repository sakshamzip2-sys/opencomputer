import json
from pathlib import Path

from opencomputer.evals.baseline import (
    BaselineSnapshot,
    compare_to_baseline,
    save_baseline,
)
from opencomputer.evals.runner import RunReport


def test_save_baseline_writes_json(tmp_path):
    report = RunReport(
        site_name="instruction_detector",
        total=10,
        correct=8,
        parse_failures=1,
    )
    save_baseline(report, baselines_dir=tmp_path, model="claude-sonnet-4-6", provider="anthropic")
    path = tmp_path / "instruction_detector.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["accuracy"] == 0.8
    assert data["parse_failure_rate"] == 0.1
    assert data["model"] == "claude-sonnet-4-6"


def test_compare_to_baseline_no_baseline_returns_none(tmp_path):
    report = RunReport(site_name="instruction_detector", total=10, correct=8, parse_failures=0)
    diff = compare_to_baseline(report, baselines_dir=tmp_path)
    assert diff is None


def test_compare_to_baseline_returns_delta(tmp_path):
    base = BaselineSnapshot(
        site_name="instruction_detector",
        accuracy=0.7,
        parse_failure_rate=0.2,
        timestamp="2026-05-01T00:00:00Z",
        model="claude-sonnet-4-6",
        provider="anthropic",
    )
    (tmp_path / "instruction_detector.json").write_text(json.dumps(base.__dict__))

    report = RunReport(site_name="instruction_detector", total=10, correct=8, parse_failures=0)
    diff = compare_to_baseline(report, baselines_dir=tmp_path)
    assert diff is not None
    assert diff.accuracy_delta == 0.8 - 0.7
    assert diff.parse_failure_rate_delta == 0.0 - 0.2
