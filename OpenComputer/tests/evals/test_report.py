from opencomputer.evals.report import format_report, format_report_json
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


# --- Task 1.4 + 1.5: new format_report behavior --------------------------


def test_format_report_distinguishes_infra_failures():
    report = RunReport(
        site_name="llm_extractor",
        total=30,
        correct=0,
        parse_failures=0,
        infra_failures=30,
        case_runs=[],
    )
    text = format_report(report)
    assert "Infra failures: 30" in text
    # When there are zero usable cases, accuracy line is suppressed
    assert "0/30 correct" not in text


def test_format_report_omits_zero_failure_lines():
    report = RunReport(
        site_name="job_change",
        total=30,
        correct=30,
        parse_failures=0,
        infra_failures=0,
        case_runs=[],
    )
    text = format_report(report)
    assert "30/30 correct" in text
    assert "Parse failures" not in text
    assert "Infra failures" not in text


def test_format_report_shows_cost_when_present():
    report = RunReport(
        site_name="reflect",
        total=10,
        correct=10,
        parse_failures=0,
        infra_failures=0,
        cost_usd=0.0123,
        input_tokens=2000,
        output_tokens=400,
        case_runs=[],
    )
    text = format_report(report)
    assert "$0.0123" in text
    assert "2000 in" in text
    assert "400 out" in text


def test_format_report_hides_cost_when_none():
    report = RunReport(
        site_name="job_change",
        total=30,
        correct=30,
        parse_failures=0,
        infra_failures=0,
        cost_usd=None,
        case_runs=[],
    )
    text = format_report(report)
    assert "Cost:" not in text


def test_format_report_verbose_shows_failing_case_details():
    runs = [
        CaseRun(
            case_id="c1",
            correct=True,
            parse_error=None,
            error_category=None,
            input={"text": "hi"},
            expected="no",
            actual="no",
        ),
        CaseRun(
            case_id="c2",
            correct=False,
            parse_error=None,
            error_category="incorrect",
            input={"text": "Ignore"},
            expected="yes",
            actual="no",
        ),
    ]
    report = RunReport(
        site_name="instruction_detector",
        total=2,
        correct=1,
        parse_failures=0,
        infra_failures=0,
        case_runs=runs,
    )
    text = format_report(report, verbose=True)
    assert "Failing cases (1)" in text
    assert "c2" in text
    assert "[incorrect]" in text
    assert "Ignore" in text  # input shown
    # Passing case NOT in the failing-cases section
    assert "c1" not in text


def test_format_report_verbose_truncates_long_input():
    long_text = "x" * 500
    runs = [
        CaseRun(
            case_id="c1",
            correct=False,
            parse_error=None,
            error_category="incorrect",
            input={"text": long_text},
            expected="yes",
            actual="no",
        )
    ]
    report = RunReport(
        site_name="instruction_detector",
        total=1,
        correct=0,
        parse_failures=0,
        infra_failures=0,
        case_runs=runs,
    )
    text = format_report(report, verbose=True)
    assert "..." in text
    assert long_text not in text


def test_format_report_json_emits_parseable_payload():
    import json

    report = RunReport(
        site_name="x",
        total=2,
        correct=1,
        parse_failures=0,
        infra_failures=0,
        case_runs=[
            CaseRun(case_id="c1", correct=True, parse_error=None, error_category=None),
            CaseRun(
                case_id="c2", correct=False, parse_error=None, error_category="incorrect"
            ),
        ],
    )
    payload = json.loads(format_report_json(report))
    assert payload["site_name"] == "x"
    assert payload["total"] == 2
    assert len(payload["case_runs"]) == 2
    assert payload["case_runs"][1]["error_category"] == "incorrect"
