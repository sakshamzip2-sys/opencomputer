# Eval System v2 (Scope C) — Design

**Status:** Approved 2026-05-03 — proceeds to implementation plan
**Author:** Claude (Opus 4.7) for Saksham
**Builds on:** `cfbcfcf3` (foundation), `e31d90a0` (cases), `ab4314f4` (PR #374)

---

## 1. Problem statement

The eval harness landed in two waves: a foundation in `cfbcfcf3` and llm_extractor cases in PR #374. Running `oc eval run all` against the as-shipped state surfaces several issues that prevent it from doing its job (catching silent regressions in LLM-decision sites):

| | Symptom | Root cause |
|---|---|---|
| 1 | `reflect` site is unrunnable | `evals/rubrics/reflect_v1.md` missing, `reflect_for_eval` raises `NotImplementedError`, no cases file, no baseline, input shape (`session_excerpt: str`) is fundamentally incompatible with `TrajectoryRecord`'s privacy-validated event structure |
| 2 | `llm_extractor` reports 30/30 parse failures | `extract_for_eval` hard-codes the Ollama backend; when Ollama isn't running, every case raises `ExtractorUnavailableError`. Reported as a quality regression but is actually an environment problem |
| 3 | Reports show only totals — no way to see *which* cases failed | `format_report` discards the per-case detail that `RunReport.case_runs` carries |
| 4 | Generator writes `<site>.candidates.jsonl` but no command merges them in | Promotion is manual `mv` |
| 5 | No CI gate | `oc eval regress all` exists but isn't wired into `.github/workflows/` |
| 6 | Cost of grader runs is invisible | The rubric grader makes paid LLM calls; nothing tracks tokens or USD |
| 7 | Regression threshold is hard-coded at 5pp | All sites get the same sensitivity even when their natural variance differs |
| 8 | No run history beyond the single most recent baseline JSON | Trend analysis requires re-running everything; lost history if baseline gets re-frozen |
| 9 | No dashboard | Output is per-invocation terminal text only |

## 2. Goals

1. **Fix all four broken-or-deferred sites** so `oc eval run all` runs cleanly end-to-end on the user's machine without spurious failures.
2. **Make failures actionable** — when a case fails, the user can see the exact input, expected, and actual without writing Python.
3. **Wire the regression gate into CI** so prompt/regex/model changes that degrade quality fail builds.
4. **Track cost** — every grader-driven run records tokens and USD estimate.
5. **Per-site thresholds** — let each `EvalSite` declare its own regression sensitivity.
6. **Persistent run history** — every run lands in a SQLite table for trend analysis, with a simple retention policy.
7. **Static HTML dashboard** — `oc eval dashboard` renders all sites' history into `evals/dashboard/index.html`. No web server, no extra deps beyond Jinja2 (already in project).
8. **Categorize failures** — separate "real regression" from "environment problem" so baselines stay meaningful when infra is missing.

## 3. Non-goals

- Multi-machine eval distribution.
- Live web server or hosted dashboard.
- Comparing across providers in one run (already handled by re-running with `--grader-model`).
- Eval-time replay of historical sessions.
- Auto-baseline updates (every promotion stays explicit, `--save-baseline`).
- Slack/email notifications.
- Replacing pytest for testing the harness — the harness is tested *with* pytest.

## 4. Architecture

### 4.1 Module map (one-way dependency: `evals → core`, never reverse)

```
opencomputer/evals/
├── types.py             ← + RegressionThreshold, RunMetadata, CostInfo
├── sites.py             ← + per-site threshold field
├── adapters.py          ← + reflect adapter (new shape)
├── runner.py            ← + ErrorCategory, infra-vs-parse split
├── baseline.py          ← + cost in snapshot
├── generator.py         ← unchanged
├── generation_prompts.py ← reflect prompt updated for new input shape
├── providers.py         ← + token/cost capture
├── report.py            ← + verbose, json renderers
├── history.py           ← NEW: SQLite history + retention
├── dashboard.py         ← NEW: Jinja2 → HTML
├── promote.py           ← NEW: candidates → cases
└── graders/{exact,schema,rubric}.py  ← rubric.py captures usage

opencomputer/cli_eval.py ← + dashboard, promote subcommands; flags expanded
evals/cases/             ← + reflect.jsonl (new shape), llm_extractor revisited
evals/baselines/         ← + reflect.json, llm_extractor.json
evals/rubrics/           ← + reflect_v1.md
evals/dashboard/         ← NEW: generated HTML + assets (gitignored)
evals/history.db         ← NEW: SQLite (gitignored)
evals/templates/         ← NEW: Jinja2 templates for dashboard
```

### 4.2 Error categorization

`GradeResult` gains an `error_category` field:

```python
ErrorCategory = Literal["correct", "incorrect", "parse_error", "infra_error"]
```

- `correct` — answer matches.
- `incorrect` — model returned wrong answer (real signal).
- `parse_error` — model output couldn't be parsed (real signal: schema/format drift).
- `infra_error` — backend unavailable (Ollama down, provider not registered, network failure). **Excluded from baseline accuracy.** Reported separately.

Runner stops conflating env errors with model errors. `RunReport` exposes `correct`, `incorrect`, `parse_failures`, **and** new `infra_failures`. Accuracy is `correct / (total - infra_failures)`.

### 4.3 Per-site thresholds

`EvalSite` adds `regression_threshold: float = 0.05`. The `regress` command reads it instead of using a global constant.

### 4.4 Cost tracking

`ProviderShim.complete()` returns an object with `.text` AND `.usage`. `LLMRubricGrader` records `input_tokens`/`output_tokens` per case. `RunReport` aggregates and `BaselineSnapshot` persists totals. `report.format_report` shows USD using a small in-process price table (Anthropic-only at v2; extends naturally).

### 4.5 Run history table

```sql
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,            -- ISO 8601 UTC
    accuracy REAL NOT NULL,
    correct INTEGER NOT NULL,
    incorrect INTEGER NOT NULL,
    parse_failures INTEGER NOT NULL,
    infra_failures INTEGER NOT NULL,
    total INTEGER NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    grader_model TEXT,                  -- nullable for non-rubric sites
    cost_usd REAL,                      -- nullable
    case_runs_json TEXT NOT NULL        -- JSON-encoded list[CaseRun] for drilldown
);
CREATE INDEX idx_eval_runs_site_ts ON eval_runs(site_name, timestamp);
```

Retention: keep last 100 runs per site (configurable via `eval.history.retention` in config; runs over the limit get pruned at write time).

### 4.6 Dashboard

`oc eval dashboard` renders into `evals/dashboard/index.html`:
- One section per site with a sparkline (raw `<svg>` — no JS dep) of accuracy over its last N runs
- Latest run summary (correct/incorrect/parse/infra, cost)
- Failing-case drilldown (collapsible, last run only)
- Generated `<meta>` timestamp

Templates use Jinja2 (already a project dep). Output is one self-contained HTML file plus a tiny inline CSS block. Open with `open evals/dashboard/index.html`.

### 4.7 reflect site redesign

Input shape changes from `{"session_excerpt": str}` to `{"events": list[dict]}` matching the production `TrajectoryEvent` shape. Each test event has `action_type`, `tool_name`, `outcome`, `metadata`. `reflect_for_eval` builds a real `TrajectoryRecord`, calls `ReflectionEngine.reflect()`, and returns the joined Insight texts as a string for the rubric grader.

`evals/rubrics/reflect_v1.md` defines what makes a good reflection: identifies a meaningful pattern, attributes it correctly, suggests an actionable change, isn't trivially generic. ~150 words.

10 hand-authored cases shipped initially — one per Insight category the reflector should produce.

### 4.8 llm_extractor unblock

`extract_for_eval` gets a guard: probe `is_ollama_available()` first; if false, raise a typed `OllamaUnavailableError` that the runner classifies as `infra_error`, not `parse_error`. When Ollama IS available, the existing path runs and we generate a real baseline.

## 5. CLI surface

```bash
# Existing (kept):
oc eval run <site|all> [--save-baseline] [--cases-dir DIR] [--baselines-dir DIR] [--grader-model M]
oc eval generate <site> -n N
oc eval regress <site|all>

# Expanded:
oc eval run <site|all> --verbose                    # ← new: dump failing case details
oc eval run <site|all> --json                       # ← new: machine-readable
oc eval run <site|all> --case-id <id>               # ← new: filter to one case
oc eval run <site|all> --no-history                 # ← new: skip SQLite write

# New subcommands:
oc eval promote <site> [--auto-id]                  # ← merge candidates → cases
oc eval dashboard [--out PATH] [--limit N]          # ← render HTML
oc eval history <site|all> [--limit N] [--json]     # ← print recent runs
```

## 6. Data flow (one happy-path run)

```
oc eval run job_change --verbose
  → cli_eval.run_command
  → runner.run_site(...)
      → load cases (jsonl)
      → for each case: adapter() → grader() → CaseRun
          ├─ correct/incorrect → real
          ├─ parse_error      → real (model output broken)
          └─ infra_error      → env (excluded from accuracy)
      → return RunReport (with cost aggregate if grader was used)
  → baseline.compare_to_baseline(...) → BaselineDiff
  → history.record_run(report)            ← persists to SQLite, prunes old
  → report.format_report(report, verbose=True)  ← prints failing cases inline
```

## 7. Testing strategy

Every new module gets a test file. Existing tests retained — new tests added for:

- `test_runner_categorizes_infra_errors` — Ollama-unavailable case → `infra_failures`, NOT `parse_failures`
- `test_grade_result_error_category` — type-level assertion on the literal
- `test_per_site_thresholds` — `instruction_detector` at threshold 0.10 doesn't trip on a 7pp drop
- `test_cost_aggregation` — rubric grader run aggregates input/output tokens
- `test_history_record_and_prune` — write 105 runs for one site → 100 retained
- `test_history_idempotent_on_no_history_flag` — `--no-history` skips DB write
- `test_dashboard_renders` — given fixture history, output HTML contains all site sections
- `test_promote_atomically` — partial failure mid-promote leaves source intact
- `test_reflect_for_eval_with_real_events` — given a fixture event list, returns insight text
- `test_report_verbose_shows_case_details` — failing cases include input + expected + actual

Plus: a bash-level smoke test that runs `oc eval run all` end-to-end on the committed fixtures and asserts exit 0.

## 8. Backwards compatibility

- Existing baseline JSON files load unchanged (new `cost_usd` field defaults `None`).
- Existing `EvalSite` entries get `regression_threshold` defaulted to 0.05; no migration needed.
- `RunReport.parse_failure_rate` retained as a computed property (now computed off the new categorization).
- Older `case_runs` without `error_category` deserialize as `correct=False, error_category=None` → treated as legacy `incorrect`.

## 9. CI integration

Add to `.github/workflows/test.yml`:

```yaml
- name: eval regression gate
  run: |
    cd OpenComputer
    pip install -e ".[dev]"
    python -m opencomputer.cli eval regress all
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

The gate is non-blocking on `infra_error` (e.g., if Anthropic API key isn't set in a fork's CI). Real regressions still fail the build.

## 10. Risks + mitigations

| Risk | Mitigation |
|---|---|
| SQLite history grows unbounded | Retention policy (100/site) enforced at write time |
| Dashboard becomes stale | `oc eval dashboard` is fast (~1s) and idempotent; users re-run after each `oc eval run` (or wire it post-run) |
| reflect site cases require maintenance as TrajectoryEvent shape evolves | Cases are in JSONL — schema mismatch detected at adapter call → `infra_error` with clear message |
| Grader cost runs away | Per-run cost printed in report; future: optional spend cap |
| New CI step adds runtime to every PR | `oc eval regress all` runs the deterministic-graded sites only by default if `ANTHROPIC_API_KEY` is missing — sub-second |
| **Silent infra failure could mask a real regression** | Documented limitation: when accuracy is computed off `usable_total` and infra failures replace what would have been incorrect answers, the rate may *appear* unchanged. Mitigation: dashboard always shows raw infra-failure count; future `--strict-infra` flag for regress to fail on any new infra failure. Escape hatch today: inspect `oc eval history` weekly. |

## 11. Out-of-scope (revisit after dogfood)

- Multi-provider parallel runs.
- Bisecting prompt changes against eval results.
- Drift-detection on baselines themselves (e.g., warn if a baseline is >90 days old).
- Auto-promote with confidence intervals.
- Real-time dashboard refresh.

## 12. Definition of done

1. `oc eval run all` exits 0 on a clean machine (with or without Ollama).
2. CI workflow runs `oc eval regress all` on every PR.
3. `oc eval run X --verbose` shows failing case details.
4. `oc eval dashboard` produces a self-contained HTML file.
5. `oc eval promote X` merges candidates atomically.
6. Per-site thresholds work — at least one site (`instruction_detector`) uses a non-default threshold.
7. Run history table populated; `oc eval history all` prints recent runs.
8. Cost shown in report and persisted in baseline.
9. All four broken sites green on baseline.
10. Test count grows by at least 30; existing tests still pass.
11. Documentation updated in `OpenComputer/docs/refs/evals.md` (new file).
