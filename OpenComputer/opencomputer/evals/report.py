"""Format eval results for terminal output."""

from __future__ import annotations

from opencomputer.evals.baseline import BaselineDiff
from opencomputer.evals.runner import RunReport


def format_report(report: RunReport, *, baseline_diff: BaselineDiff | None = None) -> str:
    """Multi-line table-shaped string."""
    lines = []
    lines.append(f"Site: {report.site_name}")
    lines.append(f"  Cases: {report.correct}/{report.total} correct ({report.accuracy:.1%})")
    lines.append(f"  Parse failures: {report.parse_failures} ({report.parse_failure_rate:.1%})")

    if baseline_diff is not None:
        sign = "+" if baseline_diff.accuracy_delta >= 0 else ""
        lines.append(
            f"  vs baseline ({baseline_diff.baseline.timestamp[:10]}, "
            f"{baseline_diff.baseline.model}): "
            f"{sign}{baseline_diff.accuracy_delta:.2%} accuracy, "
            f"{baseline_diff.parse_failure_rate_delta:+.2%} parse-failure rate"
        )

    return "\n".join(lines)
