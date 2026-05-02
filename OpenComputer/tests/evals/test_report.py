from opencomputer.evals.report import format_report
from opencomputer.evals.runner import CaseRun, RunReport


def test_format_report_includes_site_name_and_accuracy():
    report = RunReport(
        site_name="instruction_detector",
        total=10,
        correct=8,
        parse_failures=1,
        case_runs=[CaseRun(case_id=f"c{i}", correct=(i < 8), parse_error=None) for i in range(10)],
    )
    text = format_report(report)
    assert "instruction_detector" in text
    assert "8/10" in text or "80.0%" in text
    assert "parse failures: 1" in text.lower() or "parse_failures: 1" in text.lower()


def test_format_report_includes_baseline_diff_when_provided():
    from opencomputer.evals.baseline import BaselineDiff, BaselineSnapshot

    report = RunReport(site_name="instruction_detector", total=10, correct=8, parse_failures=0)
    diff = BaselineDiff(
        site_name="instruction_detector",
        accuracy_delta=0.1,
        parse_failure_rate_delta=-0.05,
        baseline=BaselineSnapshot(
            site_name="instruction_detector",
            accuracy=0.7,
            parse_failure_rate=0.05,
            timestamp="2026-05-01T00:00:00Z",
            model="claude-sonnet-4-6",
            provider="anthropic",
        ),
        current_accuracy=0.8,
        current_parse_failure_rate=0.0,
    )
    text = format_report(report, baseline_diff=diff)
    assert "+0.10" in text or "+10.00%" in text
