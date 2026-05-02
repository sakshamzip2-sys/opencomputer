from opencomputer.evals.graders.exact import ExactMatchGrader
from opencomputer.evals.types import Case


def test_exact_grader_matches_case_insensitive():
    grader = ExactMatchGrader()
    case = Case(id="t1", input={}, expected="yes")
    result = grader.grade("YES", case)
    assert result.correct is True


def test_exact_grader_strips_whitespace():
    grader = ExactMatchGrader()
    case = Case(id="t1", input={}, expected="no")
    result = grader.grade("  no  ", case)
    assert result.correct is True


def test_exact_grader_returns_false_on_mismatch():
    grader = ExactMatchGrader()
    case = Case(id="t1", input={}, expected="yes")
    result = grader.grade("maybe", case)
    assert result.correct is False
