# Eval system reference

## What it is

A regression alarm for LLM-decision sites in OpenComputer. Each "site" is one
place where the agent makes a structured decision (extract a fact, classify
a prompt, reflect on a session). Sites are graded by exact match, schema
match, or LLM rubric, against frozen baselines.

## Sites (current)

| Site | Grader | Production target | Threshold | Notes |
|---|---|---|---|---|
| `job_change` | exact | `awareness.life_events.job_change.detect_for_eval` | 0.05 | Regex, no LLM |
| `instruction_detector` | exact | `security.instruction_detector.detect` | 0.10 | Regex, noisy detector |
| `llm_extractor` | schema (subset) | `profile_bootstrap.llm_extractor.extract_for_eval` | 0.05 | Requires Ollama running |
| `reflect` | rubric (`reflect_v1`) | `evolution.reflect.reflect_for_eval` | 0.05 | Requires grader provider (Anthropic etc.); structured event input |

## Daily usage

```bash
oc eval run all                                # run everything
oc eval run job_change --verbose               # see failing case detail
oc eval run job_change --case-id jc_pos_001    # iterate on one case
oc eval run all --json                         # machine-readable; multi-site uses {"sites":[...]} envelope
oc eval regress all                            # CI gate (exits non-zero on >threshold drop)
oc eval generate llm_extractor -n 30           # LLM-author candidate cases
oc eval promote llm_extractor                  # merge candidates → cases atomically
oc eval history all --limit 20                 # recent runs
oc eval dashboard                              # render evals/dashboard/index.html
```

## Adding a site

1. Add a `*_for_eval` shim to your production module returning the structured value.
2. Add an `EvalSite` to `opencomputer/evals/sites.py` (set `regression_threshold` if 0.05 default isn't right).
3. Write a tiny adapter in `opencomputer/evals/adapters.py` that calls your shim.
4. Drop 30 cases in `evals/cases/<name>.jsonl` (or `oc eval generate` then promote).
5. For rubric grading: create `evals/rubrics/<id>.md`.
6. `oc eval run <name> --save-baseline` to freeze.

## Error categories

Every failed case lands in one of three buckets:

- `incorrect` — model returned the wrong answer (real signal).
- `parse_error` — model output couldn't parse (real signal: schema/format drift).
- `infra_error` — backend unavailable (Ollama down, provider not registered, network timeout).
  **Excluded from accuracy.** Does NOT trip the regression gate.

`accuracy = correct / (total - infra_failures)`. The harness distinguishes
"the model got worse" from "your environment isn't set up", so a missing API key
or stopped daemon never produces a false-positive regression.

## Cost tracking

Rubric-graded runs invoke a paid LLM. Each run records `input_tokens`,
`output_tokens`, and a USD estimate (Anthropic Sonnet/Opus list prices baked in;
unknown models report `cost_usd: null`). Baseline JSON persists the snapshot;
the dashboard surfaces per-run cost.

## Run history (SQLite)

`evals/history.db` (gitignored). One row per run with full per-case detail
serialized in `case_runs_json` for drilldown. Default retention: 100 runs per
site (pruned at write time). Skip writes with `oc eval run --no-history`.

## Dashboard

`oc eval dashboard` renders `evals/dashboard/index.html` (gitignored) — a
self-contained HTML file with sparklines (raw SVG, no JS), per-site summaries,
and an expandable failing-case dropdown for the latest run. Open in a browser:

```bash
oc eval dashboard && open evals/dashboard/index.html
```

## CI

`.github/workflows/test.yml` runs `oc eval regress all` on every PR. Sites
without grader providers are skipped (no false negatives on forks lacking
`ANTHROPIC_API_KEY`). Each site's regression threshold is honored
individually — `instruction_detector` (0.10) tolerates more variance than
`job_change` (0.05).

## Known limitation: silent infra-failure masking

If a real regression coincides with an infra outage, accuracy may *appear*
unchanged (the failing cases simply move from `incorrect` to `infra_failure`
and drop out of the denominator). Mitigations:

- The dashboard always surfaces raw `infra_failures` count — visible inspection.
- `oc eval history all` shows infra counts per run for trend analysis.
- Future: `--strict-infra` flag for `regress` to fail on any *new* infra failure.

For now: spot-check the dashboard weekly when running with infra dependencies
(e.g. Ollama on llm_extractor, Anthropic on reflect).
