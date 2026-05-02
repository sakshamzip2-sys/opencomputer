"""OpenComputer eval harness.

Public API:
    run(site_name, *, provider=None, grader_model=None) -> RunReport
    generate(site_name, *, n=30) -> Path  # writes candidates JSONL
    regress(site_name) -> RegressionReport
"""

from opencomputer.evals.types import Case, EvalSite, GradeResult

__all__ = ["Case", "EvalSite", "GradeResult"]
