"""Schema-match grader for structured-extraction call sites."""

from typing import Literal

from opencomputer.evals.types import Case, GradeResult

SchemaMode = Literal["strict", "subset", "partial"]


class SchemaMatchGrader:
    """Grader: compares actual dict against case.expected by field.

    Modes:
      - strict: actual keys == expected keys AND values match
      - subset: actual >= expected (extras allowed); values must match for expected keys
      - partial: returns score = (matching expected fields) / (total expected fields)
    """

    def __init__(self, mode: SchemaMode = "subset"):
        self.mode = mode

    def grade(self, actual: object, case: Case) -> GradeResult:
        if case.expected is None or not isinstance(case.expected, dict):
            raise ValueError(
                f"SchemaMatchGrader requires dict case.expected on {case.id!r}"
            )

        if not isinstance(actual, dict):
            return GradeResult(
                correct=False,
                parse_error=f"actual is {type(actual).__name__}, expected dict",
            )

        expected = case.expected

        if self.mode == "strict":
            keys_match = set(actual.keys()) == set(expected.keys())
            values_match = all(actual.get(k) == v for k, v in expected.items())
            return GradeResult(correct=(keys_match and values_match))

        if self.mode == "subset":
            all_present_and_equal = all(
                k in actual and actual[k] == v for k, v in expected.items()
            )
            return GradeResult(correct=all_present_and_equal)

        # partial
        if not expected:
            return GradeResult(correct=True, score=1.0)
        match_count = sum(1 for k, v in expected.items() if actual.get(k) == v)
        score = match_count / len(expected)
        return GradeResult(correct=(score == 1.0), score=score)
