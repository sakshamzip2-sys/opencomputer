from unittest.mock import MagicMock

from opencomputer.evals.graders.rubric import LLMRubricGrader
from opencomputer.evals.types import Case


def _make_mock_provider(response_text: str) -> MagicMock:
    provider = MagicMock()
    provider.complete.return_value = MagicMock(text=response_text)
    return provider


def test_rubric_grader_extracts_correct_from_result_tag(tmp_path):
    rubric_dir = tmp_path / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "test_v1.md").write_text("Is the response helpful?")

    provider = _make_mock_provider(
        "<thinking>Looks helpful.</thinking>\n<result>correct</result>"
    )
    grader = LLMRubricGrader(grader_provider=provider, rubric_dir=rubric_dir)

    case = Case(id="t1", input={}, rubric_id="test_v1")
    result = grader.grade("a helpful answer", case)

    assert result.correct is True
    assert result.reason is not None  # thinking captured for debug


def test_rubric_grader_extracts_incorrect_from_result_tag(tmp_path):
    rubric_dir = tmp_path / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "test_v1.md").write_text("Is the response helpful?")

    provider = _make_mock_provider(
        "<thinking>Not great.</thinking>\n<result>incorrect</result>"
    )
    grader = LLMRubricGrader(grader_provider=provider, rubric_dir=rubric_dir)

    case = Case(id="t1", input={}, rubric_id="test_v1")
    result = grader.grade("bad answer", case)

    assert result.correct is False


def test_rubric_grader_treats_missing_result_tag_as_incorrect_with_parse_error(tmp_path):
    rubric_dir = tmp_path / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "test_v1.md").write_text("Is the response helpful?")

    provider = _make_mock_provider("just some unstructured text")
    grader = LLMRubricGrader(grader_provider=provider, rubric_dir=rubric_dir)

    case = Case(id="t1", input={}, rubric_id="test_v1")
    result = grader.grade("answer", case)

    assert result.correct is False
    assert result.parse_error is not None


def test_rubric_grader_missing_rubric_file_raises(tmp_path):
    provider = _make_mock_provider("<result>correct</result>")
    grader = LLMRubricGrader(grader_provider=provider, rubric_dir=tmp_path)

    case = Case(id="t1", input={}, rubric_id="does_not_exist")
    try:
        grader.grade("answer", case)
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")
