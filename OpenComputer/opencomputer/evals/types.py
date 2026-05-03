"""Core types for the eval harness.

Single source of truth for EvalSite, Case, GradeResult. Other modules
import from here. No imports from opencomputer.* (one-directional
dependency: evals -> core, never core -> evals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GraderKind = Literal["exact", "schema", "rubric"]
ErrorCategory = Literal["incorrect", "parse_error", "infra_error"]


@dataclass(frozen=True)
class EvalSite:
    """Registry entry for one evaluable LLM call site."""

    name: str
    callable_path: str
    """Module path of the callable, e.g. 'opencomputer.evolution.reflect:reflect'."""

    grader: GraderKind
    schema: dict | None = None
    rubric_id: str | None = None
    requires_provider: bool = True


@dataclass(frozen=True)
class Case:
    """One test case loaded from JSONL."""

    id: str
    input: dict[str, Any]
    expected: Any | None = None
    rubric_id: str | None = None


@dataclass
class GradeResult:
    """Outcome of grading one case."""

    correct: bool
    score: float | None = None
    reason: str | None = None
    parse_error: str | None = None
    error_category: ErrorCategory | None = None
    extra: dict[str, Any] = field(default_factory=dict)
