"""Exact-match grader for classification call sites."""

from opencomputer.evals.types import Case, GradeResult


class ExactMatchGrader:
    """Grader: actual.strip().lower() == case.expected.lower()."""

    def grade(self, actual: object, case: Case) -> GradeResult:
        if case.expected is None:
            raise ValueError(f"ExactMatchGrader requires case.expected on {case.id!r}")
        actual_str = str(actual).strip().lower()
        expected_str = str(case.expected).strip().lower()
        return GradeResult(correct=(actual_str == expected_str))
