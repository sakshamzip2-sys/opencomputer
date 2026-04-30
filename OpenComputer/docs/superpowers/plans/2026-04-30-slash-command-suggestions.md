# Slash-Command Suggestions Implementation Plan

> **For agentic workers:** This is an extension of the existing Passive Education / Learning Moments system (PRs #213, #218, #219). It adds 13 new contextual moments that suggest slash commands when the user's behavior implies they could benefit. Reuses the engine, surfaces (INLINE_TAIL / SYSTEM_PROMPT / SESSION_END), caps, dedup, and opt-out. No new mechanism.

**Goal:** Make slash commands discoverable through behavior-triggered suggestions during normal conversation, without nagging.

**Architecture:** 5 new optional fields on `Context` (BC-safe defaults) → 13 new pure-function predicates → 13 new `LearningMoment` entries in registry → wire field population at the 3 existing call sites in `agent/loop.py`. Total expected diff: ~350 LOC code + ~250 LOC tests.

**Tech Stack:** Python 3.12+ stdlib (re, dataclasses). No new deps.

**Anti-pattern guards (per existing system spec):**
- Default silent — only fire when predicate clearly matches
- Per-moment dedup → user sees each suggestion at most once, ever
- Daily cap (1) + weekly cap (3) shared across all surfaces
- `oc memory learning-off` suppresses tip-severity suggestions
- Returning-user seed prevents noise burst on upgrade

---

## File Structure

| File | Action |
|------|--------|
| `opencomputer/awareness/learning_moments/registry.py` | Modify — add 5 Context fields + 13 LearningMoment entries |
| `opencomputer/awareness/learning_moments/predicates.py` | Modify — add 13 new predicate functions |
| `opencomputer/agent/loop.py` | Modify — wire 5 new fields at 3 call sites (mech A: ~1297, mech B: ~1851, mech C: ~2157) |
| `tests/test_learning_moments.py` | Modify — add tests for new predicates + integration |

No new modules. No new public exports beyond what flows through Context.

---

### Task 1: Extend Context dataclass

**Files:**
- Modify: `opencomputer/awareness/learning_moments/registry.py`

- [ ] **Step 1: Add 5 optional fields to Context after `turn_count`**

```python
# v3 fields (2026-04-30) — slash-command suggestion support.
# All optional with safe defaults so v1/v2 callers still work.

permission_mode_str: str = ""
"""Current permission mode as upper-case string ("DEFAULT", "AUTO",
"PLAN", "ACCEPT_EDITS"). Used by suggest_auto_mode_for_long_task to
avoid suggesting /auto when already in AUTO."""

recent_edit_count_this_turn: int = 0
"""How many file-mutating tool calls (Edit/MultiEdit/Write) ran in
the most recent agent turn. Used by /undo and /diff suggestions."""

checkpoint_count_session: int = 0
"""How many checkpoints exist for this session. Zero means
suggest_checkpoint_before_rewrite is eligible."""

session_token_total: int = 0
"""Cumulative input + output tokens for this session. Used by
suggest_usage_at_token_milestone (fires at >100k)."""

has_openai_key: bool = False
"""Whether OPENAI_API_KEY is present in the environment. Gates
suggest_voice_for_voice_user (the voice realtime feature requires
an OpenAI key)."""
```

- [ ] **Step 2: Run existing tests to confirm BC**

```bash
cd OpenComputer && pytest tests/test_learning_moments.py -x -q 2>&1 | tail -20
```
Expected: all existing tests still pass (defaults preserve old behavior).

- [ ] **Step 3: Commit**

```bash
git add opencomputer/awareness/learning_moments/registry.py
git commit -m "feat(learning-moments): add 5 v3 Context fields for slash-command suggestions"
```

---

### Task 2: Add 9 INLINE_TAIL predicates

**Files:**
- Modify: `opencomputer/awareness/learning_moments/predicates.py`

- [ ] **Step 1: Add module-level regex constants below existing `_PATH_RE`**

```python
# Multi-step / planning keywords for /plan suggestion.
_MULTISTEP_KEYWORDS = re.compile(
    r"\b(?:step[ -]by[ -]step|first.+then|plan(?:ning)?|"
    r"phases?|milestones?|breakdown|approach|outline)\b",
    re.IGNORECASE,
)

# "Build / create / implement" verbs that indicate a long task.
_LONG_TASK_VERBS = re.compile(
    r"\b(?:build|create|implement|develop|design|set\s*up|"
    r"refactor|migrate|integrate|wire|scaffold)\b",
    re.IGNORECASE,
)

# "Rewrite / refactor / redo" — risky-edit keywords.
_REWRITE_KEYWORDS = re.compile(
    r"\b(?:rewrite|refactor|redo|overhaul|restructure|reorganize|"
    r"clean\s*up|tear\s*out)\b",
    re.IGNORECASE,
)

# Frustration / undo signals.
_UNDO_KEYWORDS = re.compile(
    r"\b(?:revert|undo|go\s*back|that's\s*wrong|didn't\s*want|"
    r"not\s*what|wasn't\s*supposed|broke|broken)\b",
    re.IGNORECASE,
)

# "What changed / show me" lookback signals.
_LOOKBACK_KEYWORDS = re.compile(
    r"\b(?:what\s*changed|what\s*did\s*you|show\s*me\s*the\s*diff|"
    r"earlier|before|previously|what\s*did\s*we)\b",
    re.IGNORECASE,
)

# "By the way" aside detector.
_BTW_KEYWORDS = re.compile(
    r"\b(?:by\s*the\s*way|btw|side\s*note|aside|"
    r"on\s*another\s*note|also\s*remember|fyi)\b",
    re.IGNORECASE,
)

# URL detector.
_URL_RE = re.compile(
    r"https?://[^\s<>\"]+",
    re.IGNORECASE,
)
```

- [ ] **Step 2: Add 9 predicate functions**

```python
def suggest_plan_for_complex_task(ctx: Context) -> bool:
    """Long multi-step request submitted outside PLAN mode."""
    if not ctx.user_message or len(ctx.user_message) < 200:
        return False
    if ctx.permission_mode_str == "PLAN":
        return False
    return bool(_MULTISTEP_KEYWORDS.search(ctx.user_message))


def suggest_auto_mode_for_long_task(ctx: Context) -> bool:
    """Build/create-style request in DEFAULT mode (not AUTO yet)."""
    if not ctx.user_message or len(ctx.user_message) < 60:
        return False
    if ctx.permission_mode_str in ("AUTO", "PLAN", "ACCEPT_EDITS"):
        return False
    return bool(_LONG_TASK_VERBS.search(ctx.user_message))


def suggest_checkpoint_before_rewrite(ctx: Context) -> bool:
    """Rewrite-style request when no checkpoint exists yet."""
    if ctx.checkpoint_count_session > 0:
        return False
    if not ctx.user_message:
        return False
    return bool(_REWRITE_KEYWORDS.search(ctx.user_message))


def suggest_undo_after_unwanted_edits(ctx: Context) -> bool:
    """User signals dissatisfaction right after multiple edits."""
    if ctx.recent_edit_count_this_turn < 3:
        return False
    if not ctx.user_message:
        return False
    return bool(_UNDO_KEYWORDS.search(ctx.user_message))


def suggest_diff_for_silent_edits(ctx: Context) -> bool:
    """User asks 'what changed' after silent edits."""
    if ctx.recent_edit_count_this_turn < 2:
        return False
    if not ctx.user_message:
        return False
    return bool(_LOOKBACK_KEYWORDS.search(ctx.user_message))


def suggest_usage_at_token_milestone(ctx: Context) -> bool:
    """Cumulative session tokens crossed 100k."""
    return ctx.session_token_total >= 100_000


def suggest_history_for_lookback(ctx: Context) -> bool:
    """User asks about earlier turns."""
    if not ctx.user_message or len(ctx.user_message) > 600:
        return False
    return bool(_LOOKBACK_KEYWORDS.search(ctx.user_message))


def suggest_btw_for_aside(ctx: Context) -> bool:
    """Message contains an aside marker."""
    if not ctx.user_message or len(ctx.user_message) < 30:
        return False
    return bool(_BTW_KEYWORDS.search(ctx.user_message))


def suggest_scrape_for_url(ctx: Context) -> bool:
    """User pasted a URL without explicit fetch verb."""
    if not ctx.user_message or len(ctx.user_message) > 5000:
        return False
    if not _URL_RE.search(ctx.user_message):
        return False
    # Don't fire if message already mentions scraping/fetching.
    if re.search(
        r"\b(?:scrape|fetch|read\s*this|pull|grab|download)\b",
        ctx.user_message, re.IGNORECASE,
    ):
        return False
    return True
```

- [ ] **Step 3: Run partial test (predicates standalone)**

```bash
cd OpenComputer && python -c "
from opencomputer.awareness.learning_moments.predicates import (
    suggest_plan_for_complex_task,
    suggest_auto_mode_for_long_task,
    suggest_checkpoint_before_rewrite,
    suggest_undo_after_unwanted_edits,
    suggest_diff_for_silent_edits,
    suggest_usage_at_token_milestone,
    suggest_history_for_lookback,
    suggest_btw_for_aside,
    suggest_scrape_for_url,
)
print('all imports OK')
"
```
Expected: `all imports OK`.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/awareness/learning_moments/predicates.py
git commit -m "feat(learning-moments): 9 predicates for /plan /auto /checkpoint /undo /diff /usage /history /btw /scrape suggestions"
```

---

### Task 3: Add 4 mechanism-B/C predicates

**Files:**
- Modify: `opencomputer/awareness/learning_moments/predicates.py`

- [ ] **Step 1: Add 3 system-prompt + 1 session-end predicates**

```python
# ─── v3 mechanism B / C predicates ────────────────────────────────────


_VOICE_KEYWORDS = re.compile(
    r"\b(?:speak|talking|voice|out\s*loud|say\s*it|"
    r"narrate|read\s*aloud)\b",
    re.IGNORECASE,
)

_EMOTION_ANCHORS = re.compile(
    r"\b(?:lonely|rough\s*day|hard\s*day|burnt?\s*out|"
    r"feeling\s*(?:down|low|stuck|tired|overwhelmed)|"
    r"can't\s*focus|exhausted)\b",
    re.IGNORECASE,
)


def suggest_voice_for_voice_user(ctx: Context) -> bool:
    """User mentions voice/talk/speak AND has an OpenAI key."""
    if not ctx.has_openai_key or not ctx.user_message:
        return False
    return bool(_VOICE_KEYWORDS.search(ctx.user_message))


def suggest_personality_after_friction(ctx: Context) -> bool:
    """Vibe has gone non-calm 3+ times in this session."""
    return ctx.vibe_log_session_count_noncalm >= 3


def suggest_persona_for_companion_signals(ctx: Context) -> bool:
    """User shows emotional / companion-need signals."""
    if not ctx.user_message:
        return False
    return bool(_EMOTION_ANCHORS.search(ctx.user_message))


def suggest_skill_save_after_long_session(ctx: Context) -> bool:
    """Long, productive session — could be a saved skill."""
    return ctx.turn_count >= 20
```

- [ ] **Step 2: Commit**

```bash
git add opencomputer/awareness/learning_moments/predicates.py
git commit -m "feat(learning-moments): 4 mechanism-B/C predicates for voice/personality/persona/skill-save"
```

---

### Task 4: Register all 13 moments in registry.py

**Files:**
- Modify: `opencomputer/awareness/learning_moments/registry.py`

- [ ] **Step 1: Update import block in `all_moments()` with all 13 new predicates**

```python
def all_moments() -> tuple[LearningMoment, ...]:
    """Return the v1 + v2 + v3 registry. Stable ordering for tests."""
    from opencomputer.awareness.learning_moments.predicates import (
        # v1
        memory_continuity_first_recall,
        recent_files_paste,
        vibe_first_nonneutral,
        # v2
        confused_session,
        cross_session_recall,
        user_md_unfilled,
        # v3 (2026-04-30) — slash-command suggestions
        suggest_auto_mode_for_long_task,
        suggest_btw_for_aside,
        suggest_checkpoint_before_rewrite,
        suggest_diff_for_silent_edits,
        suggest_history_for_lookback,
        suggest_persona_for_companion_signals,
        suggest_personality_after_friction,
        suggest_plan_for_complex_task,
        suggest_scrape_for_url,
        suggest_skill_save_after_long_session,
        suggest_undo_after_unwanted_edits,
        suggest_usage_at_token_milestone,
        suggest_voice_for_voice_user,
    )
```

- [ ] **Step 2: Append all 13 LearningMoment entries to the return tuple**

(See full code in registry.py — append within the existing tuple, BEFORE the closing `)`. Priority numbers: 70-200 range, kept above existing 10-60 priorities.)

```python
        # ── v3: slash-command suggestion moments (2026-04-30) ─────────
        LearningMoment(
            id="suggest_plan_for_complex_task",
            predicate=suggest_plan_for_complex_task,
            reveal=(
                "(Heads up — for multi-step work like this, `/plan` lets you "
                "review the approach before I touch any code.)"
            ),
            priority=70,
        ),
        LearningMoment(
            id="suggest_auto_mode_for_long_task",
            predicate=suggest_auto_mode_for_long_task,
            reveal=(
                "(If you don't want to keep approving each step, `/auto` "
                "(or Shift+Tab Shift+Tab) runs with fewer interruptions.)"
            ),
            priority=80,
        ),
        LearningMoment(
            id="suggest_checkpoint_before_rewrite",
            predicate=suggest_checkpoint_before_rewrite,
            reveal=(
                "(`/checkpoint` saves state before I rewrite — easy "
                "rollback via `/rollback` if it goes sideways.)"
            ),
            priority=90,
        ),
        LearningMoment(
            id="suggest_undo_after_unwanted_edits",
            predicate=suggest_undo_after_unwanted_edits,
            reveal=(
                "(`/undo` reverts my most recent edit; `/rollback` resets "
                "to the last checkpoint.)"
            ),
            priority=100,
        ),
        LearningMoment(
            id="suggest_diff_for_silent_edits",
            predicate=suggest_diff_for_silent_edits,
            reveal=(
                "(`/diff` shows a clean diff of what I just edited — "
                "useful when edits stream past quickly.)"
            ),
            priority=110,
        ),
        LearningMoment(
            id="suggest_usage_at_token_milestone",
            predicate=suggest_usage_at_token_milestone,
            reveal=(
                "(This session has used over 100k tokens — `/usage` "
                "shows cost so far.)"
            ),
            priority=120,
        ),
        LearningMoment(
            id="suggest_history_for_lookback",
            predicate=suggest_history_for_lookback,
            reveal=(
                "(`/history` lists every turn in this session — easier "
                "than scrolling.)"
            ),
            priority=130,
        ),
        LearningMoment(
            id="suggest_btw_for_aside",
            predicate=suggest_btw_for_aside,
            reveal=(
                "(For asides like that, `/btw <note>` saves it to "
                "memory without breaking the current task.)"
            ),
            priority=140,
        ),
        LearningMoment(
            id="suggest_scrape_for_url",
            predicate=suggest_scrape_for_url,
            reveal=(
                "(If you want me to read that URL, `/scrape <url>` "
                "pulls it as text.)"
            ),
            priority=150,
        ),
        # ── v3 mechanism B (system-prompt overlays) ───────────────────
        LearningMoment(
            id="suggest_voice_for_voice_user",
            predicate=suggest_voice_for_voice_user,
            reveal=(
                "Context anchor: user mentioned voice/speech and has an "
                "OpenAI API key. If natural and useful, you may mention "
                "`oc voice realtime` for two-way streaming voice. Don't "
                "force the suggestion."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=160,
        ),
        LearningMoment(
            id="suggest_personality_after_friction",
            predicate=suggest_personality_after_friction,
            reveal=(
                "Context anchor: the user has shown frustration / "
                "non-calm vibes multiple times this session. If natural, "
                "you may suggest `/personality` to switch tone or "
                "`/clear` to reset context. Don't be patronizing — just "
                "offer if it fits."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=170,
        ),
        LearningMoment(
            id="suggest_persona_for_companion_signals",
            predicate=suggest_persona_for_companion_signals,
            reveal=(
                "Context anchor: user is showing emotional / companion "
                "signals. Respond with care first. If it fits naturally "
                "later, you may mention `/persona-mode auto` (I adapt "
                "tone) or `/personality` (manual). Never force."
            ),
            surface=Surface.SYSTEM_PROMPT,
            priority=180,
        ),
        # ── v3 mechanism C (session-end reflection) ──────────────────
        LearningMoment(
            id="suggest_skill_save_after_long_session",
            predicate=suggest_skill_save_after_long_session,
            reveal=(
                "(That was a long session. If it's a workflow you'll "
                "repeat, `oc skills new` captures the pattern as a "
                "reusable skill — same agent, less re-explaining.)"
            ),
            surface=Surface.SESSION_END,
            priority=190,
        ),
```

- [ ] **Step 3: Run existing tests to confirm registry still loads**

```bash
cd OpenComputer && python -c "
from opencomputer.awareness.learning_moments.registry import all_moments
moments = all_moments()
print(f'{len(moments)} moments registered')
ids = [m.id for m in moments]
assert len(set(ids)) == len(ids), 'duplicate ids'
print('all unique')
"
```
Expected: `19 moments registered` + `all unique`.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/awareness/learning_moments/registry.py
git commit -m "feat(learning-moments): register 13 v3 slash-command-suggestion moments"
```

---

### Task 5: Wire Mechanism A call site in loop.py

**Files:**
- Modify: `opencomputer/agent/loop.py:1297-1320` (the `_build_lm_ctx` closure)

- [ ] **Step 1: Add new field computations BEFORE the closure**

Insert after `_days_since_first` computation (~line 1291), before the `def _build_lm_ctx` line:

```python
# v3 fields: permission mode, edit count, checkpoints, tokens, openai key
import os as _os
from plugin_sdk import effective_permission_mode as _eff_mode
try:
    _perm_mode = _eff_mode(runtime).value.upper() if runtime else "DEFAULT"
except Exception:  # noqa: BLE001
    _perm_mode = "DEFAULT"

# Count Edit/MultiEdit/Write tool calls in the most recent step.
_edit_tool_names = {"Edit", "MultiEdit", "Write"}
_recent_edit_count = sum(
    1 for tc in (step.tool_calls or [])
    if tc.name in _edit_tool_names
)

# Checkpoint count for this session — degrades to 0 if DB lacks the table.
try:
    _checkpoint_count = self.db.count_checkpoints_for_session(sid)
except (AttributeError, Exception):  # noqa: BLE001
    _checkpoint_count = 0

# Cumulative session tokens (input + output).
try:
    _tokens_total = self.db.session_token_total(sid)
except (AttributeError, Exception):  # noqa: BLE001
    _tokens_total = 0

_has_openai = bool(_os.environ.get("OPENAI_API_KEY"))
```

- [ ] **Step 2: Extend `_build_lm_ctx` with new defaults + Context kwargs**

```python
def _build_lm_ctx(
    _ph_=_ph,
    _mem_text_=_mem_text,
    _vibe_rows_=_vibe_rows,
    _total_sessions_=_total_sessions,
    _sid_=sid,
    _user_msg_=user_message or "",
    _user_md_=_user_md,
    _days_=_days_since_first,
    _perm_=_perm_mode,
    _edit_count_=_recent_edit_count,
    _ckpt_=_checkpoint_count,
    _tokens_=_tokens_total,
    _has_openai_=_has_openai,
) -> _LMCtx:
    return _LMCtx(
        session_id=_sid_,
        profile_home=_ph_,
        user_message=_user_msg_,
        memory_md_text=_mem_text_,
        vibe_log_session_count_total=len(_vibe_rows_),
        vibe_log_session_count_noncalm=sum(
            1 for r in _vibe_rows_
            if r.get("vibe") != "calm"
        ),
        sessions_db_total_sessions=_total_sessions_,
        user_md_text=_user_md_,
        days_since_first_session=_days_,
        permission_mode_str=_perm_,
        recent_edit_count_this_turn=_edit_count_,
        checkpoint_count_session=_ckpt_,
        session_token_total=_tokens_,
        has_openai_key=_has_openai_,
    )
```

- [ ] **Step 3: Same changes for Mechanism B at ~1851 and Mechanism C at ~2157**

(For Mechanism B, the closure is similar — apply the same field-population pattern. For Mechanism C, `recent_edit_count_this_turn` is N/A — pass 0; `permission_mode_str` should still come from runtime if available.)

- [ ] **Step 4: Verify `db.count_checkpoints_for_session` and `db.session_token_total` exist or stub gracefully**

```bash
cd OpenComputer && grep -n "count_checkpoints_for_session\|session_token_total" opencomputer/agent/state.py 2>/dev/null
```

If absent, the try/except in step 1 returns 0. Confirm degradation path is OK by running tests.

- [ ] **Step 5: Run full learning_moments tests**

```bash
cd OpenComputer && pytest tests/test_learning_moments.py -x -q
```
Expected: all existing tests pass; new fields are passed through (the existing tests don't check new fields, so they ignore them).

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/loop.py
git commit -m "feat(learning-moments): wire 5 v3 Context fields at 3 loop call sites"
```

---

### Task 6: Add unit tests for new predicates

**Files:**
- Modify: `tests/test_learning_moments.py`

- [ ] **Step 1: Add a positive + negative test for each of the 13 predicates**

(One block per predicate — inline test code in plan.) See concrete examples for `suggest_plan_for_complex_task`:

```python
def test_suggest_plan_for_complex_task_fires_on_long_multistep():
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_plan_for_complex_task,
    )
    msg = (
        "Let's build the new auth flow step by step — first the login "
        "page, then the session middleware, then the password reset "
        "flow, and finally the email verification. Plan it out first."
    )
    ctx = _ctx_with(user_message=msg, permission_mode_str="DEFAULT")
    assert suggest_plan_for_complex_task(ctx) is True


def test_suggest_plan_for_complex_task_silent_in_plan_mode():
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_plan_for_complex_task,
    )
    msg = "Let's plan this step by step across three phases of work."
    ctx = _ctx_with(user_message=msg * 5, permission_mode_str="PLAN")
    assert suggest_plan_for_complex_task(ctx) is False
```

A small `_ctx_with(**overrides)` helper at the top of the test file builds a Context with sensible defaults so each test is one line of setup.

- [ ] **Step 2: Add an integration test that exercises a v3 moment end-to-end**

```python
def test_v3_suggest_plan_fires_via_select_reveal(tmp_path, monkeypatch):
    from opencomputer.awareness.learning_moments import select_reveal
    long_msg = (
        "Let me build this step by step: first refactor the schema, "
        "then migrate data, then update the API, then redo the UI."
    )
    ctx = _ctx_with(
        profile_home=tmp_path,
        user_message=long_msg * 3,
        permission_mode_str="DEFAULT",
    )
    out = select_reveal(ctx_builder=lambda: ctx, profile_home=tmp_path)
    assert out is not None
    assert "/plan" in out
```

- [ ] **Step 3: Run the new tests**

```bash
cd OpenComputer && pytest tests/test_learning_moments.py -x -q -k "v3 or suggest_"
```
Expected: 26+ new tests, all pass.

- [ ] **Step 4: Run the full learning_moments test file (regression check)**

```bash
cd OpenComputer && pytest tests/test_learning_moments.py -x -q
```
Expected: all v1/v2/v3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_learning_moments.py
git commit -m "test(learning-moments): unit tests for 13 v3 slash-command-suggestion predicates"
```

---

### Task 7: Run full pytest + ruff + audit

- [ ] **Step 1: Full pytest**

```bash
cd OpenComputer && pytest tests/ -x -q --no-header 2>&1 | tail -40
```
Expected: all tests pass.

- [ ] **Step 2: Ruff**

```bash
cd OpenComputer && ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```
Expected: no errors.

- [ ] **Step 3: Audit subagent**

Dispatch a code-reviewer agent against the diff (last commit range from main..HEAD). Look for: false-positive predicate triggers, missing tests, dead code paths, BC issues with old Context callers.

- [ ] **Step 4: Address any audit findings + commit fixes**

---

### Task 8: Update memory + push

- [ ] **Step 1: Update memory file**

Save `/Users/saksham/.claude/projects/-Users-saksham-Vscode-claude/memory/project_slash_command_suggestions.md` describing the feature + PR.

- [ ] **Step 2: Push branch + open PR**

```bash
cd OpenComputer && git push -u origin feat/learning-moments-slash-suggestions
gh pr create --title "feat(learning-moments): 13 slash-command suggestions (v3)" --body "..."
```
