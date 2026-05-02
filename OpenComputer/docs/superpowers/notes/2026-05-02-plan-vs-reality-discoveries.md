# Plan vs Reality — Discoveries during Task 1.6 Execution

**Date:** 2026-05-02
**Branch:** feat/quality-foundation

While executing Task 1.6 (production-side eval shims), reading the actual source files revealed three places where the plan was based on incorrect assumptions:

## 1. `prompt_evolution.py` is NOT LLM-driven

The plan listed it as a v1 site with rubric grading. Reading the code reveals:

```python
class PromptEvolver:
    """Persists Insight->proposal; reads/updates proposal status. Pure persistence,
    no LLM calls, no prompt mutations.
    """
```

The `json.loads(raw)` at line 169 is in `_insight_from_json`, which parses **persisted DB proposal records**, not LLM output. Wrong eval target.

**Action:** dropped from v1 site list. Replacement candidates (e.g., one of the unread life-event detectors with LLM logic) deferred to a future session.

## 2. `reflect.py` shim requires constructing TrajectoryRecord objects

`ReflectionEngine.reflect(records: list[TrajectoryRecord]) -> list[Insight]` — not a `str -> str` function. A real `reflect_for_eval` needs to fabricate at least one `TrajectoryRecord`, which requires understanding that dataclass + the `_cache_key`/Jinja2 template wiring.

**Action:** deferred to scheduled agent. Adapter import-resolves (so test_callable_paths_resolve passes) but calling it raises NotImplementedError with a clear message.

## 3. `job_change.py` is regex-only, not LLM-driven, AND takes URL+title not free text

```python
def consider_event(self, event_type, metadata):
    if event_type != "browser_visit":
        return None
    url = str(metadata.get("url", "")).lower()
    title = str(metadata.get("title", "")).lower()
    ...
```

Adapter input shape changed from `{"context": str}` to `{"url": str, "title": str}`. Generation prompt updated accordingly.

The eval still measures real signal: "given a (url, title) pair, does the regex correctly classify as job-related?"

## Effective v1 site list (after discoveries)

| Site | Grader | Status |
|---|---|---|
| `instruction_detector` | exact | ✅ shim works (existing detect()) |
| `llm_extractor` | schema | ✅ shim added (extract_for_eval) |
| `job_change` | exact | ✅ shim added (detect_for_eval), input reshaped |
| `reflect` | rubric | ⚠️ deferred — needs TrajectoryRecord construction |
| `prompt_evolution` | rubric | ❌ removed — not LLM-driven |

3 sites are now genuinely runnable (mocked or with real instances). 1 remains deferred for the scheduled agent.

## Process lesson

Plan-time grep on `json.loads` is a fast way to find candidate sites but produces false positives. Reading each candidate's surrounding context (10–20 lines) before committing to a site list would have caught these earlier. Recording this as feedback memory.
