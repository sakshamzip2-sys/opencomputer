"""LLM-rubric grader for open-ended call sites.

Uses a provider DIFFERENT from the one being evaluated. Reasons in
<thinking>, decides in <result>, discards reasoning except for debug.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from opencomputer.evals.types import Case, GradeResult


class _GraderProvider(Protocol):
    def complete(self, prompt: str) -> Any: ...


_RESULT_RE = re.compile(r"<result>\s*(\w+)\s*</result>", re.IGNORECASE)
_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.IGNORECASE | re.DOTALL)


class LLMRubricGrader:
    """Grader: asks a different LLM to grade actual against a rubric."""

    PROMPT_TEMPLATE = """Grade the following response against the rubric.

<rubric>
{rubric}
</rubric>

<response>
{response}
</response>

Reason through your grading in <thinking> tags, then output exactly 'correct' or 'incorrect' inside <result> tags."""

    def __init__(self, grader_provider: _GraderProvider, rubric_dir: Path):
        self.provider = grader_provider
        self.rubric_dir = Path(rubric_dir)

    def grade(self, actual: object, case: Case) -> GradeResult:
        if case.rubric_id is None:
            raise ValueError(
                f"LLMRubricGrader requires case.rubric_id on {case.id!r}"
            )

        rubric_path = self.rubric_dir / f"{case.rubric_id}.md"
        rubric_text = rubric_path.read_text(encoding="utf-8")

        prompt = self.PROMPT_TEMPLATE.format(rubric=rubric_text, response=str(actual))
        response = self.provider.complete(prompt)
        text = getattr(response, "text", str(response))

        result_match = _RESULT_RE.search(text)
        thinking_match = _THINKING_RE.search(text)
        reasoning = thinking_match.group(1).strip() if thinking_match else None

        if not result_match:
            return GradeResult(
                correct=False,
                reason=reasoning,
                parse_error="no <result>...</result> tag in grader response",
            )

        verdict = result_match.group(1).strip().lower()
        return GradeResult(
            correct=(verdict == "correct"),
            reason=reasoning,
        )
