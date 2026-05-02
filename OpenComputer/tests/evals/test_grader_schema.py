from opencomputer.evals.graders.schema import SchemaMatchGrader
from opencomputer.evals.types import Case


def test_subset_mode_passes_when_all_expected_fields_match():
    grader = SchemaMatchGrader(mode="subset")
    case = Case(id="t1", input={}, expected={"name": "Alice", "role": "engineer"})
    actual = {"name": "Alice", "role": "engineer", "extra": "ignored"}
    assert grader.grade(actual, case).correct is True


def test_subset_mode_fails_when_field_missing():
    grader = SchemaMatchGrader(mode="subset")
    case = Case(id="t1", input={}, expected={"name": "Alice", "role": "engineer"})
    actual = {"name": "Alice"}
    assert grader.grade(actual, case).correct is False


def test_subset_mode_fails_when_field_value_differs():
    grader = SchemaMatchGrader(mode="subset")
    case = Case(id="t1", input={}, expected={"name": "Alice"})
    actual = {"name": "Bob"}
    assert grader.grade(actual, case).correct is False


def test_strict_mode_fails_on_extra_fields():
    grader = SchemaMatchGrader(mode="strict")
    case = Case(id="t1", input={}, expected={"name": "Alice"})
    actual = {"name": "Alice", "role": "extra"}
    assert grader.grade(actual, case).correct is False


def test_partial_mode_returns_score():
    grader = SchemaMatchGrader(mode="partial")
    case = Case(id="t1", input={}, expected={"a": 1, "b": 2, "c": 3})
    actual = {"a": 1, "b": 2, "c": 99}
    result = grader.grade(actual, case)
    assert result.score == 2 / 3
    assert result.correct is False  # partial mode: correct only if score == 1.0


def test_records_parse_error_when_actual_is_not_dict():
    grader = SchemaMatchGrader(mode="subset")
    case = Case(id="t1", input={}, expected={"name": "Alice"})
    result = grader.grade("not a dict", case)
    assert result.correct is False
    assert result.parse_error is not None
