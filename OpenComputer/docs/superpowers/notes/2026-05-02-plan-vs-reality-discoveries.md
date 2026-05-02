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

## 2. `reflect.py` shim requires SessionDB integration, not a string fabricator

`ReflectionEngine.reflect(records: list[TrajectoryRecord]) -> list[Insight]` — not a `str -> str` function.

**Deeper discovery (2026-05-02 follow-up)**: `TrajectoryEvent.metadata` has a hard 200-char string limit enforced at construction — `__post_init__` raises `ValueError` on any string value longer than 200 chars to prevent raw prompt text from leaking into the evolution store. This means a "fabricator from `session_excerpt` string" violates the privacy contract by design. Records reference messages by `message_id` (FK into `agent_state.messages`), so a proper test fixture needs:

1. A fake (or real) `SessionDB` with messages inserted
2. `TrajectoryRecord`s pointing at those `message_id`s
3. A real provider configured to call `reflect()`

That's a substantial integration test, not a unit shim. Beyond v1 eval scope.

**Action**: keep the `reflect_for_eval` stub with `NotImplementedError` and a clear handoff message. The cleanest future path is a separate eval site whose case format is `{"records": list[dict]}` (full pre-built records) instead of `{"session_excerpt": str}`. That moves the fabricator out of production code and into eval fixtures.

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
