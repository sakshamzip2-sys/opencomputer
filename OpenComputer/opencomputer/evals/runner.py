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
    error_category: str | None = None
    input: dict[str, Any] | None = None
    expected: Any | None = None
    actual: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    site_name: str
    total: int
    correct: int
    parse_failures: int
    infra_failures: int = 0
    case_runs: list[CaseRun] = field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def incorrect(self) -> int:
        return self.total - self.correct - self.parse_failures - self.infra_failures

    @property
    def usable_total(self) -> int:
        """Cases that produced a real signal — excludes infra failures."""
        return self.total - self.infra_failures

    @property
    def accuracy(self) -> float:
        return self.correct / self.usable_total if self.usable_total else 0.0

    @property
    def parse_failure_rate(self) -> float:
        return self.parse_failures / self.total if self.total else 0.0

    @property
    def infra_failure_rate(self) -> float:
        return self.infra_failures / self.total if self.total else 0.0


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


def _classify_unknown_exception(e: Exception) -> str:
    """Heuristic fallback when an exception doesn't carry a typed category.

    Patterns matching infrastructure failure (network, registry, daemon):
    classified as ``infra_error`` so they don't pollute regression accuracy.
    Everything else is treated as a real ``parse_error`` — model output broken.
    """
    cls_name = type(e).__name__.lower()
    if "unavailable" in cls_name or "connection" in cls_name or "timeout" in cls_name:
        return "infra_error"
    msg = str(e).lower()
    if any(kw in msg for kw in ("not registered", "connection", "unreachable", "timeout", "no such file")):
        return "infra_error"
    return "parse_error"


def run_site(
    *,
    site_name: str,
    cases_dir: Path,
    rubric_dir: Path | None = None,
    grader_provider=None,
    case_ids: list[str] | None = None,
) -> RunReport:
    """Load cases, invoke site adapter, grade each, return report.

    case_ids: optional list to filter runs to specific case IDs.
    """
    site = get_site(site_name)
    cases_path = cases_dir / f"{site_name}.jsonl"
    cases = _load_cases(cases_path)
    if case_ids is not None:
        cases = [c for c in cases if c.id in case_ids]

    rubric_dir = rubric_dir or (cases_dir.parent / "rubrics")

    callable_ = _resolve_callable(site.callable_path)
    grader = _build_grader(site, rubric_dir=rubric_dir, grader_provider=grader_provider)

    runs: list[CaseRun] = []
    correct_count = 0
    parse_failures = 0
    infra_failures = 0
    input_tokens_total = 0
    output_tokens_total = 0

    # Lazy import to avoid circular: profile_bootstrap may import opencomputer.evals via shims.
    from opencomputer.profile_bootstrap.llm_extractor import OllamaUnavailableError

    for case in cases:
        actual: Any = None
        try:
            actual = callable_(case.input)
            result: GradeResult = grader.grade(actual, case)
            # Default classification for unset error_category on failures.
            if not result.correct and result.error_category is None:
                category = "parse_error" if result.parse_error else "incorrect"
                result = GradeResult(
                    correct=result.correct,
                    score=result.score,
                    reason=result.reason,
                    parse_error=result.parse_error,
                    error_category=category,
                    extra=result.extra,
                )
        except OllamaUnavailableError as e:
            result = GradeResult(
                correct=False,
                parse_error=f"OllamaUnavailableError: {e}",
                error_category="infra_error",
            )
        except json.JSONDecodeError as e:
            result = GradeResult(
                correct=False,
                parse_error=f"JSONDecodeError: {e}",
                error_category="parse_error",
            )
        except Exception as e:  # noqa: BLE001 - eval must continue past site exceptions
            cat = _classify_unknown_exception(e)
            result = GradeResult(
                correct=False,
                parse_error=f"{type(e).__name__}: {e}",
                error_category=cat,
            )

        runs.append(
            CaseRun(
                case_id=case.id,
                correct=result.correct,
                parse_error=result.parse_error,
                error_category=result.error_category,
                input=case.input,
                expected=case.expected,
                actual=actual,
                extra=result.extra,
            )
        )
        if result.correct:
            correct_count += 1
        elif result.error_category == "infra_error":
            infra_failures += 1
        elif result.error_category == "parse_error":
            parse_failures += 1
        # incorrect counted via property

        if result.extra:
            input_tokens_total += result.extra.get("input_tokens", 0)
            output_tokens_total += result.extra.get("output_tokens", 0)

    return RunReport(
        site_name=site_name,
        total=len(cases),
        correct=correct_count,
        parse_failures=parse_failures,
        infra_failures=infra_failures,
        case_runs=runs,
        cost_usd=_estimate_cost(input_tokens_total, output_tokens_total, grader_provider),
        input_tokens=input_tokens_total,
        output_tokens=output_tokens_total,
    )


def _estimate_cost(in_tokens: int, out_tokens: int, provider) -> float | None:
    """USD estimate for known grader models. Returns None for unknown."""
    if provider is None or in_tokens + out_tokens == 0:
        return None
    model = getattr(provider, "_model", "")
    if "opus" in model.lower():
        # Anthropic Opus 4.7 list price ($15 input / $75 output per 1M)
        return (in_tokens * 15 + out_tokens * 75) / 1_000_000
    if "sonnet" in model.lower():
        # Anthropic Sonnet 4.6 list price ($3 input / $15 output per 1M)
        return (in_tokens * 3 + out_tokens * 15) / 1_000_000
    return None
