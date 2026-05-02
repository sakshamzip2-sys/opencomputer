# OpenComputer Quality Foundation — Design

**Date:** 2026-05-02
**Status:** Approved (verbal), proceeding to implementation plan
**Branch:** `feat/quality-foundation`
**Author:** Claude (with Saksham's iterative guidance + audit ritual)

## Problem

OpenComputer has shipped substantial LLM-driven functionality (six life-event detectors, prompt evolution, post-response reflection, profile bootstrap extraction, prompt-injection guard) with **no measurement of correctness**. There is no `evals/` directory, no labeled test set, no baseline accuracy for any classifier or extractor. When models swap (PR #263 enables mid-session provider swap) or prompts change, regressions are invisible until users feel them.

Three concrete symptoms drove this spec:

1. **Three production call sites parse raw LLM JSON output with `json.loads`** (`evolution/prompt_evolution.py:169`, `evolution/reflect.py:146`, `profile_bootstrap/llm_extractor.py:587`). Bad model output crashes the loop. The Anthropic provider supports Structured Outputs but no site uses them.

2. **Tool descriptions on every turn** — grep finds 73 tool-related registration matches in `opencomputer/`; the count of unique `BaseTool` subclasses is to be confirmed in Phase 3 Step 1. At an estimated ~200 tokens average per description, the per-turn tool-description payload could be in the 5K–15K-token range. Even with the 4-breakpoint prompt cache shipped in PR #171, every cache miss pays this cost. Whether selective `defer_loading` would help is unmeasured.

3. **Cost / cache-hit / token / latency observability is scattered** across ten files (cli_insights, slash, cost_guard/, etc.) with no single sink. Cache-hit metrics already collected in `agent/loop.py` are not surfaced anywhere user-visible.

## Approach

Bundle four phases into a single `feat/quality-foundation` branch. Each phase produces a measurable outcome and is independently shippable, but they compose: Phase 1 (eval harness) is what proves Phase 2 (Structured Outputs) is a win, and Phase 4 (observability) is what proves Phase 3 (tool budget) is a win.

Out of scope and explicitly excluded:

- **Persona system removal** — being executed by the parallel session per `2026-05-01-persona-system-removal-design.md`. This branch does not touch `awareness/personas/`.
- **Provider extension audit** (24 OpenAI-compat providers) — maintenance concern but explicitly user-driven; not this spec.
- **Channel adapter dedup** — listed in CLAUDE.md Tier 2 dogfood-gated work.
- **Agent-loop chat quality grading** — open-ended conversation is not amenable to automated grading at the cost we're willing to pay.
- **MCP connector mode** — OpenComputer has its own MCP client; the connector's HTTP-only and non-ZDR limitations make adoption a regression for stdio MCPs and privacy.
- **Best-of-N verification** — multiplicative cost for marginal gain.
- **Heavy XML scaffolding on every prompt** — degrades conversational performance.
- **Auto-profile-suggester (Plan 3 of the active series)** — separate spec; this branch ships before it does so Plan 3 inherits the eval harness from day 1.

## Phase 1: Eval Harness

### Module layout

```
opencomputer/evals/                   ← code
├── __init__.py                       ← public: run, generate, regress
├── runner.py                         ← orchestration
├── sites.py                          ← central registry: name → callable + grader + schema
├── graders/
│   ├── exact.py                      ← exact_match grader
│   ├── schema.py                     ← JSON-schema match grader
│   └── rubric.py                     ← LLM-rubric grader (reason-then-discard)
├── generator.py                      ← LLM-driven case generation
├── baseline.py                       ← save/compare baseline accuracy
└── report.py                         ← terminal table output

opencomputer/cli_eval.py              ← oc eval subcommand

evals/                                ← data (committed, including candidates)
├── cases/
│   ├── reflect.jsonl
│   ├── reflect.candidates.jsonl
│   ├── prompt_evolution.jsonl
│   ├── llm_extractor.jsonl
│   ├── job_change.jsonl
│   └── instruction_detector.jsonl
├── baselines/
│   └── <site>.json                   ← {accuracy, f1, precision, recall, ts, model, provider, parse_failure_rate}
└── rubrics/
    ├── reflect_v1.md
    └── prompt_evolution_v1.md

tests/evals/
└── test_eval_smoke.py                ← CI: deterministic graders only, ~5 cases per site
```

### Site registry contract

```python
@dataclass(frozen=True)
class EvalSite:
    name: str
    callable_path: str               # "opencomputer.evolution.reflect:reflect"
    grader: Literal["exact", "schema", "rubric"]
    schema: dict | None = None       # for schema grader
    rubric_id: str | None = None     # for rubric grader
    requires_provider: bool = True
```

Sites declared centrally in `evals/sites.py`. **No imports from `opencomputer.evals` into core modules** (preserves directionality: evals → core, never core → evals).

### v1 site list (5)

| Site | Grader | Notes |
|---|---|---|
| `evolution/reflect.py` | rubric | Open-ended LLM judge |
| `evolution/prompt_evolution.py` | rubric | Self-modifying prompts; high blast radius |
| `profile_bootstrap/llm_extractor.py` | schema (subset) | Structured fact extraction |
| `awareness/life_events/job_change.py` | exact | Representative life-event detector |
| `security/instruction_detector.py` | exact | Confirmed regex-based; eval still measures regex accuracy |

The other 5 life-event detectors (burnout, exam_prep, health_event, relationship_shift, travel) are added post-v1 by appending registry entries.

### Three grader contracts

```python
class GradeResult:
    correct: bool
    score: float | None              # 0..1 for rubric, None for exact/schema
    reason: str | None               # rubric grader's discarded reasoning (debug only)
    parse_error: str | None          # JSON parse failure recorded as graded failure, not crash
```

- **ExactMatch** — `actual.strip().lower() == expected.lower()`. Free, deterministic.
- **SchemaMatch** — three modes: strict, subset (default), partial. Free, deterministic.
- **LLMRubric** — uses a different model than the site under test. Reason in `<thinking>`, decide in `<result>`. Reasoning discarded except for debug logs. **Critical constraint**: when the site uses Sonnet 4.6, grader uses Opus; when site uses Opus, grader uses Sonnet 4.6 sibling. If only non-Anthropic provider is configured, grader requires explicit `--grader-model` flag — fail loud.

### Test case workflow

`oc eval generate <site>` → calls Opus (different from default Sonnet 4.6) with a per-site generation prompt → writes candidates to `evals/cases/<site>.candidates.jsonl`. User reviews via PR diff or local edit, moves approved cases to `<site>.jsonl` (no fancy TUI). Candidates committed to git for portability.

### CI integration

`tests/evals/test_eval_smoke.py` runs deterministic graders only (`exact`, `schema`) on ~5 cases per site, on every PR. Free, fast (<30s). LLM-rubric sites are excluded from CI smoke; run manually via `oc eval run` or weekly via scheduled GitHub Action.

### Parse-failure handling

Wrap each call-site invocation in `try / except json.JSONDecodeError`. Record as `correct=False, parse_error=<msg>`. Aggregate report includes `parse_failure_rate` as its own metric. This is the metric Phase 2 will move to ~zero on Anthropic.

### Cost model

v1: 5 sites × 30 cases. Generation: ~5 × 30 × 1 LLM call = 150 calls. Per `oc eval run`: 60 (exact) + 30 (schema) + 120 (rubric: 30 × 2 sites × 2 calls) = 210 calls. Roughly $1–2 per full run on Sonnet 4.6 + Opus grader. CI smoke: ~25 calls (deterministic only), free.

## Phase 2: Structured Outputs Migration

### Scope

Three call sites move from raw `json.loads` to Anthropic provider's `output_config.format.json_schema`:

- `evolution/prompt_evolution.py:169`
- `evolution/reflect.py:146`
- `profile_bootstrap/llm_extractor.py:587`

### Provider work

Add `output_schema: dict | None` parameter to `BaseProvider.complete()` and `stream_complete()`. Anthropic provider routes it to the SDK's structured-outputs surface. Other providers ignore the parameter (graceful degradation — they continue parsing free-text JSON, with crash-resistant fallback added in this same phase).

### Crash-resistant fallback

Even on non-Anthropic providers, the three sites get a `try / except json.JSONDecodeError` wrapper that returns a typed "no decision" result instead of bubbling. The eval harness measures `parse_failure_rate` to verify this catches failures rather than masking them.

### Eval-driven measurement

Phase 1's harness records `parse_failure_rate` per site, per provider. The Phase 2 deliverable is "before/after" numbers committed alongside the change.

## Phase 3: Tool-Description Budget Audit

### Step 1: Measure

Add temporary logging in `tools/registry.py` to record, per turn:
- Number of tool descriptions sent
- Total token count of tool-description payload
- Cache-hit ratio for tool-description prefix

Run for one week of dogfood. Decide based on data:

- If cache-hit ratio on tools > 90% across normal usage: **no fix needed**. Document the finding. Phase 3 ends.
- If < 90%: implement selective `defer_loading` based on active skill / runtime context. Tools with `auto_load=False` only emit description when explicitly requested.

### Step 2: Selective defer_loading (only if Step 1 flags it)

Add `auto_load: bool = True` to `BaseTool` schema. ToolRegistry filters per turn based on:
- Active skill's declared tool needs (skills already declare this implicitly via instructions)
- Current `RuntimeContext.custom` flags (plan_mode, yolo_mode)
- Channel context (e.g., Telegram channel doesn't load Edit/MultiEdit unless coding-harness is active)

Tools with `auto_load=False` are described to the model as a category; the model can request specific tool details via a meta-tool similar to Anthropic's Tool Search Tool.

## Phase 4: Centralized LLM Observability

### Sink

Single module: `opencomputer/inference/observability.py`. Exports `record_llm_call(event)` where:

```python
@dataclass
class LLMCallEvent:
    ts: datetime
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int       # 0 if provider doesn't report cache stats
    cache_read_tokens: int           # 0 if provider doesn't report cache stats
    latency_ms: int
    cost_usd: float | None           # nullable: derived from per-model price table
    site: str | None                 # e.g., "agent_loop", "reflect", "llm_extractor"
```

Persists to `~/.opencomputer/<profile>/llm_events.jsonl` (append-only, gitignored, rotated at 100MB).

### Wiring points

- `agent/loop.py` already collects most of this — change call site to call `record_llm_call`
- Both providers' `complete()` and `stream_complete()` emit on response
- Eval harness emits on every grader call too (separate `site="eval_grader"`)

### Surface

`oc insights llm` (extends existing `cli_insights.py`):

```
Last 24h LLM activity:
  Calls: 423          Cost: $4.21          Avg latency: 850ms

  Provider          Calls    Tokens-in   Tokens-out   Cache-hit %   Cost
  anthropic         312      1.2M        180K         82%           $3.40
  openai            89       340K        45K          —             $0.61
  deepseek          22       80K         12K          —             $0.20

  Top sites by call count:
    agent_loop          312     $3.10
    reflect              45     $0.40
    eval_grader          30     $0.50
    llm_extractor        20     $0.15
    ...
```

## Coordination with Parallel Sessions

Four parallel Claude sessions detected at brainstorm time:

| Worktree branch | Inferred work |
|---|---|
| `feat/hermes-onboarding-polish` | Onboarding wizard polish (current main work) |
| `feat/opus-4-7-migration` | Model migration to Opus 4.7 |
| `feat/ollama-groq-providers` | New OpenAI-compat providers |
| `spec/tool-use-contract-tightening` | Tool-use contract changes |

Conflict assessment for `feat/quality-foundation`:

| Phase | Files touched | Conflict risk |
|---|---|---|
| 1 (evals) | New: `opencomputer/evals/`, `opencomputer/cli_eval.py`, `evals/`, `tests/evals/` | None |
| 2 (Structured Outputs) | `extensions/anthropic-provider/provider.py`, `evolution/prompt_evolution.py`, `evolution/reflect.py`, `profile_bootstrap/llm_extractor.py` | **Risk vs `feat/opus-4-7-migration`** if it touches anthropic-provider. Verify before Phase 2 starts. |
| 3 (tool budget) | Read-only audit, then optional `tools/registry.py` + `BaseTool` if defer_loading needed | Low |
| 4 (observability) | New: `opencomputer/inference/observability.py`. Wires into `agent/loop.py` + both provider `complete()` methods | **Risk vs `feat/opus-4-7-migration`** on provider touch. |

Mitigation: re-survey both `feat/opus-4-7-migration` and parallel branches before Phase 2 and Phase 4. If contended, pause the affected phase, let the other branch land, rebase, continue.

## Success Criteria

- **Phase 1**: 5 sites have `evals/cases/<site>.jsonl` files with ≥ 25 reviewed cases each. `oc eval run` produces per-site accuracy/F1/parse-failure-rate. CI smoke passes on every PR.
- **Phase 2**: `parse_failure_rate` for the 3 migrated sites drops to 0% on Anthropic provider, ≤ 2% on others (via fallback). Crash-resistant fallback covered by unit tests.
- **Phase 3**: Either documented finding "no fix needed; cache hits > 90%" OR selective defer_loading implemented and demonstrated to reduce per-turn token cost on a sample workload.
- **Phase 4**: `oc insights llm` shows last-24h activity. Cost table within 5% of provider invoice. JSONL log rotated correctly at 100MB.

## Followups (not in this branch)

- Auto-profile-suggester eval coverage — added to harness when Plan 3 lands
- Other 5 life-event detectors — append to registry, generate cases, review
- Output-side prompt-injection guard for tool-result injection — verify need first; defer until Phase 1 evals reveal the symptom or browser/MCP usage shows the risk
- Hot-path classifier model selection (Haiku 4.5) — Phase 4 observability will reveal frequency; decision deferred until data exists
- Persona system removal — handled by parallel session
- Scheduled weekly full-eval GitHub Action — added once manual `oc eval run` workflow is proven

## Notes

This spec follows the user's "actually useful" filter — every gap from doc analysis was triaged against codebase reality, and items that turned out to be already-done (prompt caching, Jinja2 templates), already-removed-elsewhere (personas), already-not-applicable (prefill audit), or speculative (Best-of-N) were explicitly skipped rather than padded into the plan.
