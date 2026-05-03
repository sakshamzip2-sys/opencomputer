"""Format eval results for terminal output (text + JSON variants)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from opencomputer.evals.baseline import BaselineDiff
from opencomputer.evals.runner import RunReport


def _truncate(value: Any, limit: int = 120) -> str:
    s = repr(value) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[: limit - 3] + "..."


def format_report(
    report: RunReport,
    *,
    baseline_diff: BaselineDiff | None = None,
    verbose: bool = False,
) -> str:
    """Multi-line, terminal-friendly report.

    verbose=True surfaces failing-case detail (input/expected/actual/error).
    """
    lines = [f"Site: {report.site_name}"]

    if report.usable_total > 0:
        lines.append(
            f"  Cases: {report.correct}/{report.usable_total} correct "
            f"({report.accuracy:.1%})"
        )
    else:
        lines.append("  Cases: 0/0 (no usable cases — all infra failures)")

    if report.parse_failures:
        lines.append(
            f"  Parse failures: {report.parse_failures} ({report.parse_failure_rate:.1%})"
        )
    if report.infra_failures:
        lines.append(
            f"  Infra failures: {report.infra_failures} ({report.infra_failure_rate:.1%})"
        )

    if report.cost_usd is not None and report.cost_usd > 0:
        lines.append(
            f"  Cost: ${report.cost_usd:.4f} "
            f"({report.input_tokens} in / {report.output_tokens} out)"
        )

    if baseline_diff is not None:
        sign = "+" if baseline_diff.accuracy_delta >= 0 else ""
        lines.append(
            f"  vs baseline ({baseline_diff.baseline.timestamp[:10]}, "
            f"{baseline_diff.baseline.model}): "
            f"{sign}{baseline_diff.accuracy_delta:.2%} accuracy, "
            f"{baseline_diff.parse_failure_rate_delta:+.2%} parse-failure rate"
        )

    if verbose:
        failing = [c for c in report.case_runs if not c.correct]
        if failing:
            lines.append(f"  Failing cases ({len(failing)}):")
            for c in failing:
                lines.append(f"    {c.case_id}  [{c.error_category or 'incorrect'}]")
                if c.input is not None:
                    lines.append(f"      input: {_truncate(c.input)}")
                if c.expected is not None:
                    lines.append(f"      expected: {_truncate(c.expected)}")
                if c.actual is not None:
                    lines.append(f"      actual:   {_truncate(c.actual)}")
                if c.parse_error:
                    lines.append(f"      error: {_truncate(c.parse_error)}")

    return "\n".join(lines)


def format_report_json(
    report: RunReport, *, baseline_diff: BaselineDiff | None = None
) -> str:
    """Machine-readable representation of one site's run."""
    payload: dict[str, Any] = {
        "site_name": report.site_name,
        "total": report.total,
        "correct": report.correct,
        "incorrect": report.incorrect,
        "parse_failures": report.parse_failures,
        "infra_failures": report.infra_failures,
        "accuracy": report.accuracy,
        "cost_usd": report.cost_usd,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "case_runs": [
            {
                "case_id": c.case_id,
                "correct": c.correct,
                "error_category": c.error_category,
                "input": c.input,
                "expected": c.expected,
                "actual": c.actual,
                "parse_error": c.parse_error,
            }
            for c in report.case_runs
        ],
    }
    if baseline_diff is not None:
        payload["baseline_diff"] = {
            "accuracy_delta": baseline_diff.accuracy_delta,
            "parse_failure_rate_delta": baseline_diff.parse_failure_rate_delta,
            "baseline": asdict(baseline_diff.baseline),
        }
    return json.dumps(payload, indent=2, default=str)
