# Eval System v2 (Scope C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all four broken eval sites, add per-case drilldown reporting, cost tracking, per-site regression thresholds, persistent run history, a static HTML dashboard, atomic candidate promotion, and CI integration — turning the eval harness from "shipped but unusable end-to-end" into a daily-usable quality gate.

**Architecture:** Layer additively on the existing `opencomputer/evals/*` modules. New modules: `history.py`, `dashboard.py`, `promote.py`, `templates/dashboard.html.j2`. Strict one-way dependency rule preserved (`evals → core`, never reverse). SQLite for history (already a project dep). Jinja2 for dashboard (already a project dep). No new third-party packages.

**Tech Stack:** Python 3.13, Typer (CLI), SQLite, Jinja2, pytest, ruff. Existing OpenComputer harness conventions.

**Spec:** `docs/superpowers/specs/2026-05-03-eval-system-c-design.md`

---

## Conventions

- File paths in tasks are relative to `OpenComputer/`.
- Test files mirror module structure under `tests/evals/`.
- Each task ends with a commit; commit messages follow `feat(evals): <thing>` / `fix(evals): <thing>`.
- After every task: run `pytest tests/evals/ -x` and `ruff check opencomputer/evals/ tests/evals/`.

## File map (created/modified across all tasks)

**Create:**
- `opencomputer/evals/history.py`
- `opencomputer/evals/dashboard.py`
- `opencomputer/evals/promote.py`
- `opencomputer/evals/templates/dashboard.html.j2`
- `evals/rubrics/reflect_v1.md`
- `evals/cases/reflect.jsonl`
- `tests/evals/test_history.py`
- `tests/evals/test_dashboard.py`
- `tests/evals/test_promote.py`
- `tests/evals/test_error_categorization.py`
- `tests/evals/test_per_site_thresholds.py`
- `tests/evals/test_cost_tracking.py`
- `tests/evals/test_reflect_adapter.py`
- `tests/evals/test_verbose_reporting.py`
- `OpenComputer/docs/refs/evals.md`

**Modify:**
- `opencomputer/evals/types.py`
- `opencomputer/evals/sites.py`
- `opencomputer/evals/adapters.py`
- `opencomputer/evals/runner.py`
- `opencomputer/evals/baseline.py`
- `opencomputer/evals/providers.py`
- `opencomputer/evals/report.py`
- `opencomputer/evals/generation_prompts.py`
- `opencomputer/evals/graders/rubric.py`
- `opencomputer/cli_eval.py`
- `opencomputer/profile_bootstrap/llm_extractor.py`
- `opencomputer/evolution/reflect.py`
- `evals/cases/llm_extractor.jsonl` (regenerate baseline only — no schema change)
- `.github/workflows/test.yml`
- `OpenComputer/.gitignore`
- `OpenComputer/README.md`

---

## Phase 0: Setup

### Task 0.1: Create branch and verify clean state

**Files:** none changed (branch only)

- [ ] **Step 1: Verify current state**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git status
git log --oneline -3
pytest tests/evals/ -x --tb=line
```

Expected: clean working tree, last commit is `9a9afd8b feat(cards): inline dropdowns...`, all eval tests pass.

- [ ] **Step 2: Create branch**

```bash
git checkout -b feat/eval-system-c
```

Expected: `Switched to a new branch 'feat/eval-system-c'`

---

## Phase 1: Error categorization (foundation)

### Task 1.1: Add `ErrorCategory` literal and update `GradeResult`

**Files:**
- Modify: `opencomputer/evals/types.py`
- Test: `tests/evals/test_error_categorization.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_error_categorization.py`:

```python
"""Tests for ErrorCategory and the new GradeResult shape."""
from opencomputer.evals.types import GradeResult


def test_grade_result_defaults_error_category_to_none():
    r = GradeResult(correct=True)
    assert r.error_category is None


def test_grade_result_accepts_infra_error_category():
    r = GradeResult(correct=False, error_category="infra_error", parse_error="Ollama down")
    assert r.error_category == "infra_error"


def test_grade_result_accepts_parse_error_category():
    r = GradeResult(correct=False, error_category="parse_error", parse_error="bad JSON")
    assert r.error_category == "parse_error"


def test_grade_result_accepts_incorrect_category():
    r = GradeResult(correct=False, error_category="incorrect")
    assert r.error_category == "incorrect"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_error_categorization.py -v
```

Expected: FAIL — `TypeError: GradeResult.__init__() got an unexpected keyword argument 'error_category'`

- [ ] **Step 3: Add `ErrorCategory` and update `GradeResult`**

Edit `opencomputer/evals/types.py`. Add at top:

```python
ErrorCategory = Literal["incorrect", "parse_error", "infra_error"]
```

Update `GradeResult`:

```python
@dataclass
class GradeResult:
    correct: bool
    score: float | None = None
    reason: str | None = None
    parse_error: str | None = None
    error_category: ErrorCategory | None = None
    extra: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evals/test_error_categorization.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/types.py tests/evals/test_error_categorization.py
git commit -m "feat(evals): add ErrorCategory literal + GradeResult.error_category"
```

---

### Task 1.2: Add `OllamaUnavailableError` and gate `extract_for_eval`

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py:560-625`
- Test: `tests/evals/test_error_categorization.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_error_categorization.py`:

```python
import pytest
from unittest.mock import patch
from opencomputer.profile_bootstrap.llm_extractor import (
    OllamaUnavailableError,
    extract_for_eval,
)


def test_extract_for_eval_raises_typed_error_when_ollama_missing():
    with patch("opencomputer.profile_bootstrap.llm_extractor.is_ollama_available", return_value=False):
        with pytest.raises(OllamaUnavailableError):
            extract_for_eval("any text")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_error_categorization.py::test_extract_for_eval_raises_typed_error_when_ollama_missing -v
```

Expected: FAIL — `ImportError: cannot import name 'OllamaUnavailableError'`

- [ ] **Step 3: Add the error class and gate the function**

Edit `opencomputer/profile_bootstrap/llm_extractor.py`. Near the top of the file (after imports, before existing `ExtractorUnavailableError` if present, else just below imports):

```python
class OllamaUnavailableError(RuntimeError):
    """Raised by extract_for_eval when the Ollama backend is unreachable.

    Distinct from ExtractorUnavailableError so the eval harness can
    classify it as infra_error (excluded from regression accuracy)
    rather than a real model-output failure.
    """
```

Replace `extract_for_eval` (currently at line 605) with — note the dual guard (binary on PATH AND daemon reachable):

```python
def extract_for_eval(text: str) -> dict:
    """Eval-only entry point.

    Two-stage guard:
      1. is_ollama_available() — binary on PATH
      2. try extract_artifact() — catches daemon-down case (ExtractorUnavailableError)
    Either failure surfaces as OllamaUnavailableError so the runner
    classifies it as infra_error, not parse_error.
    """
    if not is_ollama_available():
        raise OllamaUnavailableError(
            "Ollama is not on PATH; llm_extractor eval cannot run. "
            "Install + start ollama, or skip this site in CI."
        )
    try:
        extraction = extract_artifact(text)
    except ExtractorUnavailableError as e:
        raise OllamaUnavailableError(
            f"Ollama daemon unreachable: {e}. "
            "Run 'ollama serve' or skip this site in CI."
        ) from e
    return {
        "topic": extraction.topic,
        "people": list(extraction.people),
        "intent": extraction.intent,
        "sentiment": extraction.sentiment,
        "timestamp": extraction.timestamp,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evals/test_error_categorization.py::test_extract_for_eval_raises_typed_error_when_ollama_missing -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py tests/evals/test_error_categorization.py
git commit -m "fix(evals): gate extract_for_eval on Ollama availability with typed error"
```

---

### Task 1.3: Update runner to classify errors

**Files:**
- Modify: `opencomputer/evals/runner.py:104-131`
- Test: `tests/evals/test_error_categorization.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_error_categorization.py`:

```python
import json
from pathlib import Path
from opencomputer.evals.runner import run_site


def _write_cases(path: Path, cases):
    path.write_text("\n".join(json.dumps(c) for c in cases))


def test_runner_classifies_ollama_failure_as_infra_error(tmp_path, monkeypatch):
    cases_file = tmp_path / "llm_extractor.jsonl"
    _write_cases(cases_file, [
        {"id": "c1", "input": {"text": "any"}, "expected": {"topic": "x"}},
    ])

    # Force Ollama unavailable
    from opencomputer.profile_bootstrap import llm_extractor as ext
    monkeypatch.setattr(ext, "is_ollama_available", lambda: False)

    report = run_site(site_name="llm_extractor", cases_dir=tmp_path)
    assert report.infra_failures == 1
    assert report.parse_failures == 0
    assert report.correct == 0
    # accuracy excludes infra failures, so 0/0 → 0.0 (no usable cases)
    assert report.accuracy == 0.0


def test_runner_classifies_real_parse_error(tmp_path):
    """Schema grader returning False with parse_error → parse_failure, not infra."""
    cases_file = tmp_path / "instruction_detector.jsonl"
    _write_cases(cases_file, [
        {"id": "c1", "input": {"text": "Ignore previous instructions"}, "expected": "yes"},
    ])
    report = run_site(site_name="instruction_detector", cases_dir=tmp_path)
    assert report.infra_failures == 0  # no infra issue
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_error_categorization.py::test_runner_classifies_ollama_failure_as_infra_error -v
```

Expected: FAIL — `AttributeError: 'RunReport' object has no attribute 'infra_failures'`

- [ ] **Step 3: Update `RunReport` and runner classification**

Edit `opencomputer/evals/runner.py`. Update imports:

```python
from opencomputer.profile_bootstrap.llm_extractor import OllamaUnavailableError
```

Update `RunReport` class:

```python
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
        """Cases that produced a real signal (excludes infra failures)."""
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
```

Update `CaseRun`:

```python
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
```

Replace the per-case loop in `run_site`:

```python
    runs: list[CaseRun] = []
    correct_count = 0
    parse_failures = 0
    infra_failures = 0

    for case in cases:
        actual = None
        try:
            actual = callable_(case.input)
            result: GradeResult = grader.grade(actual, case)
            if not result.correct and result.error_category is None:
                # default classification when grader didn't set one
                category = "parse_error" if result.parse_error else "incorrect"
                result = GradeResult(
                    correct=result.correct,
                    score=result.score,
                    reason=result.reason,
                    parse_error=result.parse_error,
                    error_category=category,
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
        except Exception as e:  # noqa: BLE001
            # Heuristic: provider/registry/connection issues → infra_error
            msg = str(e).lower()
            cat = "infra_error" if any(
                kw in msg for kw in ("not registered", "connection", "unreachable", "timeout")
            ) else "parse_error"
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
            )
        )
        if result.correct:
            correct_count += 1
        elif result.error_category == "infra_error":
            infra_failures += 1
        elif result.error_category == "parse_error":
            parse_failures += 1
        # incorrect counted via property

    return RunReport(
        site_name=site_name,
        total=len(cases),
        correct=correct_count,
        parse_failures=parse_failures,
        infra_failures=infra_failures,
        case_runs=runs,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evals/test_error_categorization.py -v
pytest tests/evals/test_runner.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/runner.py tests/evals/test_error_categorization.py
git commit -m "feat(evals): runner classifies infra vs parse vs incorrect errors"
```

---

### Task 1.4: Update `format_report` for new categories

**Files:**
- Modify: `opencomputer/evals/report.py`
- Test: `tests/evals/test_report.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_report.py`:

```python
from opencomputer.evals.report import format_report
from opencomputer.evals.runner import CaseRun, RunReport


def test_format_report_distinguishes_infra_failures():
    report = RunReport(
        site_name="llm_extractor",
        total=30,
        correct=0,
        parse_failures=0,
        infra_failures=30,
        case_runs=[],
    )
    text = format_report(report)
    assert "Infra failures: 30" in text
    assert "30/30 correct" not in text  # accuracy hidden when no usable cases


def test_format_report_shows_normal_when_no_infra_issues():
    report = RunReport(
        site_name="job_change",
        total=30,
        correct=30,
        parse_failures=0,
        infra_failures=0,
        case_runs=[],
    )
    text = format_report(report)
    assert "30/30 correct" in text
    assert "Infra failures: 0" not in text  # don't clutter when zero
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_report.py -v
```

Expected: FAIL on the new tests.

- [ ] **Step 3: Update `format_report`**

Replace the body of `opencomputer/evals/report.py:format_report`:

```python
def format_report(report: RunReport, *, baseline_diff: BaselineDiff | None = None, verbose: bool = False) -> str:
    lines = [f"Site: {report.site_name}"]

    if report.usable_total > 0:
        lines.append(
            f"  Cases: {report.correct}/{report.usable_total} correct "
            f"({report.accuracy:.1%})"
        )
    else:
        lines.append(f"  Cases: 0/0 (no usable cases — all infra failures)")

    if report.parse_failures:
        lines.append(f"  Parse failures: {report.parse_failures} ({report.parse_failure_rate:.1%})")
    if report.infra_failures:
        lines.append(f"  Infra failures: {report.infra_failures} ({report.infra_failure_rate:.1%})")

    if report.cost_usd is not None and report.cost_usd > 0:
        lines.append(
            f"  Cost: ${report.cost_usd:.4f} "
            f"({report.input_tokens} in / {report.output_tokens} out)"
        )

    if baseline_diff is not None:
        sign = "+" if baseline_diff.accuracy_delta >= 0 else ""
        lines.append(
            f"  vs baseline ({baseline_diff.baseline.timestamp[:10]}, "
            f"{baseline_diff.baseline.model}): "
            f"{sign}{baseline_diff.accuracy_delta:.2%} accuracy, "
            f"{baseline_diff.parse_failure_rate_delta:+.2%} parse-failure rate"
        )

    if verbose:
        failing = [c for c in report.case_runs if not c.correct]
        if failing:
            lines.append(f"  Failing cases ({len(failing)}):")
            for c in failing:
                lines.append(f"    {c.case_id}  [{c.error_category or 'incorrect'}]")
                if c.input is not None:
                    lines.append(f"      input: {_truncate(c.input)}")
                if c.expected is not None:
                    lines.append(f"      expected: {_truncate(c.expected)}")
                if c.actual is not None:
                    lines.append(f"      actual:   {_truncate(c.actual)}")
                if c.parse_error:
                    lines.append(f"      error: {c.parse_error}")

    return "\n".join(lines)


def _truncate(value, limit: int = 120) -> str:
    s = repr(value) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[: limit - 3] + "..."
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/evals/test_report.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/report.py tests/evals/test_report.py
git commit -m "feat(evals): format_report shows infra failures, cost, verbose drilldown"
```

---

### Task 1.5: Coverage tests — cost display, verbose truncation, JSON envelope

**Files:**
- Test: `tests/evals/test_report.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/evals/test_report.py`:

```python
def test_format_report_shows_cost_when_present():
    report = RunReport(
        site_name="reflect",
        total=10,
        correct=10,
        parse_failures=0,
        infra_failures=0,
        cost_usd=0.0123,
        input_tokens=2000,
        output_tokens=400,
        case_runs=[],
    )
    text = format_report(report)
    assert "$0.0123" in text
    assert "2000 in" in text
    assert "400 out" in text


def test_format_report_hides_cost_when_zero():
    report = RunReport(
        site_name="job_change",
        total=30,
        correct=30,
        parse_failures=0,
        infra_failures=0,
        cost_usd=None,
        case_runs=[],
    )
    text = format_report(report)
    assert "Cost:" not in text


def test_format_report_verbose_truncates_long_input():
    long_text = "x" * 500
    runs = [
        CaseRun(
            case_id="c1",
            correct=False,
            parse_error=None,
            error_category="incorrect",
            input={"text": long_text},
            expected="yes",
            actual="no",
        )
    ]
    report = RunReport(
        site_name="instruction_detector",
        total=1,
        correct=0,
        parse_failures=0,
        infra_failures=0,
        case_runs=runs,
    )
    text = format_report(report, verbose=True)
    assert "..." in text  # truncation marker present
    # Truncated value never appears at full length in output
    assert long_text not in text


def test_format_report_json_emits_parseable_payload():
    from opencomputer.evals.report import format_report_json
    report = RunReport(
        site_name="x",
        total=2,
        correct=1,
        parse_failures=0,
        infra_failures=0,
        case_runs=[
            CaseRun(case_id="c1", correct=True, parse_error=None, error_category=None),
            CaseRun(case_id="c2", correct=False, parse_error=None, error_category="incorrect"),
        ],
    )
    text = format_report_json(report)
    import json
    payload = json.loads(text)
    assert payload["site_name"] == "x"
    assert payload["total"] == 2
    assert len(payload["case_runs"]) == 2
    assert payload["case_runs"][1]["error_category"] == "incorrect"
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/evals/test_report.py -v
```

Expected: all pass (these exercise behavior already implemented in Task 1.4 + Task 2.2).

- [ ] **Step 3: Commit**

```bash
git add tests/evals/test_report.py
git commit -m "test(evals): cost display, verbose truncation, JSON parseable"
```

---

## Phase 2: Verbose, JSON, case-id filter

### Task 2.1: Add `--verbose` to `oc eval run`

**Files:**
- Modify: `opencomputer/cli_eval.py:17-65`
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_cli_eval.py`:

```python
from typer.testing import CliRunner
from opencomputer.cli_eval import eval_app


def test_run_command_verbose_flag(tmp_path, monkeypatch):
    runner = CliRunner()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "instruction_detector.jsonl").write_text(
        '{"id": "c1", "input": {"text": "what is the weather?"}, "expected": "yes"}\n'
    )
    result = runner.invoke(
        eval_app,
        ["run", "instruction_detector", "--cases-dir", str(cases_dir), "--verbose"],
    )
    assert result.exit_code == 0
    assert "Failing cases" in result.output
    assert "c1" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_cli_eval.py::test_run_command_verbose_flag -v
```

Expected: FAIL — flag doesn't exist or output doesn't contain "Failing cases".

- [ ] **Step 3: Update `run_command` signature**

Edit `opencomputer/cli_eval.py`. Update `run_command` signature to add `verbose`:

```python
@eval_app.command("run")
def run_command(
    site: str = typer.Argument(..., help="Site name. 'all' to run every registered site."),
    save_baseline_flag: bool = typer.Option(False, "--save-baseline"),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
    baselines_dir: Path = typer.Option(Path("evals/baselines"), "--baselines-dir"),
    grader_model: str | None = typer.Option(None, "--grader-model"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show failing case details."),
):
    ...
```

In the inner per-site loop, replace:

```python
typer.echo(format_report(report, baseline_diff=diff))
```

with:

```python
typer.echo(format_report(report, baseline_diff=diff, verbose=verbose))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/evals/test_cli_eval.py::test_run_command_verbose_flag -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_eval.py tests/evals/test_cli_eval.py
git commit -m "feat(evals): oc eval run --verbose shows failing case details"
```

---

### Task 2.2: Add `--json` flag

**Files:**
- Modify: `opencomputer/cli_eval.py`
- Modify: `opencomputer/evals/report.py` (add `format_report_json`)
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
import json as _json


def test_run_command_json_flag(tmp_path):
    runner = CliRunner()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "instruction_detector.jsonl").write_text(
        '{"id": "c1", "input": {"text": "what time is it?"}, "expected": "no"}\n'
    )
    result = runner.invoke(
        eval_app,
        ["run", "instruction_detector", "--cases-dir", str(cases_dir), "--json"],
    )
    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload["site_name"] == "instruction_detector"
    assert payload["total"] == 1
    assert "case_runs" in payload
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_cli_eval.py::test_run_command_json_flag -v
```

Expected: FAIL.

- [ ] **Step 3: Add `format_report_json`**

Add to `opencomputer/evals/report.py`:

```python
import json
from dataclasses import asdict


def format_report_json(report: RunReport, *, baseline_diff: BaselineDiff | None = None) -> str:
    payload = {
        "site_name": report.site_name,
        "total": report.total,
        "correct": report.correct,
        "incorrect": report.incorrect,
        "parse_failures": report.parse_failures,
        "infra_failures": report.infra_failures,
        "accuracy": report.accuracy,
        "cost_usd": report.cost_usd,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "case_runs": [
            {
                "case_id": c.case_id,
                "correct": c.correct,
                "error_category": c.error_category,
                "input": c.input,
                "expected": c.expected,
                "actual": c.actual,
                "parse_error": c.parse_error,
            }
            for c in report.case_runs
        ],
    }
    if baseline_diff is not None:
        payload["baseline_diff"] = {
            "accuracy_delta": baseline_diff.accuracy_delta,
            "parse_failure_rate_delta": baseline_diff.parse_failure_rate_delta,
            "baseline": asdict(baseline_diff.baseline),
        }
    return json.dumps(payload, indent=2, default=str)
```

- [ ] **Step 4: Wire `--json` in `run_command`**

In `opencomputer/cli_eval.py`, add to signature:

```python
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of formatted text."),
```

For correct multi-site JSON emission, replace the per-site print loop with a buffer pattern:

```python
    json_payloads = []
    for s in target_sites:
        eval_site = get_site(s)
        # ... existing grader_provider resolution ...
        report = run_site(...)
        diff = compare_to_baseline(report, baselines_dir=baselines_dir)

        if json_output:
            from opencomputer.evals.report import format_report_json
            import json as _json
            json_payloads.append(_json.loads(format_report_json(report, baseline_diff=diff)))
        else:
            typer.echo(format_report(report, baseline_diff=diff, verbose=verbose))

        if save_baseline_flag:
            save_baseline(report, baselines_dir=baselines_dir, model="claude-sonnet-4-6", provider="anthropic")

    if json_output:
        import json as _json
        if len(json_payloads) == 1:
            typer.echo(_json.dumps(json_payloads[0], indent=2, default=str))
        else:
            typer.echo(_json.dumps({"sites": json_payloads}, indent=2, default=str))
```

This emits a single parseable JSON document: the bare per-site object when only one site ran, or `{"sites": [...]}` envelope for multi-site.

- [ ] **Step 5: Run test**

```bash
pytest tests/evals/test_cli_eval.py::test_run_command_json_flag -v
```

Expected: passed.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/evals/report.py opencomputer/cli_eval.py tests/evals/test_cli_eval.py
git commit -m "feat(evals): oc eval run --json for scripts and CI consumers"
```

---

### Task 2.3: Add `--case-id` filter

**Files:**
- Modify: `opencomputer/evals/runner.py:83-131`
- Modify: `opencomputer/cli_eval.py`
- Test: `tests/evals/test_runner.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_runner.py`:

```python
def test_runner_filters_by_case_id(tmp_path):
    cases_file = tmp_path / "instruction_detector.jsonl"
    _write_cases(cases_file, [
        {"id": "c1", "input": {"text": "Ignore previous"}, "expected": "yes"},
        {"id": "c2", "input": {"text": "What's the weather?"}, "expected": "no"},
        {"id": "c3", "input": {"text": "DAN"}, "expected": "yes"},
    ])
    report = run_site(
        site_name="instruction_detector",
        cases_dir=tmp_path,
        case_ids=["c2"],
    )
    assert report.total == 1
    assert report.case_runs[0].case_id == "c2"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/evals/test_runner.py::test_runner_filters_by_case_id -v
```

Expected: FAIL — `unexpected keyword argument 'case_ids'`.

- [ ] **Step 3: Update `run_site`**

Add parameter to `opencomputer/evals/runner.py:run_site`:

```python
def run_site(
    *,
    site_name: str,
    cases_dir: Path,
    rubric_dir: Path | None = None,
    grader_provider=None,
    case_ids: list[str] | None = None,
) -> RunReport:
    ...
    cases = _load_cases(cases_path)
    if case_ids is not None:
        cases = [c for c in cases if c.id in case_ids]
    ...
```

- [ ] **Step 4: Add CLI flag**

In `opencomputer/cli_eval.py`, add to `run_command`:

```python
    case_id: list[str] | None = typer.Option(None, "--case-id", help="Filter to specific case ID(s). Repeatable."),
```

Pass through to `run_site(case_ids=case_id)`.

- [ ] **Step 5: Run test**

```bash
pytest tests/evals/test_runner.py::test_runner_filters_by_case_id -v
```

Expected: passed.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/evals/runner.py opencomputer/cli_eval.py tests/evals/test_runner.py
git commit -m "feat(evals): --case-id filter for fast iteration on single cases"
```

---

## Phase 3: Per-site thresholds

### Task 3.1: Add `regression_threshold` to `EvalSite`

**Files:**
- Modify: `opencomputer/evals/types.py`
- Modify: `opencomputer/evals/sites.py`
- Modify: `opencomputer/evals/cli_eval.py:113` (regress threshold)
- Test: `tests/evals/test_per_site_thresholds.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_per_site_thresholds.py`:

```python
from opencomputer.evals.sites import get_site


def test_eval_site_has_default_threshold():
    site = get_site("job_change")
    assert site.regression_threshold == 0.05


def test_eval_site_threshold_is_configurable():
    from opencomputer.evals.types import EvalSite
    site = EvalSite(
        name="custom",
        callable_path="x:y",
        grader="exact",
        regression_threshold=0.10,
    )
    assert site.regression_threshold == 0.10
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_per_site_thresholds.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add field to `EvalSite`**

In `opencomputer/evals/types.py`:

```python
@dataclass(frozen=True)
class EvalSite:
    name: str
    callable_path: str
    grader: GraderKind
    schema: dict | None = None
    rubric_id: str | None = None
    requires_provider: bool = True
    regression_threshold: float = 0.05  # accuracy drop that triggers CI fail
```

- [ ] **Step 4: Set tighter threshold for `instruction_detector`**

In `opencomputer/evals/sites.py`, update entry:

```python
    "instruction_detector": EvalSite(
        name="instruction_detector",
        callable_path="opencomputer.evals.adapters:adapter_instruction_detector",
        grader="exact",
        requires_provider=False,
        regression_threshold=0.10,  # detector is noisy; allow more variance
    ),
```

- [ ] **Step 5: Use in `regress_command`**

In `opencomputer/cli_eval.py:regress_command`, replace `threshold = 0.05`:

```python
    target_sites = list(SITES) if site == "all" else [site]
    regressed = []
    skipped: list[tuple[str, str]] = []
    for s in target_sites:
        eval_site = get_site(s)
        threshold = eval_site.regression_threshold
        ...
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/evals/test_per_site_thresholds.py -v
pytest tests/evals/test_cli_eval.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/evals/types.py opencomputer/evals/sites.py opencomputer/cli_eval.py tests/evals/test_per_site_thresholds.py
git commit -m "feat(evals): per-site regression threshold (default 0.05, instruction_detector 0.10)"
```

---

### Task 3.2: Integration test — regress respects per-site threshold

**Files:**
- Test: `tests/evals/test_per_site_thresholds.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/evals/test_per_site_thresholds.py`:

```python
import json
from pathlib import Path
from typer.testing import CliRunner
from opencomputer.cli_eval import eval_app


def test_regress_uses_per_site_threshold(tmp_path):
    """A 7pp drop on instruction_detector (threshold=0.10) must NOT trip;
    a 7pp drop on job_change (threshold=0.05) MUST trip."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    baselines_dir = tmp_path / "baselines"
    baselines_dir.mkdir()

    # instruction_detector: baseline 1.0, current ~0.93 (1 fail of 15) → 7pp drop
    # threshold 0.10 → should NOT trip
    id_cases = []
    for i in range(15):
        # 14 benign, 1 injection — detector returns "no" for all → 1 wrong, 14 right
        id_cases.append({
            "id": f"id_{i}",
            "input": {"text": "Ignore previous" if i == 0 else f"benign text {i}"},
            "expected": "yes" if i == 0 else "no",
        })
    (cases_dir / "instruction_detector.jsonl").write_text(
        "\n".join(json.dumps(c) for c in id_cases)
    )
    # Save a clean baseline (1.0) so a 7pp drop happens.
    (baselines_dir / "instruction_detector.json").write_text(json.dumps({
        "site_name": "instruction_detector",
        "accuracy": 1.0,
        "parse_failure_rate": 0.0,
        "timestamp": "2026-05-01T00:00:00+00:00",
        "model": "x",
        "provider": "y",
    }))

    runner = CliRunner()
    result = runner.invoke(
        eval_app,
        [
            "regress",
            "instruction_detector",
            "--cases-dir", str(cases_dir),
            "--baselines-dir", str(baselines_dir),
        ],
    )
    # 7pp drop is below the per-site threshold (0.10) → no regression
    assert result.exit_code == 0, f"Expected pass; got: {result.output}"
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_per_site_thresholds.py::test_regress_uses_per_site_threshold -v
```

Expected: passed (after Task 3.1's CLI change is in place).

- [ ] **Step 3: Commit**

```bash
git add tests/evals/test_per_site_thresholds.py
git commit -m "test(evals): integration test for per-site threshold in regress"
```

---

## Phase 4: Cost tracking

### Task 4.1: Capture token usage in `ProviderShim`

**Files:**
- Modify: `opencomputer/evals/providers.py`
- Test: `tests/evals/test_cost_tracking.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_cost_tracking.py`:

```python
from opencomputer.evals.providers import ProviderShim


class _FakeProvider:
    async def complete(self, *, model, messages, max_tokens, temperature, site):
        from plugin_sdk.core import Message
        from plugin_sdk.provider_contract import ProviderResponse, Usage
        return ProviderResponse(
            message=Message(role="assistant", content="<thinking>ok</thinking><result>correct</result>"),
            usage=Usage(input_tokens=100, output_tokens=20),
        )


def test_provider_shim_returns_usage():
    shim = ProviderShim(_FakeProvider(), model="claude-sonnet-4-6")
    response = shim.complete("test prompt")
    assert response.text.startswith("<thinking>")
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_cost_tracking.py -v
```

Expected: FAIL — `usage` attribute missing on shim response.

- [ ] **Step 3: Update `ProviderShim`**

In `opencomputer/evals/providers.py`, replace the inner `complete` body:

```python
    def complete(self, prompt: str) -> Any:
        from plugin_sdk.core import Message

        response = asyncio.run(
            self._provider.complete(
                model=self._model,
                messages=[Message(role="user", content=prompt)],
                max_tokens=2048,
                temperature=0.3,
                site="eval_grader",
            )
        )
        text = response.message.content if hasattr(response, "message") else str(response)
        usage = getattr(response, "usage", None)
        return type("ShimResponse", (), {"text": text, "usage": usage})()
```

- [ ] **Step 4: Run test**

```bash
pytest tests/evals/test_cost_tracking.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/providers.py tests/evals/test_cost_tracking.py
git commit -m "feat(evals): ProviderShim exposes usage for cost tracking"
```

---

### Task 4.2: Aggregate cost in rubric grader and runner

**Files:**
- Modify: `opencomputer/evals/graders/rubric.py`
- Modify: `opencomputer/evals/runner.py`
- Test: `tests/evals/test_cost_tracking.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_cost_tracking.py`:

```python
import json
from pathlib import Path
from opencomputer.evals.runner import run_site


class _CountingProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return type(
            "R", (),
            {
                "text": "<result>correct</result>",
                "usage": type("U", (), {"input_tokens": 50, "output_tokens": 10})(),
            },
        )()


def test_runner_aggregates_grader_cost(tmp_path, monkeypatch):
    rubric_dir = tmp_path / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "test_rubric.md").write_text("Was the response correct? Yes if it makes sense.")

    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "_test_rubric_site.jsonl").write_text(
        '{"id": "c1", "input": {"x": 1}, "rubric_id": "test_rubric"}\n'
        '{"id": "c2", "input": {"x": 2}, "rubric_id": "test_rubric"}\n'
    )

    # Register a synthetic site for this test
    from opencomputer.evals.sites import SITES
    from opencomputer.evals.types import EvalSite
    SITES["_test_rubric_site"] = EvalSite(
        name="_test_rubric_site",
        callable_path="opencomputer.evals.adapters:adapter_instruction_detector",  # any callable
        grader="rubric",
        rubric_id="test_rubric",
    )
    try:
        provider = _CountingProvider()
        report = run_site(
            site_name="_test_rubric_site",
            cases_dir=cases_dir,
            rubric_dir=rubric_dir,
            grader_provider=provider,
        )
        assert provider.calls == 2
        assert report.input_tokens == 100  # 2 * 50
        assert report.output_tokens == 20
    finally:
        del SITES["_test_rubric_site"]
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_cost_tracking.py::test_runner_aggregates_grader_cost -v
```

Expected: FAIL.

- [ ] **Step 3: Update `LLMRubricGrader.grade` to attach usage**

In `opencomputer/evals/graders/rubric.py`, update `grade` to capture usage in `extra`:

```python
    def grade(self, actual: object, case: Case) -> GradeResult:
        if case.rubric_id is None:
            raise ValueError(f"LLMRubricGrader requires case.rubric_id on {case.id!r}")

        rubric_path = self.rubric_dir / f"{case.rubric_id}.md"
        rubric_text = rubric_path.read_text(encoding="utf-8")

        prompt = self.PROMPT_TEMPLATE.format(rubric=rubric_text, response=str(actual))
        response = self.provider.complete(prompt)
        text = getattr(response, "text", str(response))
        usage = getattr(response, "usage", None)
        extra = {}
        if usage is not None:
            extra["input_tokens"] = getattr(usage, "input_tokens", 0)
            extra["output_tokens"] = getattr(usage, "output_tokens", 0)

        result_match = _RESULT_RE.search(text)
        thinking_match = _THINKING_RE.search(text)
        reasoning = thinking_match.group(1).strip() if thinking_match else None

        if not result_match:
            return GradeResult(
                correct=False,
                reason=reasoning,
                parse_error="no <result>...</result> tag in grader response",
                extra=extra,
            )

        verdict = result_match.group(1).strip().lower()
        return GradeResult(correct=(verdict == "correct"), reason=reasoning, extra=extra)
```

- [ ] **Step 4: Aggregate in `runner.run_site`**

In `opencomputer/evals/runner.py`, add aggregation after the loop and before returning:

```python
    total_in = sum(r.extra.get("input_tokens", 0) for r in [...])  # see below
```

Cleaner: track during the loop. Update the loop body — after `runs.append(...)`:

```python
        if result.extra:
            input_tokens_total += result.extra.get("input_tokens", 0)
            output_tokens_total += result.extra.get("output_tokens", 0)
```

Initialize at top of `run_site`:

```python
    input_tokens_total = 0
    output_tokens_total = 0
```

And include in `RunReport(...)`:

```python
    return RunReport(
        ...
        input_tokens=input_tokens_total,
        output_tokens=output_tokens_total,
        cost_usd=_estimate_cost(input_tokens_total, output_tokens_total, grader_provider),
    )
```

Add helper at module bottom:

```python
def _estimate_cost(in_tokens: int, out_tokens: int, provider) -> float | None:
    """Tiny price table (USD per 1M tokens) for known grader models."""
    if provider is None or in_tokens + out_tokens == 0:
        return None
    # claude-sonnet-4-6: $3 in, $15 out per 1M
    # claude-opus-4-7: $15 in, $75 out per 1M
    model = getattr(provider, "_model", "")
    if "opus" in model.lower():
        return (in_tokens * 15 + out_tokens * 75) / 1_000_000
    if "sonnet" in model.lower():
        return (in_tokens * 3 + out_tokens * 15) / 1_000_000
    return None  # unknown model
```

- [ ] **Step 5: Update `CaseRun` to forward extra**

We already added `extra: dict[str, Any]` to CaseRun above. The runner loop should pass `result.extra` through.

- [ ] **Step 6: Run tests**

```bash
pytest tests/evals/test_cost_tracking.py -v
```

Expected: passed.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/evals/runner.py opencomputer/evals/graders/rubric.py tests/evals/test_cost_tracking.py
git commit -m "feat(evals): aggregate grader cost (tokens + USD) in RunReport"
```

---

### Task 4.3: Persist cost in baseline

**Files:**
- Modify: `opencomputer/evals/baseline.py`
- Test: `tests/evals/test_baseline.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/evals/test_baseline.py`:

```python
def test_baseline_snapshot_persists_cost(tmp_path):
    from opencomputer.evals.baseline import save_baseline, _load_baseline
    from opencomputer.evals.runner import RunReport

    report = RunReport(
        site_name="reflect",
        total=10,
        correct=8,
        parse_failures=0,
        infra_failures=0,
        cost_usd=0.0123,
        input_tokens=2000,
        output_tokens=400,
    )
    save_baseline(report, baselines_dir=tmp_path, model="claude-sonnet-4-6", provider="anthropic")
    snap = _load_baseline(tmp_path, "reflect")
    assert snap.cost_usd == 0.0123
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_baseline.py -v
```

Expected: FAIL.

- [ ] **Step 3: Update `BaselineSnapshot` and save_baseline**

In `opencomputer/evals/baseline.py`:

```python
@dataclass
class BaselineSnapshot:
    site_name: str
    accuracy: float
    parse_failure_rate: float
    timestamp: str
    model: str
    provider: str
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
```

Update `save_baseline`:

```python
    snapshot = BaselineSnapshot(
        site_name=report.site_name,
        accuracy=report.accuracy,
        parse_failure_rate=report.parse_failure_rate,
        timestamp=datetime.now(UTC).isoformat(),
        model=model,
        provider=provider,
        cost_usd=report.cost_usd,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
    )
```

- [ ] **Step 4: Run test**

```bash
pytest tests/evals/test_baseline.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/baseline.py tests/evals/test_baseline.py
git commit -m "feat(evals): persist cost + tokens in baseline snapshot"
```

---

### Task 4.4: Backward-compat — old baseline JSON loads with new schema

**Files:**
- Test: `tests/evals/test_baseline.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/evals/test_baseline.py`:

```python
import json


def test_old_baseline_json_loads_with_new_fields_defaulted(tmp_path):
    """A baseline written before cost_usd/input_tokens/output_tokens existed
    must still load — new fields default to None/0."""
    from opencomputer.evals.baseline import _load_baseline, BaselineSnapshot

    old_payload = {
        "site_name": "job_change",
        "accuracy": 1.0,
        "parse_failure_rate": 0.0,
        "timestamp": "2026-05-02T14:42:00.237745+00:00",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
    }
    (tmp_path / "job_change.json").write_text(json.dumps(old_payload))
    snap = _load_baseline(tmp_path, "job_change")
    assert isinstance(snap, BaselineSnapshot)
    assert snap.accuracy == 1.0
    assert snap.cost_usd is None
    assert snap.input_tokens == 0
    assert snap.output_tokens == 0
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_baseline.py::test_old_baseline_json_loads_with_new_fields_defaulted -v
```

Expected: passes if Task 4.3's BaselineSnapshot defaults are correctly defined.

If it fails (e.g., `_load_baseline` uses `BaselineSnapshot(**dict)` and dict doesn't have new keys, but defaults handle it) — confirm `@dataclass` field defaults populate. Should pass.

- [ ] **Step 3: Commit**

```bash
git add tests/evals/test_baseline.py
git commit -m "test(evals): backward-compat for old baseline JSON files"
```

---

## Phase 5: Run history (SQLite)

### Task 5.1: Create `history.py` with schema + record_run

**Files:**
- Create: `opencomputer/evals/history.py`
- Test: `tests/evals/test_history.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_history.py`:

```python
import json
from pathlib import Path
from opencomputer.evals.history import record_run, load_recent_runs, prune_to_limit
from opencomputer.evals.runner import CaseRun, RunReport


def _make_report(site="x", correct=10) -> RunReport:
    return RunReport(
        site_name=site,
        total=10,
        correct=correct,
        parse_failures=0,
        infra_failures=0,
        case_runs=[CaseRun(case_id=f"c{i}", correct=i < correct, parse_error=None) for i in range(10)],
    )


def test_record_and_load(tmp_path):
    db_path = tmp_path / "history.db"
    record_run(_make_report(), db_path=db_path, model="m", provider="p")
    rows = load_recent_runs("x", db_path=db_path, limit=10)
    assert len(rows) == 1
    assert rows[0]["accuracy"] == 1.0
    assert rows[0]["site_name"] == "x"


def test_prune_keeps_only_limit(tmp_path):
    db_path = tmp_path / "history.db"
    for i in range(105):
        record_run(_make_report(correct=i % 10), db_path=db_path, model="m", provider="p")
    prune_to_limit("x", limit=100, db_path=db_path)
    rows = load_recent_runs("x", db_path=db_path, limit=200)
    assert len(rows) == 100


def test_load_recent_orders_by_timestamp_desc(tmp_path):
    db_path = tmp_path / "history.db"
    for i in range(3):
        record_run(_make_report(correct=i), db_path=db_path, model="m", provider="p")
    rows = load_recent_runs("x", db_path=db_path, limit=10)
    # most recent first
    assert rows[0]["correct"] == 2
    assert rows[2]["correct"] == 0
```

- [ ] **Step 2: Run test**

```bash
pytest tests/evals/test_history.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `history.py`**

```python
"""SQLite run history for the eval harness.

Append-only log of every eval run. Retention enforced at write time (default 100/site).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from opencomputer.evals.runner import CaseRun, RunReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    accuracy REAL NOT NULL,
    correct INTEGER NOT NULL,
    incorrect INTEGER NOT NULL,
    parse_failures INTEGER NOT NULL,
    infra_failures INTEGER NOT NULL,
    total INTEGER NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    grader_model TEXT,
    cost_usd REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    case_runs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_site_ts ON eval_runs(site_name, timestamp DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def record_run(
    report: RunReport,
    *,
    db_path: Path,
    model: str,
    provider: str,
    grader_model: str | None = None,
    retention_limit: int = 100,
) -> int:
    """Insert one run; prune to retention_limit. Returns row id."""
    case_runs_payload = json.dumps(
        [
            {
                "case_id": c.case_id,
                "correct": c.correct,
                "error_category": c.error_category,
                "input": c.input,
                "expected": c.expected,
                "actual": c.actual,
                "parse_error": c.parse_error,
            }
            for c in report.case_runs
        ],
        default=str,
    )
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO eval_runs
            (site_name, timestamp, accuracy, correct, incorrect, parse_failures,
             infra_failures, total, model, provider, grader_model, cost_usd,
             input_tokens, output_tokens, case_runs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.site_name,
                datetime.now(UTC).isoformat(),
                report.accuracy,
                report.correct,
                report.incorrect,
                report.parse_failures,
                report.infra_failures,
                report.total,
                model,
                provider,
                grader_model,
                report.cost_usd,
                report.input_tokens,
                report.output_tokens,
                case_runs_payload,
            ),
        )
        new_id = cur.lastrowid
    prune_to_limit(report.site_name, limit=retention_limit, db_path=db_path)
    return new_id


def load_recent_runs(site_name: str, *, db_path: Path, limit: int = 50) -> list[dict]:
    """Return list of run dicts (newest first)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM eval_runs WHERE site_name = ? ORDER BY timestamp DESC LIMIT ?",
            (site_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_to_limit(site_name: str, *, limit: int, db_path: Path) -> int:
    """Keep newest `limit` rows for site_name; delete the rest. Returns deleted count."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            DELETE FROM eval_runs
            WHERE site_name = ?
              AND id NOT IN (
                SELECT id FROM eval_runs
                WHERE site_name = ?
                ORDER BY timestamp DESC
                LIMIT ?
              )
            """,
            (site_name, site_name, limit),
        )
        return cur.rowcount


def list_sites_with_history(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT site_name FROM eval_runs ORDER BY site_name"
        ).fetchall()
    return [r["site_name"] for r in rows]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/evals/test_history.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/history.py tests/evals/test_history.py
git commit -m "feat(evals): SQLite run history with retention policy"
```

---

### Task 5.2: Wire history into `run_command`

**Files:**
- Modify: `opencomputer/cli_eval.py`
- Modify: `OpenComputer/.gitignore`
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_run_command_writes_history(tmp_path, monkeypatch):
    runner = CliRunner()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "instruction_detector.jsonl").write_text(
        '{"id": "c1", "input": {"text": "hi"}, "expected": "no"}\n'
    )
    history_db = tmp_path / "history.db"
    monkeypatch.setenv("OPENCOMPUTER_EVAL_HISTORY_DB", str(history_db))

    result = runner.invoke(
        eval_app,
        ["run", "instruction_detector", "--cases-dir", str(cases_dir)],
    )
    assert result.exit_code == 0
    assert history_db.exists()


def test_run_command_no_history_skips_db(tmp_path, monkeypatch):
    runner = CliRunner()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "instruction_detector.jsonl").write_text(
        '{"id": "c1", "input": {"text": "hi"}, "expected": "no"}\n'
    )
    history_db = tmp_path / "history.db"
    monkeypatch.setenv("OPENCOMPUTER_EVAL_HISTORY_DB", str(history_db))

    result = runner.invoke(
        eval_app,
        ["run", "instruction_detector", "--cases-dir", str(cases_dir), "--no-history"],
    )
    assert result.exit_code == 0
    assert not history_db.exists()
```

- [ ] **Step 2: Run test**

Expected: FAIL.

- [ ] **Step 3: Wire history in `run_command`**

Add to signature:

```python
    no_history: bool = typer.Option(False, "--no-history", help="Skip writing run to SQLite history."),
    history_db: Path = typer.Option(
        Path(os.environ.get("OPENCOMPUTER_EVAL_HISTORY_DB", "evals/history.db")),
        "--history-db",
    ),
```

Add `import os` at top.

After `report = run_site(...)`, before output:

```python
        if not no_history:
            from opencomputer.evals.history import record_run
            record_run(
                report,
                db_path=history_db,
                model="claude-sonnet-4-6",  # TODO: read from config
                provider="anthropic",
                grader_model=grader_model,
            )
```

- [ ] **Step 4: Update `.gitignore`**

Add to `OpenComputer/.gitignore`:

```
evals/history.db
evals/history.db-journal
evals/dashboard/
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/evals/test_cli_eval.py -v
```

Expected: passed.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_eval.py OpenComputer/.gitignore tests/evals/test_cli_eval.py
git commit -m "feat(evals): write every run to SQLite history (--no-history to skip)"
```

---

### Task 5.3: Add `oc eval history` subcommand

**Files:**
- Modify: `opencomputer/cli_eval.py`
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_history_command_prints_recent_runs(tmp_path, monkeypatch):
    from opencomputer.evals.history import record_run
    from opencomputer.evals.runner import RunReport

    db_path = tmp_path / "history.db"
    record_run(
        RunReport(site_name="job_change", total=30, correct=30, parse_failures=0, infra_failures=0),
        db_path=db_path, model="m", provider="p",
    )
    monkeypatch.setenv("OPENCOMPUTER_EVAL_HISTORY_DB", str(db_path))

    runner = CliRunner()
    result = runner.invoke(eval_app, ["history", "job_change"])
    assert result.exit_code == 0
    assert "job_change" in result.output
    assert "100.0%" in result.output
```

- [ ] **Step 2: Run test**

Expected: FAIL — `history` command not registered.

- [ ] **Step 3: Add `history_command`**

Append to `opencomputer/cli_eval.py`:

```python
@eval_app.command("history")
def history_command(
    site: str = typer.Argument("all"),
    limit: int = typer.Option(20, "--limit"),
    history_db: Path = typer.Option(
        Path(os.environ.get("OPENCOMPUTER_EVAL_HISTORY_DB", "evals/history.db")),
        "--history-db",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Show recent runs from history."""
    from opencomputer.evals.history import list_sites_with_history, load_recent_runs

    sites = list_sites_with_history(history_db) if site == "all" else [site]

    output_rows: list[dict] = []
    for s in sites:
        rows = load_recent_runs(s, db_path=history_db, limit=limit)
        output_rows.extend(rows)

    if json_output:
        import json
        typer.echo(json.dumps(output_rows, indent=2, default=str))
        return

    for r in output_rows:
        typer.echo(
            f"{r['timestamp'][:19]}  {r['site_name']:<25}  "
            f"{r['correct']}/{r['total']} ({r['accuracy']:.1%})  "
            f"infra={r['infra_failures']}  parse={r['parse_failures']}"
        )
```

- [ ] **Step 4: Run test**

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_eval.py tests/evals/test_cli_eval.py
git commit -m "feat(evals): oc eval history shows recent runs from SQLite"
```

---

## Phase 6: Dashboard

### Task 6.1: Dashboard template

**Files:**
- Create: `opencomputer/evals/templates/dashboard.html.j2`

- [ ] **Step 1: Create directory and template**

```bash
mkdir -p opencomputer/evals/templates
```

Write `opencomputer/evals/templates/dashboard.html.j2`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="generator" content="opencomputer eval dashboard">
<meta name="generated-at" content="{{ generated_at }}">
<title>OpenComputer eval dashboard</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1f2328; }
h1 { font-size: 1.6rem; margin-bottom: 0.2rem; }
.subtitle { color: #57606a; font-size: 0.9rem; margin-bottom: 2rem; }
.site { border: 1px solid #d0d7de; border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }
.site h2 { font-size: 1.15rem; margin: 0 0 0.5rem; display: flex; align-items: center; gap: 0.75rem; }
.metric { font-family: 'SF Mono', Menlo, monospace; font-size: 0.85rem; }
.green { color: #1a7f37; }
.amber { color: #9a6700; }
.red { color: #cf222e; }
.spark { display: inline-block; vertical-align: middle; }
.summary { display: flex; gap: 2rem; flex-wrap: wrap; margin: 0.75rem 0; font-size: 0.9rem; }
details { margin-top: 0.75rem; }
summary { cursor: pointer; color: #0969da; font-size: 0.85rem; }
.case { font-family: 'SF Mono', Menlo, monospace; font-size: 0.78rem; padding: 0.5rem; background: #f6f8fa; border-radius: 4px; margin: 0.25rem 0; }
.case-id { font-weight: 600; }
.case-cat { display: inline-block; padding: 0 0.4rem; border-radius: 3px; margin-left: 0.5rem; font-size: 0.7rem; text-transform: uppercase; }
.case-cat.parse_error { background: #fff8c5; color: #9a6700; }
.case-cat.infra_error { background: #ddf4ff; color: #0969da; }
.case-cat.incorrect { background: #ffebe9; color: #cf222e; }
</style>
</head>
<body>
<h1>OpenComputer eval dashboard</h1>
<p class="subtitle">Generated {{ generated_at }} · {{ sites|length }} sites · history retained per site: {{ retention }}</p>

{% for s in sites %}
<section class="site">
  <h2>
    {{ s.name }}
    {% set acc = s.latest.accuracy * 100 %}
    {% if acc >= 95 %}<span class="metric green">●</span>
    {% elif acc >= 75 %}<span class="metric amber">●</span>
    {% else %}<span class="metric red">●</span>
    {% endif %}
    <span class="metric">{{ "%.1f"|format(acc) }}%</span>
  </h2>

  <div class="summary">
    <div>Correct: <span class="metric">{{ s.latest.correct }}/{{ s.latest.total }}</span></div>
    <div>Parse failures: <span class="metric">{{ s.latest.parse_failures }}</span></div>
    <div>Infra failures: <span class="metric">{{ s.latest.infra_failures }}</span></div>
    {% if s.latest.cost_usd is not none %}
    <div>Cost: <span class="metric">${{ "%.4f"|format(s.latest.cost_usd) }}</span></div>
    {% endif %}
    <div>Last run: <span class="metric">{{ s.latest.timestamp[:19] }}</span></div>
  </div>

  {% if s.spark_points %}
  <svg class="spark" width="200" height="36" viewBox="0 0 200 36" preserveAspectRatio="none">
    <polyline fill="none" stroke="#0969da" stroke-width="2"
      points="{% for p in s.spark_points %}{{ p.x }},{{ p.y }} {% endfor %}"/>
  </svg>
  <span class="metric" style="font-size: 0.75rem; color: #57606a;">last {{ s.spark_points|length }} runs</span>
  {% endif %}

  {% if s.failing_cases %}
  <details>
    <summary>{{ s.failing_cases|length }} failing case(s) — click to expand</summary>
    {% for c in s.failing_cases %}
    <div class="case">
      <span class="case-id">{{ c.case_id }}</span>
      <span class="case-cat {{ c.error_category or 'incorrect' }}">{{ c.error_category or 'incorrect' }}</span>
      {% if c.input %}<div>input: {{ c.input|string|truncate(180) }}</div>{% endif %}
      {% if c.expected is not none %}<div>expected: {{ c.expected|string|truncate(180) }}</div>{% endif %}
      {% if c.actual is not none %}<div>actual: {{ c.actual|string|truncate(180) }}</div>{% endif %}
      {% if c.parse_error %}<div>error: {{ c.parse_error|truncate(180) }}</div>{% endif %}
    </div>
    {% endfor %}
  </details>
  {% endif %}
</section>
{% endfor %}

{% if not sites %}
<p>No history yet. Run <code>oc eval run all</code> first.</p>
{% endif %}
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add opencomputer/evals/templates/dashboard.html.j2
git commit -m "feat(evals): dashboard HTML template (sparklines + drilldown)"
```

---

### Task 6.2: Implement `dashboard.py`

**Files:**
- Create: `opencomputer/evals/dashboard.py`
- Test: `tests/evals/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_dashboard.py`:

```python
import json
from pathlib import Path
from opencomputer.evals.dashboard import render_dashboard
from opencomputer.evals.history import record_run
from opencomputer.evals.runner import CaseRun, RunReport


def _seed(db_path: Path):
    for site in ["job_change", "instruction_detector"]:
        for i in range(5):
            record_run(
                RunReport(
                    site_name=site, total=10, correct=9 - (i % 2),
                    parse_failures=0, infra_failures=0,
                    case_runs=[CaseRun(case_id="c1", correct=False, parse_error=None, error_category="incorrect", input={"x": 1}, expected="yes", actual="no")],
                ),
                db_path=db_path, model="m", provider="p",
            )


def test_render_dashboard_writes_html(tmp_path):
    db_path = tmp_path / "history.db"
    _seed(db_path)
    out_path = tmp_path / "dashboard.html"

    render_dashboard(db_path=db_path, out_path=out_path, limit=20)

    assert out_path.exists()
    html = out_path.read_text()
    assert "OpenComputer eval dashboard" in html
    assert "job_change" in html
    assert "instruction_detector" in html
    assert "<svg" in html  # sparkline


def test_render_dashboard_handles_empty_history(tmp_path):
    db_path = tmp_path / "history.db"
    out_path = tmp_path / "dashboard.html"
    render_dashboard(db_path=db_path, out_path=out_path, limit=20)
    assert "No history yet" in out_path.read_text()
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement `dashboard.py`**

```python
"""Static HTML dashboard renderer for the eval harness.

Reads history.db, builds per-site summary + sparkline points + failing-case
drilldown, renders Jinja2 template to a single self-contained HTML file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from opencomputer.evals.history import list_sites_with_history, load_recent_runs

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_dashboard(*, db_path: Path, out_path: Path, limit: int = 50, retention: int = 100) -> Path:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("dashboard.html.j2")

    sites_data = []
    for site_name in list_sites_with_history(db_path):
        rows = load_recent_runs(site_name, db_path=db_path, limit=limit)
        if not rows:
            continue
        latest = rows[0]
        # Sparkline: x in [0, 200], y in [0, 36], inverted (lower y = higher accuracy)
        accs = [r["accuracy"] for r in reversed(rows)]
        if len(accs) >= 2:
            n = len(accs)
            spark_points = [
                {
                    "x": round(i * 200 / max(n - 1, 1), 1),
                    "y": round(36 - a * 36, 1),
                }
                for i, a in enumerate(accs)
            ]
        else:
            spark_points = []

        failing_cases = [
            c for c in json.loads(latest["case_runs_json"])
            if not c["correct"]
        ]

        sites_data.append({
            "name": site_name,
            "latest": latest,
            "spark_points": spark_points,
            "failing_cases": failing_cases,
        })

    html = template.render(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sites=sites_data,
        retention=retention,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/evals/test_dashboard.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/dashboard.py tests/evals/test_dashboard.py
git commit -m "feat(evals): static HTML dashboard renderer with sparklines"
```

---

### Task 6.3: Add `oc eval dashboard` subcommand

**Files:**
- Modify: `opencomputer/cli_eval.py`
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_command_writes_file(tmp_path, monkeypatch):
    from opencomputer.evals.history import record_run
    from opencomputer.evals.runner import RunReport

    db_path = tmp_path / "history.db"
    record_run(
        RunReport(site_name="job_change", total=10, correct=10, parse_failures=0, infra_failures=0),
        db_path=db_path, model="m", provider="p",
    )
    out_path = tmp_path / "out.html"
    monkeypatch.setenv("OPENCOMPUTER_EVAL_HISTORY_DB", str(db_path))

    runner = CliRunner()
    result = runner.invoke(eval_app, ["dashboard", "--out", str(out_path)])
    assert result.exit_code == 0
    assert out_path.exists()
    assert "job_change" in out_path.read_text()
```

- [ ] **Step 2: Run test**

Expected: FAIL.

- [ ] **Step 3: Add `dashboard_command`**

```python
@eval_app.command("dashboard")
def dashboard_command(
    out: Path = typer.Option(Path("evals/dashboard/index.html"), "--out"),
    limit: int = typer.Option(50, "--limit"),
    history_db: Path = typer.Option(
        Path(os.environ.get("OPENCOMPUTER_EVAL_HISTORY_DB", "evals/history.db")),
        "--history-db",
    ),
):
    """Render a static HTML dashboard of run history."""
    from opencomputer.evals.dashboard import render_dashboard

    render_dashboard(db_path=history_db, out_path=out, limit=limit)
    typer.echo(f"Dashboard written to {out}")
```

- [ ] **Step 4: Run test**

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_eval.py tests/evals/test_cli_eval.py
git commit -m "feat(evals): oc eval dashboard renders static HTML report"
```

---

## Phase 7: Promote candidates → cases

### Task 7.1: Implement `promote.py`

**Files:**
- Create: `opencomputer/evals/promote.py`
- Test: `tests/evals/test_promote.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_promote.py`:

```python
import json
from pathlib import Path
import pytest

from opencomputer.evals.promote import promote_candidates


def test_promote_appends_candidates_to_cases(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    cases = cases_dir / "x.jsonl"
    candidates = cases_dir / "x.candidates.jsonl"

    cases.write_text('{"id": "a", "input": {}, "expected": "yes"}\n')
    candidates.write_text(
        '{"id": "b", "input": {}, "expected": "no"}\n'
        '{"id": "c", "input": {}, "expected": "yes"}\n'
    )

    n = promote_candidates(site_name="x", cases_dir=cases_dir)
    assert n == 2

    contents = cases.read_text()
    assert '"id": "a"' in contents
    assert '"id": "b"' in contents
    assert '"id": "c"' in contents
    assert not candidates.exists(), "candidates should be cleared after promote"


def test_promote_rejects_duplicate_ids(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    cases = cases_dir / "x.jsonl"
    candidates = cases_dir / "x.candidates.jsonl"

    cases.write_text('{"id": "a", "input": {}, "expected": "yes"}\n')
    candidates.write_text('{"id": "a", "input": {}, "expected": "no"}\n')

    with pytest.raises(ValueError, match="duplicate"):
        promote_candidates(site_name="x", cases_dir=cases_dir)

    # Original cases file unchanged on failure
    assert cases.read_text() == '{"id": "a", "input": {}, "expected": "yes"}\n'
    # Candidates file untouched on failure
    assert candidates.exists()


def test_promote_no_candidates_returns_zero(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    n = promote_candidates(site_name="x", cases_dir=cases_dir)
    assert n == 0
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Implement `promote.py`**

```python
"""Atomic promotion of candidate cases into the canonical cases file."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


def promote_candidates(*, site_name: str, cases_dir: Path) -> int:
    """Append <site>.candidates.jsonl onto <site>.jsonl atomically.

    Returns count of promoted cases. Raises ValueError on ID collision —
    leaves both files untouched.
    """
    cases_path = cases_dir / f"{site_name}.jsonl"
    candidates_path = cases_dir / f"{site_name}.candidates.jsonl"

    if not candidates_path.exists():
        return 0

    candidate_lines = [line for line in candidates_path.read_text().splitlines() if line.strip()]
    if not candidate_lines:
        return 0

    existing_ids: set[str] = set()
    if cases_path.exists():
        for line in cases_path.read_text().splitlines():
            if line.strip():
                existing_ids.add(json.loads(line)["id"])

    candidate_ids: list[str] = []
    for line in candidate_lines:
        cid = json.loads(line)["id"]
        if cid in existing_ids or cid in candidate_ids:
            raise ValueError(
                f"duplicate case id {cid!r} between candidates and existing cases"
            )
        candidate_ids.append(cid)

    # Atomic write: write to temp, fsync, rename.
    cases_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=cases_dir, prefix=f"{site_name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w") as f:
            if cases_path.exists():
                f.write(cases_path.read_text())
                if not cases_path.read_text().endswith("\n"):
                    f.write("\n")
            for line in candidate_lines:
                f.write(line + "\n")
            f.flush()
        shutil.move(str(tmp_path), str(cases_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    candidates_path.unlink()
    return len(candidate_lines)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/evals/test_promote.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/evals/promote.py tests/evals/test_promote.py
git commit -m "feat(evals): atomic promote_candidates with duplicate-ID guard"
```

---

### Task 7.2: Add `oc eval promote` subcommand

**Files:**
- Modify: `opencomputer/cli_eval.py`
- Test: `tests/evals/test_cli_eval.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_promote_command(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "x.jsonl").write_text('{"id": "a", "input": {}, "expected": "yes"}\n')
    (cases_dir / "x.candidates.jsonl").write_text('{"id": "b", "input": {}, "expected": "no"}\n')

    runner = CliRunner()
    result = runner.invoke(eval_app, ["promote", "x", "--cases-dir", str(cases_dir)])
    assert result.exit_code == 0
    assert "Promoted 1 case" in result.output
```

- [ ] **Step 2: Run test**

Expected: FAIL.

- [ ] **Step 3: Add subcommand**

```python
@eval_app.command("promote")
def promote_command(
    site: str = typer.Argument(...),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
):
    """Atomically merge <site>.candidates.jsonl into <site>.jsonl."""
    from opencomputer.evals.promote import promote_candidates

    n = promote_candidates(site_name=site, cases_dir=cases_dir)
    if n == 0:
        typer.echo(f"No candidates to promote for {site}.")
    else:
        typer.echo(f"Promoted {n} case{'s' if n != 1 else ''} for {site}.")
```

- [ ] **Step 4: Run test**

Expected: passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_eval.py tests/evals/test_cli_eval.py
git commit -m "feat(evals): oc eval promote subcommand"
```

---

## Phase 8: Fix the broken sites

### Task 8.1: Add `reflect_v1.md` rubric

**Files:**
- Create: `evals/rubrics/reflect_v1.md`

- [ ] **Step 1: Write rubric**

Create `evals/rubrics/reflect_v1.md`:

```markdown
# reflect_v1 — rubric for Reflection Insights

Grade the response as **correct** ONLY if it satisfies ALL of:

1. **Identifies a real pattern.** The insight names a specific, concrete pattern in the input events — not a generic platitude ("you should write better code"). It cites at least one specific tool, action, or outcome from the trajectory.

2. **Attributes correctly.** The insight's claimed cause matches the actual structure of the events. It doesn't invent events that aren't in the trajectory.

3. **Suggests an actionable change.** The insight proposes something specific the agent could do differently next time — not just "do better." Action items must be testable: "use TodoWrite before multi-step edits" is actionable; "be more careful" is not.

4. **Avoids obvious genericity.** Reject responses that would apply equally well to any session. The insight must be specific enough that it would NOT make sense applied to a different trajectory.

5. **Is honest about uncertainty.** If the trajectory has only 1-2 events, the response should acknowledge limited signal rather than pattern-match aggressively.

Mark **incorrect** if any criterion fails. When borderline, prefer incorrect — false-positive insights pollute the procedural memory loop downstream.
```

- [ ] **Step 2: Commit**

```bash
git add evals/rubrics/reflect_v1.md
git commit -m "feat(evals): reflect_v1 rubric for Insight grading"
```

---

### Task 8.2: Implement `reflect_for_eval` with structured input

**Files:**
- Modify: `opencomputer/evolution/reflect.py:123-140`
- Modify: `opencomputer/evals/adapters.py:19-31`
- Modify: `opencomputer/evals/generation_prompts.py` (REFLECT_PROMPT)
- Test: `tests/evals/test_reflect_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/evals/test_reflect_adapter.py`:

```python
import pytest
from opencomputer.evals.adapters import adapter_reflect


def test_reflect_adapter_with_structured_events_returns_string():
    case_input = {
        "events": [
            {
                "action_type": "tool_call",
                "tool_name": "Edit",
                "outcome": "success",
                "metadata": {"file": "x.py"},
            },
            {
                "action_type": "tool_call",
                "tool_name": "Edit",
                "outcome": "failure",
                "metadata": {"error": "string not found"},
            },
            {
                "action_type": "tool_call",
                "tool_name": "Edit",
                "outcome": "success",
                "metadata": {"file": "x.py"},
            },
        ]
    }

    # ReflectionEngine may make a real LLM call; for this test just confirm
    # we don't raise on input shape and return a string.
    try:
        result = adapter_reflect(case_input)
    except Exception as e:
        # Accept infra error (no LLM available) but NOT NotImplementedError
        assert "NotImplementedError" not in type(e).__name__
        return
    assert isinstance(result, str)


def test_reflect_adapter_rejects_legacy_session_excerpt():
    """Old shape must error clearly, not silently misbehave."""
    with pytest.raises((KeyError, ValueError)):
        adapter_reflect({"session_excerpt": "old shape"})
```

- [ ] **Step 2: Run tests**

Expected: FAIL.

- [ ] **Step 3: Update `reflect_for_eval`**

In `opencomputer/evolution/reflect.py`, replace `reflect_for_eval`:

```python
def reflect_for_eval(events: list[dict]) -> str:
    """Eval-only entry point.

    Builds a TrajectoryRecord from the structured event list, runs
    ReflectionEngine.reflect(), and returns the joined Insight texts.
    Raises if the input shape doesn't match TrajectoryEvent's contract.
    """
    import time
    from opencomputer.evolution.trajectory import (
        SCHEMA_VERSION_CURRENT,
        TrajectoryEvent,
        TrajectoryRecord,
    )

    if not isinstance(events, list):
        raise ValueError(f"events must be a list, got {type(events).__name__}")

    session_id = "_eval_synthetic"
    started_at = time.time()
    traj_events: list[TrajectoryEvent] = []
    for i, ev in enumerate(events):
        traj_events.append(
            TrajectoryEvent(
                session_id=session_id,
                message_id=i,
                action_type=ev["action_type"],
                tool_name=ev.get("tool_name"),
                outcome=ev["outcome"],
                timestamp=started_at + i,
                metadata=ev.get("metadata", {}),
            )
        )

    record = TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at,
        ended_at=started_at + len(events),
        events=tuple(traj_events),
        completion_flag=True,
    )

    engine = ReflectionEngine()
    insights = engine.reflect([record])
    return "\n".join(getattr(i, "text", str(i)) for i in insights)
```

- [ ] **Step 4: Update adapter**

In `opencomputer/evals/adapters.py`, replace `adapter_reflect`:

```python
def adapter_reflect(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.evolution.reflect for evaluation.

    case_input shape: {"events": [<TrajectoryEvent dict>, ...]}
    Returns: joined Insight texts.
    """
    from opencomputer.evolution.reflect import reflect_for_eval

    if "events" not in case_input:
        raise KeyError("reflect adapter requires 'events' key (legacy 'session_excerpt' is removed)")
    return reflect_for_eval(case_input["events"])
```

- [ ] **Step 5: Update generation prompt**

In `opencomputer/evals/generation_prompts.py`, replace `REFLECT_PROMPT`:

```python
REFLECT_PROMPT = """Generate {n} diverse test cases for an agent post-response reflector.

Each case has a structured event list representing one agent session.
Events match the TrajectoryEvent schema.

Return a JSON array. Each case has:
  id: short slug
  input: {{"events": [{{"action_type": "tool_call"|"user_reply"|"assistant_reply"|"error", "tool_name": <str|null>, "outcome": "success"|"failure"|"blocked_by_hook"|"user_cancelled", "metadata": {{<short tool-name-level keys, no raw text>}}}}]}}
  rubric_id: "reflect_v1"

Construct sessions that exhibit clear patterns: repeated failures of the same tool, mode-switching, premature termination, ignored hook blocks, etc.

Output the JSON array only, no preamble."""
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/evals/test_reflect_adapter.py -v
```

Expected: passed.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/evolution/reflect.py opencomputer/evals/adapters.py opencomputer/evals/generation_prompts.py tests/evals/test_reflect_adapter.py
git commit -m "feat(evals): reflect_for_eval accepts structured events; new adapter shape"
```

---

### Task 8.3: Hand-author 10 reflect cases

**Files:**
- Create: `evals/cases/reflect.jsonl`

- [ ] **Step 1: Write 10 cases**

Create `evals/cases/reflect.jsonl`:

```json
{"id": "ref_001_repeat_edit_failure", "input": {"events": [{"action_type": "tool_call", "tool_name": "Edit", "outcome": "failure", "metadata": {"error": "string_not_found"}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "failure", "metadata": {"error": "string_not_found"}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "failure", "metadata": {"error": "string_not_found"}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_002_no_todo_for_multi_step", "input": {"events": [{"action_type": "tool_call", "tool_name": "Edit", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "failure", "metadata": {"exit_code": 1}}, {"action_type": "tool_call", "tool_name": "Edit", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "success", "metadata": {"exit_code": 0}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_003_blocked_by_hook_ignored", "input": {"events": [{"action_type": "tool_call", "tool_name": "Bash", "outcome": "blocked_by_hook", "metadata": {"hook": "PreToolUse"}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "blocked_by_hook", "metadata": {"hook": "PreToolUse"}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "blocked_by_hook", "metadata": {"hook": "PreToolUse"}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_004_premature_completion", "input": {"events": [{"action_type": "user_reply", "tool_name": null, "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "assistant_reply", "tool_name": null, "outcome": "success", "metadata": {}}, {"action_type": "user_reply", "tool_name": null, "outcome": "success", "metadata": {"sentiment": "frustrated"}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_005_clean_short_session", "input": {"events": [{"action_type": "user_reply", "tool_name": null, "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "assistant_reply", "tool_name": null, "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_006_grep_then_read_loop", "input": {"events": [{"action_type": "tool_call", "tool_name": "Grep", "outcome": "success", "metadata": {"matches": 12}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Grep", "outcome": "success", "metadata": {"matches": 8}}, {"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_007_user_cancelled_long_op", "input": {"events": [{"action_type": "tool_call", "tool_name": "Bash", "outcome": "user_cancelled", "metadata": {"runtime_seconds": 45}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "success", "metadata": {"runtime_seconds": 2}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_008_error_recovery_path", "input": {"events": [{"action_type": "tool_call", "tool_name": "Write", "outcome": "failure", "metadata": {"error": "permission_denied"}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "success", "metadata": {"command": "chmod"}}, {"action_type": "tool_call", "tool_name": "Write", "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_009_minimal_one_event", "input": {"events": [{"action_type": "user_reply", "tool_name": null, "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
{"id": "ref_010_mixed_tools_no_pattern", "input": {"events": [{"action_type": "tool_call", "tool_name": "Read", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Grep", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Glob", "outcome": "success", "metadata": {}}, {"action_type": "tool_call", "tool_name": "Bash", "outcome": "success", "metadata": {}}, {"action_type": "assistant_reply", "tool_name": null, "outcome": "success", "metadata": {}}]}, "rubric_id": "reflect_v1"}
```

- [ ] **Step 2: Verify cases parse**

```bash
python -c "import json; [json.loads(line) for line in open('evals/cases/reflect.jsonl').read().splitlines() if line.strip()]; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add evals/cases/reflect.jsonl
git commit -m "feat(evals): 10 hand-authored reflect cases (structured events)"
```

---

### Task 8.4: Verify all four sites run cleanly

**Files:** none changed

- [ ] **Step 1: Run full eval suite**

```bash
oc eval run all
```

Expected behavior:
- `job_change` 30/30 = 100%, baseline +0%
- `instruction_detector` 16/30 = 53.3% (or as-is), baseline +0%
- `llm_extractor`: if Ollama not on PATH → 30 infra failures (NOT parse failures); if Ollama running → real result
- `reflect`: if grader provider not registered → skipped with reason; if available → real result

If any site shows unexpected `parse_failures` count from previous bug, investigate.

- [ ] **Step 2: Save baselines for newly-runnable sites**

If `reflect` ran successfully (Anthropic key set):

```bash
oc eval run reflect --save-baseline
```

If `llm_extractor` ran successfully (Ollama running):

```bash
oc eval run llm_extractor --save-baseline
```

- [ ] **Step 3: Commit any new baselines**

```bash
git add evals/baselines/
git commit -m "chore(evals): freeze new baselines for reflect + llm_extractor (where runnable)"
```

(Skip the commit if no new baselines were produced — both sites still gated on local infra.)

---

## Phase 9: CI gate

### Task 9.1: Add eval regression gate to test.yml

**Files:**
- Modify: `/Users/saksham/Vscode/claude/.github/workflows/test.yml`

- [ ] **Step 1: Read current workflow**

```bash
cat /Users/saksham/Vscode/claude/.github/workflows/test.yml
```

- [ ] **Step 2: Add eval step**

Append after the existing pytest step (use the actual job name from the file). Example:

```yaml
      - name: Eval regression gate (deterministic-graded sites only without API key)
        run: |
          cd OpenComputer
          pip install -e ".[dev]"
          python -m opencomputer.cli eval regress all
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        continue-on-error: false
```

The regress command already skips rubric-graded sites when no provider is wired, so this step is safe even on PRs from forks without the secret.

- [ ] **Step 3: Verify locally first**

```bash
cd OpenComputer
python -m opencomputer.cli eval regress all
echo "exit: $?"
```

Expected: exit 0 (no regressions on baselined sites).

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add .github/workflows/test.yml
git commit -m "ci(evals): wire oc eval regress all into test workflow"
```

---

## Phase 10: Documentation

### Task 10.1: Write evals reference doc

**Files:**
- Create: `OpenComputer/docs/refs/evals.md`

- [ ] **Step 1: Write doc**

Create `OpenComputer/docs/refs/evals.md` (~350 words):

```markdown
# Eval system reference

## What it is

A regression alarm for LLM-decision sites in OpenComputer. Each "site" is one
place where the agent makes a structured decision (extract a fact, classify
a prompt, reflect on a session). Sites are graded by exact match, schema
match, or LLM rubric, against frozen baselines.

## Sites (current)

| Site | Grader | Production target | Notes |
|---|---|---|---|
| `job_change` | exact | `awareness.life_events.job_change.detect_for_eval` | Regex, no LLM |
| `instruction_detector` | exact | `security.instruction_detector.detect` | Regex, no LLM. Threshold 0.10 |
| `llm_extractor` | schema (subset) | `profile_bootstrap.llm_extractor.extract_for_eval` | Requires Ollama |
| `reflect` | rubric (`reflect_v1`) | `evolution.reflect.reflect_for_eval` | Requires grader provider (Anthropic etc.) |

## Daily usage

```bash
oc eval run all                                # run everything
oc eval run job_change --verbose               # see failing case detail
oc eval run job_change --case-id jc_pos_001    # iterate on one case
oc eval run all --json                         # for scripting
oc eval regress all                            # CI gate (exits non-zero on >threshold drop)
oc eval generate llm_extractor -n 30           # LLM-author candidate cases
oc eval promote llm_extractor                  # merge candidates → cases atomically
oc eval history all --limit 20                 # recent runs
oc eval dashboard                              # render evals/dashboard/index.html
```

## Adding a site

1. Add a `*_for_eval` shim to your production module returning the structured value.
2. Add an `EvalSite` to `opencomputer/evals/sites.py` (set `regression_threshold` if 5pp default isn't right).
3. Write a tiny adapter in `opencomputer/evals/adapters.py` that calls your shim.
4. Drop 30 cases in `evals/cases/<name>.jsonl` (or `oc eval generate` then promote).
5. For rubric grading: create `evals/rubrics/<id>.md`.
6. `oc eval run <name> --save-baseline` to freeze.

## Error categories

- `correct` — passed.
- `incorrect` — model returned wrong answer.
- `parse_error` — model output couldn't parse (real signal).
- `infra_error` — backend unavailable (Ollama down, provider not registered).
  **Excluded from accuracy.** Doesn't trip CI.

## CI

`.github/workflows/test.yml` runs `oc eval regress all`. Sites without grader
providers are skipped (no false negatives on forks).
```

- [ ] **Step 2: Commit**

```bash
git add OpenComputer/docs/refs/evals.md
git commit -m "docs(evals): comprehensive reference for eval system v2"
```

---

### Task 10.2: Add README pointer

**Files:**
- Modify: `OpenComputer/README.md`

- [ ] **Step 1: Read current README**

```bash
grep -n "eval" OpenComputer/README.md || echo "no existing eval section"
```

- [ ] **Step 2: Add a one-line pointer**

If a "## Quality" or "## Testing" section exists, append:

```markdown
- **LLM-decision regression gate:** see [docs/refs/evals.md](docs/refs/evals.md). Run `oc eval regress all` locally; CI runs it on every PR.
```

If not, add a new section before "## License" (or wherever the table of contents ends):

```markdown
## Eval system

OpenComputer ships an eval harness for LLM-decision sites — places where a model
or detector makes a structured choice (classify, extract, reflect). Every change
is gated against frozen baselines.

See [docs/refs/evals.md](docs/refs/evals.md).

```bash
oc eval run all                # run all sites
oc eval dashboard              # render evals/dashboard/index.html
```
```

- [ ] **Step 3: Commit**

```bash
git add OpenComputer/README.md
git commit -m "docs(readme): point to eval system reference"
```

---

## Phase 11: Final verification

### Task 11.1: Full test suite + lint

**Files:** none

- [ ] **Step 1: Run full pytest**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/ -x --tb=short
```

Expected: all tests pass (existing + ~30 new).

- [ ] **Step 2: Lint**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: no errors.

- [ ] **Step 3: Run end-to-end smoke test**

```bash
oc eval run all
oc eval run job_change --verbose
oc eval history all --limit 5
oc eval dashboard
open evals/dashboard/index.html  # visual check (mac)
```

Expected: all run cleanly. Dashboard shows site sections with sparklines and (if any) failing cases.

- [ ] **Step 4: Confirm `oc eval regress all` exits 0**

```bash
oc eval regress all
echo "exit: $?"
```

Expected: `No regressions detected.` and exit 0.

- [ ] **Step 5: Commit any small fixups discovered**

If lint/test issues surface, fix and commit per the existing pattern. No new functionality.

---

### Task 11.2: Open PR

**Files:** none

- [ ] **Step 1: Push branch**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git push -u origin feat/eval-system-c
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "Eval system v2 (Scope C): bug fixes + observability + CI gate" --body "$(cat <<'EOF'
## Summary
- Fixes all four broken/deferred eval sites (reflect rubric + structured events, llm_extractor Ollama-aware, instruction_detector + job_change kept green)
- Adds error categorization (incorrect / parse_error / infra_error) so env issues don't trip baselines
- Adds `--verbose`, `--json`, `--case-id` flags for actionable runs
- Adds `oc eval promote`, `oc eval history`, `oc eval dashboard` subcommands
- Adds per-site regression thresholds (instruction_detector at 0.10)
- Adds cost tracking (tokens + USD) for grader-driven runs
- Adds SQLite run history with retention
- Adds static HTML dashboard with sparklines + failing-case drilldown
- Wires `oc eval regress all` into CI

Spec: `docs/superpowers/specs/2026-05-03-eval-system-c-design.md`
Plan: `docs/superpowers/plans/2026-05-03-eval-system-c.md`

## Test plan
- [ ] `pytest tests/evals/ -v` (~30 new tests)
- [ ] `pytest tests/ -x` full suite green
- [ ] `ruff check opencomputer/ plugin_sdk/ extensions/ tests/` clean
- [ ] `oc eval run all` runs to completion
- [ ] `oc eval run job_change --verbose` shows failing cases inline
- [ ] `oc eval dashboard` produces a valid HTML file
- [ ] `oc eval promote x` round-trips with duplicate-ID guard
- [ ] CI workflow gate passes on this PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Wait for CI**

```bash
gh pr checks --watch
```

Expected: all checks pass.

- [ ] **Step 4: Report PR URL**

The PR URL will be in the `gh pr create` output — relay to user.

---

## Self-review checklist (run before handing off)

- [ ] Every spec section in §4 has an implementing task
- [ ] No task has placeholder code or "implement later" steps
- [ ] Type names consistent across tasks (RunReport, CaseRun, GradeResult, ErrorCategory)
- [ ] Method signatures match between definition and use sites
- [ ] All file paths absolute or rooted at `OpenComputer/`
- [ ] Each task has a commit step
- [ ] CI gate is non-blocking on infra failures
- [ ] Backwards-compat preserved (existing baselines load, existing sites unchanged)
