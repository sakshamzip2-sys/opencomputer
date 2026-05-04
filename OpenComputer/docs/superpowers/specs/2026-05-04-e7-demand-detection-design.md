# E7 — UserPromptSubmit Demand Detection — Design Spec + Plan

**Date:** 2026-05-04
**Status:** Combined spec+plan, draft
**Reference:** CLAUDE.md §5 Tier-4 "E7 — keyword-match demand detection on `UserPromptSubmit` hook"

---

## 1. Goal

Close E7. Two parts:
1. **Fire `USER_PROMPT_SUBMIT`** hook at the start of `run_conversation`. Today the event enum exists but no producer.
2. **Wire `PluginDemandTracker.scan_user_prompt(text)`** as a subscriber that keyword-matches the user's message against installed-but-disabled plugin manifest descriptions, recording demand signals when a match suggests a missing tool would help.

---

## 2. Verification (Karpathy "Think Before Coding")

Verified against actual source:
- `plugin_sdk.hooks.HookEvent.USER_PROMPT_SUBMIT = "UserPromptSubmit"` exists (line 50)
- `ALL_HOOK_EVENTS` includes it (line 177)
- **Zero firing sites in the codebase** (grep confirmed)
- `PluginDemandTracker` exists with `record_tool_not_found`, `recommended_plugins`, `signals_by_plugin`, `clear` — but no `scan_user_prompt`
- `BEFORE_PROMPT_BUILD` fires inside `run_conversation` line ~880 (cache-miss branch only) — NOT a good place for the user-prompt hook because cached-prompt sessions skip it
- The right fire site: top of `run_conversation`, just after `sid = session_id or str(uuid.uuid4())` (line ~664), so every user prompt fires it regardless of cache state

---

## 3. Implementation

### Task 1: Fire `USER_PROMPT_SUBMIT` at run_conversation entry

**Files:**
- Modify: `opencomputer/agent/loop.py` — fire after `sid` resolution
- Modify: `plugin_sdk/core.py` — `Message.text` content field already exists
- Test: `tests/test_user_prompt_submit_hook.py`

**Code (insertion point after line 664):**

```python
# E7 (2026-05-04) — USER_PROMPT_SUBMIT fires once per inbound user
# message so observers can scan natural-language intent. Subscribers
# include PluginDemandTracker.scan_user_prompt for missing-tool demand
# recording. Fire-and-forget — does NOT block the loop.
try:
    from opencomputer.hooks.engine import engine as _hook_engine_ups
    from plugin_sdk.core import Message as _MessagePUB
    from plugin_sdk.hooks import HookContext as _HookContextUPS
    from plugin_sdk.hooks import HookEvent as _HookEventUPS

    _hook_engine_ups.fire_and_forget(
        _HookContextUPS(
            event=_HookEventUPS.USER_PROMPT_SUBMIT,
            session_id=sid,
            message=_MessagePUB(role="user", content=user_message),
            runtime=self._runtime,
        )
    )
except Exception as _exc:  # noqa: BLE001 — never crash the loop on hook init
    logger = logging.getLogger("opencomputer.agent.loop")
    logger.debug("USER_PROMPT_SUBMIT fire failed: %s", _exc)
```

### Task 2: Add `PluginDemandTracker.scan_user_prompt`

**Files:**
- Modify: `opencomputer/plugins/demand_tracker.py` — add method + table column for source kind
- Test: `tests/test_demand_tracker_user_prompt_scan.py`

**Logic:**
- Iterate disabled plugin candidates (those discovered but not in active registry)
- For each plugin: extract searchable terms from `manifest.description` + `manifest.id` + tool names
- Tokenize the user's prompt (lowercase, alphanumeric word splits)
- Match: at least 2 manifest-derived terms appear in the prompt → record demand signal with `kind="user_prompt_keyword"` (vs existing `kind="tool_not_found"`)

**Why "at least 2"?** Single-word matches generate too much noise (e.g., "github" mentioned anywhere fires github plugin demand). Two co-occurring terms (e.g., "github" + "issue") is a much stronger signal.

**Code shape:**

```python
def scan_user_prompt(
    self,
    text: str,
    *,
    session_id: str = "",
    turn_index: int = 0,
    min_matches: int = 2,
) -> list[str]:
    """Scan a user prompt for keyword matches against disabled plugins.

    Returns the list of plugin_ids that triggered a demand signal so
    callers can log/notify. Recording is best-effort; exceptions are
    swallowed so a tracker failure can never wedge the loop.
    """
    candidates = self._discover_fn()
    enabled_ids = self._enabled_ids_fn() if self._enabled_ids_fn else set()
    tokens = _tokenize(text.lower())
    triggered: list[str] = []

    for cand in candidates:
        if cand.manifest.id in enabled_ids:
            continue
        terms = _extract_terms(cand)
        hits = sum(1 for term in terms if term in tokens)
        if hits >= min_matches:
            try:
                self._record_keyword_signal(
                    plugin_id=cand.manifest.id,
                    session_id=session_id,
                    turn_index=turn_index,
                    matched_terms=tuple(t for t in terms if t in tokens),
                )
                triggered.append(cand.manifest.id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "demand_tracker keyword signal record failed for %s",
                    cand.manifest.id,
                )
    return triggered
```

Plus helpers:
```python
_WORD_RE = re.compile(r"[a-z0-9]+")

def _tokenize(text: str) -> set[str]:
    """Lowercase + word split; returns a set for O(1) membership."""
    return set(_WORD_RE.findall(text.lower()))

def _extract_terms(cand: PluginCandidate) -> set[str]:
    """Search-relevant terms from a plugin candidate."""
    terms: set[str] = set()
    terms.update(_tokenize(cand.manifest.id))
    terms.update(_tokenize(cand.manifest.description or ""))
    # Remove generic stopwords that would over-match
    return terms - _STOPWORDS
```

DB schema needs `kind` column. Migration: ALTER TABLE plugin_demand ADD COLUMN kind TEXT DEFAULT 'tool_not_found'.

### Task 3: Wire scan_user_prompt as USER_PROMPT_SUBMIT subscriber

This is the hook registration. Done at AgentLoop init time (or earlier — wherever the demand tracker is constructed). Subscriber callback ignores ctx.tool_call/tool_result; uses ctx.message.content.

---

## 4. Testing

| Item | Test approach |
|---|---|
| T1 hook fires | Register a tracker callback. Call run_conversation with a fake handler. Assert callback received HookEvent.USER_PROMPT_SUBMIT with the user's message text. |
| T2 keyword match | Create PluginDemandTracker with mock candidates (e.g., a github plugin with description "GitHub repos issues PRs"). Call scan_user_prompt("how do I close a github issue") → assert "github" returned in triggered list. |
| T2 single-word doesn't trigger | scan_user_prompt("github") with min_matches=2 → returns []. |
| T2 enabled plugins skipped | If "github" is in enabled_ids, no signal fires. |
| T2 stopwords filtered | scan_user_prompt("a the and") → no matches. |
| T2 graceful failure | Underlying DB write raises → method continues, returns [] without raising. |

---

## 5. Out of scope

- Demand-driven plugin auto-suggestion to user — recording is sufficient for v1; UI/CLI suggestion ("you might want to enable plugin X") is follow-up.
- LLM-based intent detection — keyword match is good enough; LLM call per turn would be costly.
- Configurable stopword list — hardcoded for now.

---

## 6. Self-audit (executed before showing this design)

- **Risk: Hook fire raises if `Message` constructor signature changed.** Counter: existing message.content shape is `str | list[dict]` (multimodal); using `content=user_message` (string) is the canonical path used elsewhere. Fire wrapped in try/except to never crash the loop.
- **Risk: scan_user_prompt is called per turn, could be slow.** Counter: candidates list is read once and cached by `discover_fn`; tokenize is O(N) on prompt text; matching is O(plugins × terms). For ~10 plugins × ~20 terms each, the inner loop is fast (<1ms).
- **Risk: false-positive demand signals create noise.** Counter: min_matches=2 + stopword filter + only-disabled-plugins gives strong signal. CLI surface lets users see + clear signals.
- **Risk: DB migration breaks existing rows.** Counter: ALTER TABLE ADD COLUMN with DEFAULT — non-destructive, backwards-compatible. Existing rows get default kind='tool_not_found'.
- **Risk: stopwords list is too aggressive (filters real terms).** Counter: stopwords are common English filler words ("the", "and", "a", "is", "of", "to", etc.) — none overlap with plugin id keywords.

### Edge cases
1. **Empty prompt** → `_tokenize("")` returns `set()` → no matches. Safe.
2. **Prompt with only stopwords** → empty token set after filter → no matches. Safe.
3. **Plugin with empty description** → `_extract_terms` returns just id tokens; works.
4. **Discovery throws** → discover_fn caller wraps in try/except; scan_user_prompt returns [] gracefully.

### Defensible? Yes.

2 commits, ≤200 LOC, ~2-3h estimate. Closes E7.
