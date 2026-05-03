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


def test_baseline_snapshot_persists_cost(tmp_path):
    """Phase 4 — cost_usd, input_tokens, output_tokens round-trip."""
    from opencomputer.evals.baseline import _load_baseline

    report = RunReport(
        site_name="reflect",
        total=10,
        correct=8,
        parse_failures=0,
        infra_failures=0,
        cost_usd=0.0123,
        input_tokens=2000,
        output_tokens=400,
    )
    save_baseline(report, baselines_dir=tmp_path, model="claude-sonnet-4-6", provider="anthropic")
    snap = _load_baseline(tmp_path, "reflect")
    assert snap is not None
    assert snap.cost_usd == 0.0123
    assert snap.input_tokens == 2000
    assert snap.output_tokens == 400


def test_old_baseline_json_loads_with_new_fields_defaulted(tmp_path):
    """Phase 4 — backward compat: pre-cost-tracking JSON files load fine."""
    from opencomputer.evals.baseline import _load_baseline

    old_payload = {
        "site_name": "job_change",
        "accuracy": 1.0,
        "parse_failure_rate": 0.0,
        "timestamp": "2026-05-02T14:42:00.237745+00:00",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
    }
    (tmp_path / "job_change.json").write_text(json.dumps(old_payload))
    snap = _load_baseline(tmp_path, "job_change")
    assert snap is not None
    assert snap.accuracy == 1.0
    assert snap.cost_usd is None
    assert snap.input_tokens == 0
    assert snap.output_tokens == 0
