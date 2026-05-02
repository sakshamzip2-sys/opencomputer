"""Runner: orchestrates load -> invoke -> grade for a site."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencomputer.evals.graders.exact import ExactMatchGrader
from opencomputer.evals.graders.rubric import LLMRubricGrader
from opencomputer.evals.graders.schema import SchemaMatchGrader
from opencomputer.evals.sites import get_site
from opencomputer.evals.types import Case, EvalSite, GradeResult


@dataclass
class CaseRun:
    case_id: str
    correct: bool
    parse_error: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    site_name: str
    total: int
    correct: int
    parse_failures: int
    case_runs: list[CaseRun] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def parse_failure_rate(self) -> float:
        return self.parse_failures / self.total if self.total else 0.0


def _load_cases(path: Path) -> list[Case]:
    if not path.exists():
        return []
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        cases.append(
            Case(
                id=d["id"],
                input=d["input"],
                expected=d.get("expected"),
                rubric_id=d.get("rubric_id"),
            )
        )
    return cases


def _resolve_callable(callable_path: str):
    module_path, _, attr = callable_path.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _build_grader(site: EvalSite, *, rubric_dir: Path, grader_provider):
    if site.grader == "exact":
        return ExactMatchGrader()
    if site.grader == "schema":
        return SchemaMatchGrader(mode="subset")
    if site.grader == "rubric":
        if grader_provider is None:
            raise ValueError(
                f"site {site.name!r} uses rubric grader but no grader_provider given"
            )
        return LLMRubricGrader(grader_provider=grader_provider, rubric_dir=rubric_dir)
    raise ValueError(f"unknown grader kind: {site.grader}")


def run_site(
    *,
    site_name: str,
    cases_dir: Path,
    rubric_dir: Path | None = None,
    grader_provider=None,
) -> RunReport:
    """Load cases, invoke site adapter, grade each, return report."""
    site = get_site(site_name)
    cases_path = cases_dir / f"{site_name}.jsonl"
    cases = _load_cases(cases_path)

    rubric_dir = rubric_dir or (cases_dir.parent / "rubrics")

    callable_ = _resolve_callable(site.callable_path)
    grader = _build_grader(site, rubric_dir=rubric_dir, grader_provider=grader_provider)

    runs: list[CaseRun] = []
    correct_count = 0
    parse_failures = 0

    for case in cases:
        try:
            actual = callable_(case.input)
            result: GradeResult = grader.grade(actual, case)
        except json.JSONDecodeError as e:
            result = GradeResult(correct=False, parse_error=f"JSONDecodeError: {e}")
        except Exception as e:  # noqa: BLE001 - eval must continue past site exceptions
            result = GradeResult(correct=False, parse_error=f"{type(e).__name__}: {e}")

        runs.append(
            CaseRun(
                case_id=case.id,
                correct=result.correct,
                parse_error=result.parse_error,
            )
        )
        if result.correct:
            correct_count += 1
        if result.parse_error:
            parse_failures += 1

    return RunReport(
        site_name=site_name,
        total=len(cases),
        correct=correct_count,
        parse_failures=parse_failures,
        case_runs=runs,
    )
