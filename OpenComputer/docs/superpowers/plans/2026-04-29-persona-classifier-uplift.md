# Persona Classifier Uplift — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the persona auto-classifier (1) correct on multi-line / non-English / emotion-leading inputs, (2) adaptive within a session via per-turn re-classification with a stability gate, and (3) user-overridable via a `/persona-mode <id>` slash command — without breaking the existing prefix-cache invariant or the never-break-startup contract.

**Architecture:** Three additive changes, no refactors.
1. Bug fixes inside `opencomputer/awareness/personas/classifier.py` — per-line state-query check, all-3-messages scan, Hindi/Hinglish patterns, emotion-lexicon classification rule.
2. New `_maybe_reclassify_persona(session_id)` on `AgentLoop` — per-turn re-classification with a 2-consecutive-match stability gate (or confidence ≥ 0.85 short-circuit). Evicts the session's prompt snapshot on a confirmed flip; the next turn rebuilds with the new overlay. Override-locked when `runtime.custom["persona_id_override"]` is set.
3. New `/persona-mode` slash command at `opencomputer/agent/slash_commands_impl/persona_mode_cmd.py`, registered alongside the existing built-ins.

**Tech Stack:** Python 3.12+, `re`, existing pytest setup, existing slash-command infrastructure (`SlashCommand` ABC + `_BUILTIN_COMMANDS` tuple), existing `RegexClassifier` is *not* used here — the persona classifier's compound logic stays as-is per the prior plan's deliberate exclusion.

---

## File Structure

**Create:**
- `opencomputer/agent/slash_commands_impl/persona_mode_cmd.py` — `PersonaModeCommand` slash command (list / set / auto)
- `tests/test_persona_mode_command.py` — slash command unit tests

**Modify:**
- `opencomputer/awareness/personas/classifier.py` — per-line + all-3-messages state-query, Hindi patterns, emotion-lexicon rule
- `opencomputer/agent/loop.py` — read `persona_id_override` in `_build_persona_overlay`; new `_maybe_reclassify_persona`; new `_cached_foreground_app`; call re-classification on user-turn boundary; new state vars `_pending_persona_id`, `_pending_persona_count`, `_foreground_app_cache`, `_foreground_app_cache_at`
- `opencomputer/agent/slash_commands.py` — import + register `PersonaModeCommand` in `_BUILTIN_COMMANDS`
- `tests/test_persona_classifier.py` — extend with multi-line, Hindi, emotion, all-3-messages tests
- `tests/test_persona_loop_integration.py` — extend with override + re-classification + stability gate + snapshot-evict tests

**Spec reference:** `docs/superpowers/specs/2026-04-29-persona-classifier-uplift-design.md`

---

## Tasks

### Task 1: Per-line state-query check

**Why:** A first message of `"source .venv/bin/activate\nhi\nhello"` should classify as `companion`. Today the start-anchored regex sees `source` and fails.

**Files:**
- Modify: `opencomputer/awareness/personas/classifier.py`
- Test: `tests/test_persona_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_classifier.py`:

```python
def test_multi_line_first_message_state_query_matches():
    """Greeting on a non-first line should still match. Real-world bug:
    user pastes ``source .venv/bin/activate`` then types ``hi`` on the
    next line — the message reaches the classifier as a single
    multi-line string and the start-anchored regex used to miss it."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source /path/.venv/bin/activate\nhi\nhello",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"
    assert "state-query" in result.reason


def test_multi_line_greeting_on_third_line_matches():
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("ls -la\ncd /tmp\nhow are you?",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_single_line_state_query_still_matches_after_per_line_change():
    """Regression guard: the simple single-line case must keep working."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("hi",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_non_greeting_multi_line_does_not_match_state_query():
    """Regression guard: a multi-line message with no greeting line must
    NOT trigger the state-query rule."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source .venv/bin/activate\npython main.py\npytest",),
    )
    result = classify(ctx)
    # Falls through to coding-app rule — NOT to companion.
    assert result.persona_id == "coding"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate
pytest tests/test_persona_classifier.py::test_multi_line_first_message_state_query_matches -v
```

Expected: FAIL — current regex matches `last_msg` whole, sees `source` first, returns False.

- [ ] **Step 3: Update `is_state_query` to scan per line**

Edit `opencomputer/awareness/personas/classifier.py` — replace `is_state_query`:

```python
def is_state_query(text: str) -> bool:
    """True iff *text* leads with a state-query / greeting / "how are you" pattern.

    Splits on newlines and checks each line independently — a multi-line
    paste like ``source .venv/bin/activate\\nhi`` should match because
    line 2 leads with a greeting. Lines are stripped before matching to
    avoid leading-whitespace defeating the start anchor.

    Exposed for tests; used internally by :func:`classify`.
    """
    if not text:
        return False
    for line in text.split("\n"):
        if _STATE_QUERY_PATTERN.match(line):
            return True
    return False
```

- [ ] **Step 4: Run the failing tests to verify they pass**

```bash
pytest tests/test_persona_classifier.py -v
```

Expected: PASS for all four new tests + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git add opencomputer/awareness/personas/classifier.py tests/test_persona_classifier.py
git commit -m "fix(persona): per-line state-query check

Multi-line first messages (e.g. \`source .venv/bin/activate\\nhi\`)
were defeated by the start-anchored regex. Split on newlines and try
each line; matches on any one line wins.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Scan all 3 recent messages, not just the latest

**Why:** A user who says `hello` then asks a coding question should still nudge toward companion if their *recent* trajectory was social. Today we only look at `last_messages[-1]`.

**Files:**
- Modify: `opencomputer/awareness/personas/classifier.py:65-86` (the `classify` function)
- Test: `tests/test_persona_classifier.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persona_classifier.py`:

```python
def test_state_query_in_recent_messages_not_just_latest():
    """If any of the last 3 user messages is a state-query, the
    classifier should consider it. Conversation often opens with
    'hi' then continues with 'btw can you check this thing'."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=(
            "hi",
            "how was your day",
            "ok",
        ),
    )
    result = classify(ctx)
    # 'hi' on the first message should keep us in companion territory.
    assert result.persona_id == "companion"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_persona_classifier.py::test_state_query_in_recent_messages_not_just_latest -v
```

Expected: FAIL — current `classify` only checks `last_messages[-1]` (`"ok"`), which doesn't match.

- [ ] **Step 3: Update `classify` to scan recent messages**

In `opencomputer/awareness/personas/classifier.py`, replace these lines in `classify`:

```python
    last_msg = ctx.last_messages[-1] if ctx.last_messages else ""
    state_query = is_state_query(last_msg)
```

with:

```python
    # Scan the last up-to-3 messages. State-query in any one of them
    # signals social register. Latest-message-only was too brittle —
    # users often open with "hi" then ask a follow-up like "ok".
    state_query = any(is_state_query(m) for m in ctx.last_messages[-3:])
    # ``last_msg`` is still used in the matched-reason string below.
    last_msg = ctx.last_messages[-1] if ctx.last_messages else ""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_persona_classifier.py -v
```

Expected: PASS — new test + all existing tests.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/awareness/personas/classifier.py tests/test_persona_classifier.py
git commit -m "fix(persona): check all 3 recent messages for state-query

Latest-message-only missed the common 'hi' -> 'follow-up question'
shape. Scan the last 3 user messages; any one matching is enough.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Hindi / Hinglish state-query patterns

**Why:** The user is `en_IN` and uses Hinglish socially. Patterns like "kaise ho" / "kya haal hai" / "theek ho" don't match today.

**Files:**
- Modify: `opencomputer/awareness/personas/classifier.py:43-54` (`_STATE_QUERY_PATTERN`)
- Test: `tests/test_persona_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_classifier.py`:

```python
def test_hindi_state_query_matches():
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("kaise ho",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_hinglish_state_query_matches():
    for opener in (
        "kya haal hai bhai",
        "theek ho?",
        "sab badhiya?",
        "kya chal raha hai",
    ):
        ctx = ClassificationContext(
            foreground_app="iTerm2",
            time_of_day_hour=14,
            last_messages=(opener,),
        )
        result = classify(ctx)
        assert result.persona_id == "companion", f"failed for {opener!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_persona_classifier.py::test_hindi_state_query_matches \
       tests/test_persona_classifier.py::test_hinglish_state_query_matches -v
```

Expected: FAIL.

- [ ] **Step 3: Extend `_STATE_QUERY_PATTERN`**

In `opencomputer/awareness/personas/classifier.py`, replace `_STATE_QUERY_PATTERN`:

```python
#: Regex that detects state-query openings. Anchored at start (after
#: optional punctuation/whitespace) so "how are you doing in this codebase"
#: only matches if the message LEADS with the greeting — random
#: occurrences mid-coding-question don't trigger.
#:
#: Hindi / Hinglish openers ("kaise ho", "kya haal hai", "theek ho",
#: "sab badhiya", "kya chal raha hai") are folded in for the en_IN
#: register.
_STATE_QUERY_PATTERN = re.compile(
    r"^[\s\W]*"
    r"(how\s+are\s+you|how\s+(are\s+)?u|"
    r"how\s+(have\s+)?you\s+been|how's\s+it\s+going|how('?s|s)\s+life|"
    r"what'?s\s+up|whats\s+up|sup\b|"
    r"hey\s*(claude|oc|computer)?\b|hi\s*(claude|oc|computer)?\b|hello\b|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"you\s+(doing|feeling)\s+(ok|alright|good)|"
    r"how('?re|\s+are)\s+you\s+holding\s+up|"
    r"(are\s+you\s+)?ok\??\s*$|"
    # Hindi / Hinglish — common openers in en_IN scripts.
    r"kaise\s+ho|kaisa\s+hai|kaise\s+hain|"
    r"kya\s+haal|kya\s+chal|kya\s+ho\s+raha|"
    r"theek\s+ho|theek\s+hain|"
    r"sab\s+badhiya|sab\s+theek|"
    r"namaste|namaskar)",
    re.IGNORECASE,
)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_classifier.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/awareness/personas/classifier.py tests/test_persona_classifier.py
git commit -m "feat(persona): Hindi/Hinglish state-query patterns

Adds 'kaise ho', 'kya haal hai', 'theek ho', 'sab badhiya',
'namaste' etc. to the state-query opener regex for the en_IN
register.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Emotion-lexicon classification rule

**Why:** A message like "i am sad just went through a break up" has no greeting marker but is overwhelmingly companion-shaped. The classifier today falls through to time-of-day / coding-app and never picks companion.

**Files:**
- Modify: `opencomputer/awareness/personas/classifier.py`
- Test: `tests/test_persona_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_classifier.py`:

```python
def test_emotion_anchor_message_classifies_companion():
    ctx = ClassificationContext(
        foreground_app="iTerm2",  # would normally trigger coding
        time_of_day_hour=14,
        last_messages=("i am sad just went through a break up",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"
    assert "emotion" in result.reason.lower()


def test_emotion_lexicon_does_not_override_trading_app():
    """Strong app signals (trading) are explicit user choice — should
    still win over emotion lexicon. The lexicon only beats coding /
    file-fallback / time-of-day."""
    ctx = ClassificationContext(
        foreground_app="Zerodha Kite",
        time_of_day_hour=14,
        last_messages=("im stressed about this loss",),
    )
    result = classify(ctx)
    assert result.persona_id == "trading"


def test_emotion_lexicon_does_not_override_relaxed_app():
    ctx = ClassificationContext(
        foreground_app="Spotify",
        time_of_day_hour=22,
        last_messages=("im exhausted today",),
    )
    result = classify(ctx)
    assert result.persona_id == "relaxed"


def test_multiple_emotion_terms_match():
    for msg in (
        "feeling lonely tonight",
        "i'm heartbroken",
        "really stressed about work",
        "grieving my dog",
        "im happy we shipped it!",
    ):
        ctx = ClassificationContext(
            foreground_app="iTerm2",
            time_of_day_hour=14,
            last_messages=(msg,),
        )
        result = classify(ctx)
        assert result.persona_id == "companion", f"failed for {msg!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_persona_classifier.py::test_emotion_anchor_message_classifies_companion -v
```

Expected: FAIL — current code falls through to coding-app rule.

- [ ] **Step 3: Add the emotion-lexicon pattern + classification rule**

In `opencomputer/awareness/personas/classifier.py`, after `_STATE_QUERY_PATTERN`, add:

```python
#: Emotion-anchor lexicon. When a recent user message contains one of
#: these terms — without necessarily leading with a greeting — the
#: register is companion-shaped. Inserted into :func:`classify` AFTER
#: trading/relaxed (which are explicit user-app choices that still win)
#: but BEFORE coding-app / file-fallback / time-of-day so the warm
#: register lands on emotional content even while the user is in a
#: terminal.
_EMOTION_PATTERN = re.compile(
    r"\b("
    r"sad|lonely|heartbroken|grieving|depressed|anxious|"
    r"stressed|frustrated|burnt\s+out|burned\s+out|exhausted|"
    r"happy|excited|grateful|relieved|"
    r"break\s*up|breakup|broke\s+up|"
    r"miss\s+(her|him|them|my|you)|"
    r"died|passed\s+away|funeral|"
    r"feeling\s+(\w+)|"  # 'feeling X' — generic emotion shape
    r"i('?m|\s+am)\s+(sad|happy|stressed|anxious|tired|done|broken|hurt|fine|ok|okay)"
    r")\b",
    re.IGNORECASE,
)


def has_emotion_anchor(text: str) -> bool:
    """True iff *text* contains an emotion-anchor term.

    Exposed for tests; used internally by :func:`classify`.
    """
    return bool(_EMOTION_PATTERN.search(text or ""))
```

Then modify `classify` to insert the emotion check between the relaxed-app rule and the state-query rule:

```python
def classify(ctx: ClassificationContext) -> ClassificationResult:
    # Path A.1 — state-query detector. Runs FIRST so a "how are you" while
    # in VS Code still goes to companion (the user is engaging socially,
    # not asking about code). Strong app signals (trading, relaxed) still
    # win because those are explicit user-context choices, but the default
    # coding signal yields to companion when the actual message is a
    # state-query.
    state_query = any(is_state_query(m) for m in ctx.last_messages[-3:])
    last_msg = ctx.last_messages[-1] if ctx.last_messages else ""

    app_lower = ctx.foreground_app.lower()
    if any(a in app_lower for a in _TRADING_APPS):
        return ClassificationResult("trading", 0.85, f"foreground app '{ctx.foreground_app}' suggests trading")
    if any(a in app_lower for a in _RELAXED_APPS):
        return ClassificationResult("relaxed", 0.8, f"foreground app '{ctx.foreground_app}' suggests relaxed mode")
    if state_query:
        return ClassificationResult(
            "companion", 0.9,
            f"state-query / greeting detected in recent messages: {last_msg[:40]!r}",
        )
    # Emotion-anchor scan over the last 3 messages. Same precedence as
    # state-query: trading/relaxed app overrides win, but coding-app
    # and file-fallback yield to emotional content.
    emotion_msg = next(
        (m for m in reversed(ctx.last_messages[-3:]) if has_emotion_anchor(m)),
        None,
    )
    if emotion_msg is not None:
        return ClassificationResult(
            "companion", 0.75,
            f"emotion-anchor term detected in recent messages: {emotion_msg[:40]!r}",
        )
    if any(a in app_lower for a in _CODING_APPS):
        return ClassificationResult("coding", 0.85, f"foreground app '{ctx.foreground_app}' suggests coding")

    # File-based fallback
    py_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".py"))
    md_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".md"))
    if py_files >= 3:
        return ClassificationResult("coding", 0.7, f"{py_files} recent .py files")
    if md_files >= 3:
        return ClassificationResult("learning", 0.6, f"{md_files} recent .md files")

    # Time-of-day fallback
    if ctx.time_of_day_hour >= 21 or ctx.time_of_day_hour < 6:
        return ClassificationResult("relaxed", 0.5, f"hour={ctx.time_of_day_hour} (evening/late)")
    if 9 <= ctx.time_of_day_hour < 12:
        return ClassificationResult("coding", 0.4, "morning hours, default to coding")

    return ClassificationResult("companion", 0.3, "no strong signal — default companion")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_classifier.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/awareness/personas/classifier.py tests/test_persona_classifier.py
git commit -m "feat(persona): emotion-lexicon classification rule

Adds an emotion-anchor scan (sad, lonely, heartbroken, breakup,
'feeling X', etc.) that bumps to companion when the user is talking
about feelings — even from a coding-app foreground. Trading/relaxed
app overrides still win.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Read `persona_id_override` in `_build_persona_overlay`

**Why:** Foundation for the slash-command override. The override must short-circuit the classifier so a user-set persona is honored verbatim.

**Files:**
- Modify: `opencomputer/agent/loop.py:1532-1633` (`_build_persona_overlay`)
- Test: `tests/test_persona_loop_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_loop_integration.py`:

```python
def test_persona_override_short_circuits_classifier(tmp_path, monkeypatch):
    """When runtime.custom['persona_id_override'] is set to a known
    persona id, _build_persona_overlay must return that persona's
    overlay regardless of foreground app or messages."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "companion"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",  # would normally trigger coding
    ):
        overlay = loop._build_persona_overlay("test-session")

    assert "honest answer" in overlay.lower() or "warm" in overlay.lower() or "companion" in overlay.lower()
    assert loop._active_persona_id == "companion"


def test_persona_override_invalid_id_falls_back_to_classifier(tmp_path, monkeypatch):
    """An override pointing at a deleted/invalid persona id must NOT
    break the loop — fall through to the classifier path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "nonexistent_persona"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("test-session")

    assert isinstance(overlay, str)
    # Falls through to classifier → coding (Cursor is a coding app).
    assert loop._active_persona_id == "coding"
```

(Make sure `from unittest.mock import patch` is imported at the top of the test file — it already is per Task 5 of the existing file; verify before adding the tests.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_persona_loop_integration.py::test_persona_override_short_circuits_classifier -v
```

Expected: FAIL — `_build_persona_overlay` doesn't read the override yet.

- [ ] **Step 3: Add override read at the top of `_build_persona_overlay`**

In `opencomputer/agent/loop.py`, replace the body of `_build_persona_overlay` after the import block with:

```python
        from opencomputer.awareness.personas._foreground import (
            detect_frontmost_app,
        )
        from opencomputer.awareness.personas.classifier import (
            ClassificationContext,
            classify,
        )
        from opencomputer.awareness.personas.registry import get_persona

        # Phase persona-uplift (2026-04-29): user override wins over the
        # auto-classifier. ``runtime.custom["persona_id_override"]`` is
        # set by the ``/persona-mode <id>`` slash command. An invalid id
        # (e.g. user-deleted persona) falls through to the classifier
        # path so the agent never wedges over a bad override.
        override_id = ""
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            override_id = str(rt.custom.get("persona_id_override", "") or "").strip()

        if override_id:
            override_persona = get_persona(override_id)
            if override_persona is not None:
                self._active_persona_id = str(override_id)
                if rt is not None:
                    rt.custom["active_persona_id"] = self._active_persona_id
                self._active_persona_preferred_tone = str(
                    override_persona.get("preferred_tone", "") or ""
                ).strip()
                overlay = override_persona.get("system_prompt_overlay", "") or ""
                return str(overlay).strip()
            # Invalid override id — log and fall through. We do NOT
            # clear the override; the user can fix or `/persona-mode auto`.
            _log.debug(
                "persona override id %r not found; falling through to classifier",
                override_id,
            )

        try:
            foreground_app = detect_frontmost_app()
        except Exception:  # noqa: BLE001 — defensive: never break loop
            foreground_app = ""
        # ... (rest of existing function unchanged)
```

(Keep the existing classifier body from `try:` `foreground_app = detect_frontmost_app()` onward intact.)

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_loop_integration.py -v
```

Expected: PASS — both new tests + all existing tests.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_persona_loop_integration.py
git commit -m "feat(persona): runtime override short-circuits classifier

_build_persona_overlay now reads runtime.custom['persona_id_override']
first. If set to a known persona id, that overlay wins; if set to an
unknown id, falls through to the classifier (defensive — never wedge).

Foundation for /persona-mode slash command landing next.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: `/persona-mode` slash command implementation

**Why:** User-facing knob to set / clear / inspect the override.

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/persona_mode_cmd.py`
- Test: `tests/test_persona_mode_command.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persona_mode_command.py`:

```python
"""Tests for the /persona-mode slash command."""
from __future__ import annotations

import asyncio

from plugin_sdk.runtime_context import RuntimeContext


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_persona_mode_lists_personas_when_no_args():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["active_persona_id"] = "coding"
    result = asyncio.run(cmd.execute("", rt))

    assert "Active persona: coding" in result.output
    assert "companion" in result.output
    assert "coding" in result.output
    assert "(override: none)" in result.output


def test_persona_mode_lists_shows_override_when_set():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["active_persona_id"] = "coding"
    rt.custom["persona_id_override"] = "companion"
    result = asyncio.run(cmd.execute("", rt))

    assert "(override: companion)" in result.output


def test_persona_mode_sets_override():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("companion", rt))

    assert rt.custom.get("persona_id_override") == "companion"
    assert "companion" in result.output.lower()


def test_persona_mode_auto_clears_override():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["persona_id_override"] = "companion"
    result = asyncio.run(cmd.execute("auto", rt))

    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
    assert "auto" in result.output.lower() or "cleared" in result.output.lower()


def test_persona_mode_rejects_unknown_id():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("not_a_real_persona", rt))

    assert "Unknown" in result.output or "not found" in result.output.lower()
    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )


def test_persona_mode_set_evicts_prompt_snapshot_via_runtime_flag():
    """Setting an override should drop a marker the agent loop can read
    to invalidate its prompt snapshot. We use runtime.custom['_persona_dirty']
    as that marker — the loop reads + clears it on the next turn."""
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    asyncio.run(cmd.execute("companion", rt))

    assert rt.custom.get("_persona_dirty") is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_persona_mode_command.py -v
```

Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Create the slash command**

Create `opencomputer/agent/slash_commands_impl/persona_mode_cmd.py`:

```python
"""``/persona-mode [<id>|auto]`` — list / set / clear the auto-classifier
persona override.

Distinct from:

- ``/persona``     — ensemble profile switcher (different SOUL.md /
                     MEMORY.md per profile dir)
- ``/personality`` — storage-only knob with a different vocabulary
                     (helpful / concise / technical / creative / ...)

This command sets ``runtime.custom["persona_id_override"]``. The agent
loop reads it in :meth:`AgentLoop._build_persona_overlay` (override wins
over the auto-classifier). Setting it ALSO drops a
``runtime.custom["_persona_dirty"]`` flag so the loop can evict its
prompt snapshot for the session and pick up the new overlay on the very
next turn.

``auto`` clears the override and re-enables the classifier.
"""
from __future__ import annotations

from opencomputer.awareness.personas.registry import list_personas
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class PersonaModeCommand(SlashCommand):
    name = "persona-mode"
    description = "Set / clear / list the persona override (see /persona-mode)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        personas = list_personas()
        ids = sorted(p["id"] for p in personas)

        if not ids:
            return SlashCommandResult(
                output=(
                    "No personas configured. Bundled defaults should ship "
                    "in opencomputer/awareness/personas/defaults/. Check "
                    "your install or report a bug."
                ),
                handled=True,
            )

        if sub == "":
            active = runtime.custom.get("active_persona_id", "(unset)")
            override = runtime.custom.get("persona_id_override", "")
            override_line = f"(override: {override})" if override else "(override: none)"
            lines = [
                f"Active persona: {active} {override_line}",
                "",
                "Available:",
            ]
            for pid in ids:
                marker = " (active)" if pid == active else ""
                lines.append(f"  - {pid}{marker}")
            lines.append("")
            lines.append(
                "Usage: /persona-mode <id> | auto      "
                "(`auto` clears the override and re-enables the classifier)"
            )
            return SlashCommandResult(output="\n".join(lines), handled=True)

        if sub == "auto":
            runtime.custom.pop("persona_id_override", None)
            runtime.custom["_persona_dirty"] = True
            return SlashCommandResult(
                output="Persona override cleared — auto-classifier re-enabled.",
                handled=True,
            )

        if sub not in ids:
            return SlashCommandResult(
                output=(
                    f"Unknown persona {sub!r}. "
                    f"Available: {', '.join(ids)}"
                ),
                handled=True,
            )

        runtime.custom["persona_id_override"] = sub
        runtime.custom["_persona_dirty"] = True
        return SlashCommandResult(
            output=f"Persona override set to {sub}. "
                   f"Takes effect on the next turn.",
            handled=True,
        )


__all__ = ["PersonaModeCommand"]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_mode_command.py -v
```

Expected: PASS for all six tests.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/persona_mode_cmd.py tests/test_persona_mode_command.py
git commit -m "feat(slash): /persona-mode — override auto-classifier persona

Lists, sets, and clears the auto-classifier persona override.
Distinct from /persona (ensemble) and /personality (knob).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Register `/persona-mode` in `_BUILTIN_COMMANDS`

**Why:** Wire the new command into the dispatch path.

**Files:**
- Modify: `opencomputer/agent/slash_commands.py:27-87`
- Test: `tests/test_persona_mode_command.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persona_mode_command.py`:

```python
def test_persona_mode_command_is_registered():
    """The /persona-mode command must be in the built-ins registry so
    dispatch can find it."""
    from opencomputer.agent.slash_commands import (
        get_registered_commands,
        register_builtin_slash_commands,
    )

    register_builtin_slash_commands()
    names = {getattr(c, "name", "") for c in get_registered_commands()}
    assert "persona-mode" in names
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_persona_mode_command.py::test_persona_mode_command_is_registered -v
```

Expected: FAIL — `persona-mode` not in registry.

- [ ] **Step 3: Add to imports + `_BUILTIN_COMMANDS`**

In `opencomputer/agent/slash_commands.py`, add the import (alphabetised with siblings):

```python
from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
    PersonaModeCommand,
)
```

And add `PersonaModeCommand` to the `_BUILTIN_COMMANDS` tuple, e.g. just after `PersonalityCommand`:

```python
    SkinCommand,
    PersonalityCommand,
    PersonaModeCommand,  # /persona-mode — auto-classifier override
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_mode_command.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/slash_commands.py
git commit -m "feat(slash): register /persona-mode as built-in

Wires PersonaModeCommand into _BUILTIN_COMMANDS so the dispatch path
sees it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: Foreground-app caching helper

**Why:** Per-turn re-classification (Task 9) calls `detect_frontmost_app()` every turn, which spawns `osascript` with a 2-second timeout. Cache for 30 seconds inside the loop instance.

**Files:**
- Modify: `opencomputer/agent/loop.py` — add `_cached_foreground_app(now)` method + state vars
- Test: `tests/test_persona_loop_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_loop_integration.py`:

```python
def test_cached_foreground_app_returns_cached_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0

    call_count = {"n": 0}

    def _fake_detect():
        call_count["n"] += 1
        return f"App{call_count['n']}"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        side_effect=_fake_detect,
    ):
        first = loop._cached_foreground_app(now=1000.0)
        second = loop._cached_foreground_app(now=1010.0)  # +10s, within TTL
        third = loop._cached_foreground_app(now=1031.0)  # +31s, past TTL

    assert first == "App1"
    assert second == "App1"  # cached
    assert third == "App2"   # refreshed
    assert call_count["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_persona_loop_integration.py::test_cached_foreground_app_returns_cached_within_ttl -v
```

Expected: FAIL — `_cached_foreground_app` doesn't exist.

- [ ] **Step 3: Add the helper + initialize state vars**

In `opencomputer/agent/loop.py`, add to `AgentLoop.__init__` near `self._active_persona_id`:

```python
        #: Persona-uplift (2026-04-29): cached foreground-app value with
        #: 30s TTL so per-turn re-classification doesn't spawn osascript
        #: every turn. Empty string is a valid cache state.
        self._foreground_app_cache: str = ""
        self._foreground_app_cache_at: float = 0.0
        #: Stability gate state for re-classification: track candidate
        #: persona id + how many consecutive turns it has been seen.
        self._pending_persona_id: str = ""
        self._pending_persona_count: int = 0
        #: Cooldown counter — reset to 0 on a confirmed persona flip.
        #: Increments on every reclassify call. We refuse to flip again
        #: until this exceeds the cooldown threshold (3) — prevents
        #: thrash when the user briefly Cmd-Tabs between apps. The
        #: dirty-flag path (slash-command override) bypasses this
        #: cooldown so an explicit user choice always wins.
        self._reclassify_calls_since_flip: int = 999
```

Then add the method on the class, e.g. just below `_build_persona_overlay`:

```python
    def _cached_foreground_app(self, now: float | None = None) -> str:
        """Return foreground app name with a 30-second TTL cache.

        Per-turn re-classification calls this on every user turn; the
        underlying ``detect_frontmost_app()`` spawns ``osascript`` with a
        2-second timeout which is too slow to run unconditionally.
        ``now`` is for testing — production callers omit it.
        """
        import time as _time

        from opencomputer.awareness.personas._foreground import (
            detect_frontmost_app,
        )

        if now is None:
            now = _time.monotonic()
        if now - self._foreground_app_cache_at < 30.0 and (
            self._foreground_app_cache_at != 0.0
        ):
            return self._foreground_app_cache
        try:
            value = detect_frontmost_app()
        except Exception:  # noqa: BLE001 — defensive: never break loop
            value = ""
        self._foreground_app_cache = value
        self._foreground_app_cache_at = now
        return value
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_persona_loop_integration.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_persona_loop_integration.py
git commit -m "feat(persona): foreground-app cache with 30s TTL

Avoids spawning osascript on every turn once per-turn re-classification
lands. Defensive: any detector exception caches the empty string.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 9: Per-turn re-classification with stability gate

**Why:** The headline change. Persona adapts to drift — same shape as `vibe_classifier` already does for vibe.

**Files:**
- Modify: `opencomputer/agent/loop.py` — new `_maybe_reclassify_persona`; call site after user-message persist; honour `_persona_dirty` from Task 6
- Test: `tests/test_persona_loop_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_persona_loop_integration.py`:

```python
def _make_loop_with_db(messages):
    """Build a stub AgentLoop with a fixed message history."""
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content
            self.tool_calls = ()

    class _StubDB:
        def __init__(self, msgs):
            self._msgs = msgs

        def get_messages(self, sid):
            return self._msgs

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB([_Msg("user", m) for m in messages])
    loop._runtime = RuntimeContext()
    loop._active_persona_id = "coding"
    loop._active_persona_preferred_tone = ""
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0
    loop._pending_persona_id = ""
    loop._pending_persona_count = 0
    loop._reclassify_calls_since_flip = 999  # no cooldown active
    loop._prompt_snapshots = type("D", (), {"pop": lambda self, k, d=None: None})()
    return loop


def test_reclassify_does_not_flap_on_single_signal(tmp_path, monkeypatch):
    """One emotional message in an otherwise-coding session should NOT
    flip persona on its own. Stability gate requires 2 consecutive
    same-classification turns OR confidence >= 0.85."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(
        ["fix this bug", "i am sad about this regression"]
    )

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    # First sighting of 'companion' — gate not yet passed.
    assert loop._active_persona_id == "coding"
    assert loop._pending_persona_id == "companion"
    assert loop._pending_persona_count == 1


def test_reclassify_flips_after_two_consecutive_signals(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(
        ["fix this bug", "i am sad about this regression"]
    )

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")  # first sighting
        # Second user turn — same classification result.
        loop.db._msgs.append(type(loop.db._msgs[0])(
            "user", "feeling really lonely tonight"
        ))
        loop._maybe_reclassify_persona("test-session")  # second sighting → flip

    assert loop._active_persona_id == "companion"
    assert loop._pending_persona_count == 0  # reset after flip


def test_reclassify_high_confidence_short_circuits_gate(tmp_path, monkeypatch):
    """Confidence >= 0.85 (e.g. trading-app foreground) flips immediately."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    # Trading app -> confidence 0.85 -> immediate flip.
    assert loop._active_persona_id == "trading"


def test_reclassify_skipped_when_override_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["i am sad"])
    loop._runtime.custom["persona_id_override"] = "admin"
    loop._active_persona_id = "admin"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert loop._active_persona_id == "admin"  # unchanged
    assert loop._pending_persona_id == ""      # gate untouched


def test_reclassify_evicts_prompt_snapshot_on_flip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])
    # OrderedDict-shaped pop captures whether eviction happened.
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    # Confidence 0.85 short-circuit -> flip -> snapshot evicted.
    assert "test-session" in evicted


def test_reclassify_honours_persona_dirty_flag_from_slash_command(tmp_path, monkeypatch):
    """When /persona-mode sets _persona_dirty=True, the loop must evict
    the snapshot on the next reclassification call regardless of whether
    persona changed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["fix this bug"])
    loop._runtime.custom["_persona_dirty"] = True
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert "test-session" in evicted
    assert loop._runtime.custom.get("_persona_dirty") is None  # cleared


def test_reclassify_cooldown_prevents_thrashing(tmp_path, monkeypatch):
    """After a flip, refuse to flip again within 3 reclassify calls.
    Prevents thrash when user Cmd-Tabs between coding and trading apps
    in quick succession."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # First flip — Zerodha → trading (immediate, conf 0.85).
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"
    assert loop._reclassify_calls_since_flip == 0

    # User immediately switches back to iTerm — would normally flip to
    # coding but cooldown prevents it.
    loop.db._msgs.append(type(loop.db._msgs[0])("user", "fix this bug"))
    loop._foreground_app_cache = ""  # force refresh
    loop._foreground_app_cache_at = 0.0
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"  # cooldown blocked the flip


def test_reclassify_cooldown_clears_after_threshold(tmp_path, monkeypatch):
    """After 3 reclassify calls, the cooldown lifts and flips can fire."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # Flip to trading.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"

    # Three more reclassify calls in trading mode — cooldown counter
    # increments past 3.
    for _ in range(3):
        with patch(
            "opencomputer.awareness.personas._foreground.detect_frontmost_app",
            return_value="Zerodha Kite",
        ):
            loop._maybe_reclassify_persona("test-session")

    # Now switch app — should flip.
    loop.db._msgs.append(type(loop.db._msgs[0])("user", "fix this bug"))
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "coding"


def test_reclassify_dirty_flag_bypasses_cooldown(tmp_path, monkeypatch):
    """Slash-command override (dirty flag) must always evict the snapshot
    even if the cooldown is active. Explicit user choice wins."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # Flip first to set up cooldown.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._reclassify_calls_since_flip == 0

    # User runs `/persona-mode admin` mid-cooldown.
    loop._runtime.custom["persona_id_override"] = "admin"
    loop._runtime.custom["_persona_dirty"] = True
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert "test-session" in evicted  # dirty flag evicted despite cooldown
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_persona_loop_integration.py -v
```

Expected: FAIL — `_maybe_reclassify_persona` doesn't exist.

- [ ] **Step 3: Implement `_maybe_reclassify_persona`**

In `opencomputer/agent/loop.py`, add after the existing `_build_persona_overlay`:

```python
    def _maybe_reclassify_persona(
        self, session_id: str, messages: list | None = None
    ) -> None:
        """Per-turn re-classification with stability gate + cooldown.

        Called from the user-turn boundary in :meth:`run_conversation`
        AFTER the user message is persisted. ``messages`` is the
        in-memory message list the loop already holds; we accept it so
        we don't re-read from SQLite. ``messages=None`` falls back to
        ``db.get_messages(session_id)``.

        Behavior:
        - The slash-command dirty flag (``runtime.custom["_persona_dirty"]``)
          forces a snapshot evict regardless. Set by ``/persona-mode``;
          the slash-command path always wins.
        - When ``runtime.custom["persona_id_override"]`` is set, skip
          re-classification entirely.
        - Otherwise classify, apply stability gate (2 consecutive
          same-id matches OR confidence >= 0.85), then a cooldown gate
          (no flip within 3 reclassify calls of the last flip).
        - On a confirmed flip: update ``_active_persona_id``, mirror to
          ``runtime.custom``, reset pending + cooldown counters, evict
          ``_prompt_snapshots[session_id]``, and log at DEBUG level.

        Defensive: any failure is caught and logged; the active persona
        is left unchanged. The agent loop must NEVER break over a
        re-classification miss.
        """
        import datetime as _dt

        from opencomputer.awareness.personas.classifier import (
            ClassificationContext,
            classify,
        )

        rt = getattr(self, "_runtime", None)

        # Honour the slash-command dirty flag — the user just set or
        # cleared an override, snapshot must be rebuilt next turn even
        # if the active persona id didn't change. This bypasses the
        # cooldown — an explicit user choice always wins.
        if rt is not None and rt.custom.pop("_persona_dirty", False):
            try:
                self._prompt_snapshots.pop(session_id, None)
            except Exception:  # noqa: BLE001
                _log.debug("snapshot evict on _persona_dirty failed", exc_info=True)

        # Override-locked: skip the classifier entirely.
        if rt is not None and rt.custom.get("persona_id_override"):
            return

        # Cooldown bookkeeping happens BEFORE classify (so even a no-op
        # call increments). Cap at a large number to avoid overflow on
        # ultra-long sessions; any value >= 3 satisfies the threshold.
        self._reclassify_calls_since_flip = min(
            self._reclassify_calls_since_flip + 1, 1_000_000
        )

        try:
            ctx = ClassificationContext(
                foreground_app=self._cached_foreground_app(),
                time_of_day_hour=_dt.datetime.now().hour,
                recent_file_paths=(),  # not used for re-classification
                last_messages=self._recent_user_messages(session_id, messages),
            )
            result = classify(ctx)
        except Exception:  # noqa: BLE001 — defensive: never break loop
            _log.debug("re-classify failed; persona unchanged", exc_info=True)
            return

        # Already in the same persona — reset gate, done.
        if result.persona_id == self._active_persona_id:
            self._pending_persona_id = ""
            self._pending_persona_count = 0
            return

        # Stability gate: 2 consecutive matches required, OR confidence
        # >= 0.85 short-circuits (strong-app signal).
        flip_now = result.confidence >= 0.85
        if not flip_now:
            if result.persona_id == self._pending_persona_id:
                self._pending_persona_count += 1
                if self._pending_persona_count >= 2:
                    flip_now = True
            else:
                self._pending_persona_id = result.persona_id
                self._pending_persona_count = 1

        if not flip_now:
            return

        # Cooldown gate: refuse to flip again within 3 reclassify calls
        # of the last flip. Prevents thrashing when the user briefly
        # Cmd-Tabs between apps.
        if self._reclassify_calls_since_flip < 3:
            return

        prev = self._active_persona_id
        self._active_persona_id = result.persona_id
        self._pending_persona_id = ""
        self._pending_persona_count = 0
        self._reclassify_calls_since_flip = 0
        if rt is not None:
            rt.custom["active_persona_id"] = self._active_persona_id

        # Evict snapshot so the next turn rebuilds with the new overlay.
        try:
            self._prompt_snapshots.pop(session_id, None)
        except Exception:  # noqa: BLE001 — defensive
            _log.debug("snapshot evict on flip failed", exc_info=True)

        _log.debug(
            "persona_classifier.flip session=%s from=%s to=%s reason=%s",
            session_id,
            prev or "(unset)",
            self._active_persona_id,
            result.reason,
        )

    def _recent_user_messages(
        self, session_id: str, messages: list | None = None
    ) -> tuple[str, ...]:
        """Return the last 3 user-message contents for classifier context.

        Accepts ``messages`` from the caller (the loop already holds the
        in-memory list) to avoid re-reading the SQLite session DB. When
        ``messages`` is None, falls back to ``db.get_messages``.
        """
        if messages is None:
            try:
                messages = self.db.get_messages(session_id)
            except Exception:  # noqa: BLE001 — defensive
                return ()
        texts = [
            m.content for m in messages
            if getattr(m, "role", "") == "user"
            and isinstance(getattr(m, "content", None), str)
        ]
        return tuple(texts[-3:])
```

- [ ] **Step 4: Wire the call site in `run_conversation`**

In `opencomputer/agent/loop.py`, find the user-message-persist block (around the line `self._emit_before_message_write(session_id=sid, message=user_msg)` near line 854) and add the re-classification call IMMEDIATELY AFTER the user message is appended/persisted. Find this block:

```python
        user_msg = Message(
            role="user", content=user_message, attachments=list(images or [])
        )
        messages.append(user_msg)
        self._emit_before_message_write(session_id=sid, message=user_msg)
```

Add after it:

```python
        # Persona-uplift (2026-04-29): per-turn re-classification with
        # stability gate + cooldown. Pass the in-memory ``messages`` list
        # so the helper doesn't re-read SQLite. On a confirmed flip the
        # snapshot for ``sid`` is evicted; the NEXT turn rebuilds the
        # system prompt with the new overlay. Defensive: never raises.
        try:
            self._maybe_reclassify_persona(sid, messages=messages)
        except Exception:  # noqa: BLE001
            _log.debug("_maybe_reclassify_persona raised (suppressed)", exc_info=True)
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_persona_loop_integration.py -v
```

Expected: PASS — all 6 new re-classification tests + all existing tests.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_persona_loop_integration.py
git commit -m "feat(persona): per-turn re-classification with stability gate

The persona classifier now runs on every user turn. A 2-consecutive-
match gate (or confidence >= 0.85 short-circuit) prevents flapping on
single emotional messages. An active /persona-mode override skips
re-classification entirely. On a confirmed flip the prompt snapshot
for the session is evicted and the next turn rebuilds with the new
overlay.

Closes the asymmetry where vibe_classifier ran every turn but persona
was frozen at session start.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 10: End-to-end acceptance + final test sweep

**Why:** Validate every spec acceptance criterion in one place; confirm no regressions in the 880+ existing tests; lint clean.

**Files:**
- Test: `tests/test_persona_loop_integration.py` (one e2e test)

- [ ] **Step 1: Write the e2e acceptance test**

Append to `tests/test_persona_loop_integration.py`:

```python
def test_acceptance_multi_line_first_message_picks_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 1: multi-line first message with greeting
    on a non-first line picks companion, not coding."""
    from opencomputer.awareness.personas.classifier import (
        ClassificationContext,
        classify,
    )

    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source /path/.venv/bin/activate\nhi\nhello",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_acceptance_emotion_message_eventually_flips_to_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 2: starting in coding mode, two
    emotion-shaped turns flips persona to companion."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["fix this bug", "i am sad"])

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("acceptance-session")
        assert loop._active_persona_id == "coding"  # gate not yet passed
        loop.db._msgs.append(type(loop.db._msgs[0])(
            "user", "feeling lonely tonight"
        ))
        loop._maybe_reclassify_persona("acceptance-session")

    assert loop._active_persona_id == "companion"


def test_acceptance_persona_mode_override_renders_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 3: /persona-mode companion forces the
    companion overlay regardless of foreground app."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "companion"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("acceptance-session")

    assert loop._active_persona_id == "companion"
    assert overlay  # non-empty


def test_acceptance_persona_mode_auto_clears_and_reclassifies(tmp_path, monkeypatch):
    """Spec acceptance criterion 4: /persona-mode auto clears the
    override and the classifier resumes."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["persona_id_override"] = "companion"
    asyncio.run(cmd.execute("auto", rt))

    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
    assert rt.custom.get("_persona_dirty") is True


def test_acceptance_persona_mode_rejects_invalid():
    """Spec acceptance criterion 5: /persona-mode <invalid> rejects with
    list of valid ids."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("definitely_not_a_persona", rt))

    assert "Unknown" in result.output
    assert "companion" in result.output  # available list rendered
    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
```

- [ ] **Step 2: Run the new tests**

```bash
pytest tests/test_persona_loop_integration.py -k acceptance -v
```

Expected: PASS for all 5.

- [ ] **Step 3: Run the FULL persona-related suite**

```bash
pytest tests/test_persona_classifier.py \
       tests/test_persona_loop_integration.py \
       tests/test_persona_registry.py \
       tests/test_persona_mode_command.py \
       tests/test_companion_persona.py \
       tests/test_companion_anti_robot_cosplay.py \
       tests/test_companion_life_event_hook.py \
       tests/test_personality_prompt_wiring.py \
       tests/test_vibe_log.py -v
```

Expected: PASS on every test.

- [ ] **Step 4: Run the FULL pytest suite (regression sweep)**

```bash
pytest tests/ -x -q
```

Expected: PASS — total ~890+ tests. The `-x` halts on the first failure so any regression surfaces immediately.

- [ ] **Step 5: Run ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: no errors. Fix any minor style issues inline if they appear.

- [ ] **Step 6: Commit**

```bash
git add tests/test_persona_loop_integration.py
git commit -m "test(persona): end-to-end acceptance for uplift PR

Covers all 5 spec acceptance criteria — multi-line greeting,
emotion-driven flip, override path, override clear, invalid id reject.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review (writing-plans skill)

**Spec coverage:**
- Multi-line state-query bug → Task 1 ✓
- All-3-messages scan → Task 2 ✓
- Hindi/Hinglish → Task 3 ✓
- Emotion lexicon → Task 4 ✓
- Override read → Task 5 ✓
- /persona-mode command → Task 6 + 7 ✓
- Foreground app cache → Task 8 ✓
- Per-turn reclassification + stability gate → Task 9 ✓
- Snapshot eviction on flip → Task 9 ✓
- `_persona_dirty` flag honoured → Task 9 ✓
- All 5 acceptance criteria → Task 10 ✓
- Telemetry log line → Task 9 (`_log.debug("persona_classifier.flip ...")`) ✓
- Failure modes — defensive try/except at every layer → present in Task 5, 8, 9 ✓

**Placeholder scan:** No "TODO" / "implement later" / "similar to Task N" / "appropriate error handling" patterns. Every step has actual code or an exact command.

**Type consistency:**
- `_pending_persona_id: str` declared in Task 8, used in Task 9 ✓
- `_pending_persona_count: int` declared in Task 8, used in Task 9 ✓
- `_reclassify_calls_since_flip: int` declared in Task 8, used in Task 9 ✓
- `_foreground_app_cache` / `_foreground_app_cache_at` declared in Task 8, used in Task 9 via `_cached_foreground_app()` ✓
- `runtime.custom["persona_id_override"]` written in Task 6, read in Task 5 + Task 9 ✓
- `runtime.custom["_persona_dirty"]` written in Task 6, read in Task 9 ✓
- `PersonaModeCommand` defined in Task 6, registered in Task 7 ✓
- `_maybe_reclassify_persona(session_id, messages=None)` signature in Task 9 matches call site (passes `messages=messages`) ✓
- `_recent_user_messages(session_id, messages=None)` signature matches caller ✓

**Latent dependencies verified:**
- `from unittest.mock import patch` already imported in `tests/test_persona_loop_integration.py` (line 13).
- `_log` is the module-level logger in `loop.py`, used throughout.
- `_prompt_snapshots` is an `OrderedDict` — `.pop(key, default)` is a dict-protocol method, works on both `dict` and `OrderedDict`.
- `RuntimeContext.custom` is a mutable dict (verified via `loop.py:805-806` reading `self._runtime.custom.get("personality", "")`).

## Audit trail — refinements applied 2026-04-29

After the initial draft, an expert-critic self-audit caught these issues, all folded into the plan above:

1. **End-anchor regression risk.** Added `[\s\W]*$` to the state-query regex to prevent `hi = 5` matching, but realized this also breaks `"hi how are you doing"` (legit greeting + continuation). Reverted; accepting the rare `hi = 5` false positive — the regex is bounded by `\b` already and the per-line scan from Task 1 is sufficient.
2. **Cooldown gate.** Added `_reclassify_calls_since_flip` counter — refuse to flip again within 3 reclassify calls of the last flip. Prevents thrash when the user briefly Cmd-Tabs between coding and trading apps. Slash-command dirty path bypasses cooldown so explicit user choice wins.
3. **Avoid SQLite re-read on every turn.** `_maybe_reclassify_persona` now takes the in-memory `messages` list as a kwarg (default None falls back to db.get_messages). Loop call site passes the list it already holds.
4. **DEBUG, not INFO, for flip log.** Flip telemetry should not produce production log spam.
5. **Empty personas defensive case.** `/persona-mode` now handles `list_personas() == []` cleanly with a useful error.
6. **Slash description simplified.** Removed the meta-circular `(see /persona-mode for the list)` from the description text.
7. **Three additional cooldown tests** added to Task 9 to exercise the new gate.

Plan is complete, internally consistent, and audit-clean.
