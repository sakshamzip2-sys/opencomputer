# Personality + Statusline Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the existing `/personality <name>` slash command actually shape the model's output, and extend the new mode badge to also surface the active persona + personality.

**Architecture:** `runtime.custom["personality"]` is already set by the slash command but `base.j2` doesn't read it — today it's a no-op flag. Add a Jinja branch that injects a short personality directive when personality is anything other than "helpful" (the default-ish). Pass `personality` through `PromptContext` and both `build()` / `build_with_memory()` (mirrors the `permission_mode` plumbing pattern from PR-2). Extend `_render_mode_badge()` to also show persona + personality when they're set, in a single bottom row.

**Tech Stack:** Python 3.12+, frozen dataclasses, Jinja2, prompt_toolkit.

**Audit findings integrated:**
- `loop._active_persona_id` is the auto-classified plural-persona (V2.C) — distinct from user-set `runtime.custom["personality"]`. Both should appear in the badge when set.
- `loop._active_persona_id` already flows into `prompt_builder` via `active_persona_id` kwarg; `runtime.custom["personality"]` does not. PR-5 wires the second axis.
- Skin (`runtime.custom["skin"]`) is theme rendering, not prompt content. Out of scope for prompt wiring; could affect badge color but skipping for now.

---

### Task 1: Wire `personality` through PromptContext + base.j2

**Files:**
- Modify: `opencomputer/agent/prompt_builder.py` (PromptContext field; build / build_with_memory kwargs)
- Modify: `opencomputer/agent/loop.py` (pass `runtime.custom.get("personality")` into build call)
- Modify: `opencomputer/agent/prompts/base.j2` (new conditional block)
- Test: `tests/test_personality_prompt_wiring.py`

- [ ] **Step 1.1: Write failing test**

```python
# tests/test_personality_prompt_wiring.py
"""runtime.custom['personality'] shapes the system prompt via base.j2."""

from __future__ import annotations

from opencomputer.agent.prompt_builder import PromptBuilder


class TestPersonalityPromptWiring:
    def test_default_no_personality_block(self) -> None:
        rendered = PromptBuilder().build()
        # No personality directive when the value is empty / 'helpful'.
        assert "concise" not in rendered.lower() or "concise mode" not in rendered.lower()

    def test_concise_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="concise")
        assert "Concise" in rendered or "concise" in rendered.lower()
        # Should mention terseness / no filler in the directive
        assert "terse" in rendered.lower() or "no filler" in rendered.lower() or "skip preambles" in rendered.lower()

    def test_technical_personality_renders_directive(self) -> None:
        rendered = PromptBuilder().build(personality="technical")
        assert "technical" in rendered.lower() or "Technical" in rendered

    def test_helpful_personality_no_overlay(self) -> None:
        # 'helpful' is the baseline — no extra directive needed.
        rendered = PromptBuilder().build(personality="helpful")
        # Should not insert any personality-specific overlay.
        assert "Personality directive" not in rendered

    def test_unknown_personality_no_overlay(self) -> None:
        # Defensive: an unknown personality (typo, future addition) just no-ops.
        rendered = PromptBuilder().build(personality="bogus")
        assert "Personality directive" not in rendered
```

- [ ] **Step 1.2: Add `personality` to PromptContext + build kwargs + build_with_memory kwargs**

Add the field after `permission_mode` in `PromptContext`. Thread through `build()` and `build_with_memory()` as a kwarg (default `""`).

- [ ] **Step 1.3: Add Jinja branch to base.j2**

Insert after the existing "Auto mode" section, before "Memory integration":

```jinja
{% if personality and personality != "helpful" %}
# Personality directive

{% if personality == "concise" -%}
Be terse. Skip preambles. No filler. Lead with the result; explanation only on request.
{%- elif personality == "technical" -%}
Use precise technical vocabulary. Cite specific function names, file paths, line numbers, and behavior. Skip motivational framing.
{%- elif personality == "creative" -%}
Generate diverse options. Use vivid examples. Take stylistic liberties when appropriate.
{%- elif personality == "teacher" -%}
Explain the why before the what. Anticipate confusion. Show worked examples. Check understanding.
{%- elif personality == "hype" -%}
Be energetic and direct. Encourage. Lean into momentum. Concise but warm.
{%- endif %}
{% endif %}
```

- [ ] **Step 1.4: Wire `runtime.custom["personality"]` from loop**

In `loop.py` near the `permission_mode` thread-through (the `build_with_memory` call site), add:

```python
                    personality=self._runtime.custom.get("personality", "") if self._runtime else "",
```

- [ ] **Step 1.5: Run + commit**

```
pytest tests/test_personality_prompt_wiring.py tests/test_base_prompt_engineered.py -v
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
git add OpenComputer/opencomputer/agent/prompt_builder.py OpenComputer/opencomputer/agent/loop.py OpenComputer/opencomputer/agent/prompts/base.j2 OpenComputer/tests/test_personality_prompt_wiring.py
git commit -m "feat(prompt): /personality value now shapes base.j2 output via Jinja branch"
```

---

### Task 2: Extend mode badge with persona + personality

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py` (`_render_mode_badge`)
- Modify: `tests/test_mode_badge.py` (add new assertions)

- [ ] **Step 2.1: Update test**

```python
class TestBadgeIncludesPersonaAndPersonality:
    def test_badge_shows_personality_when_set(self) -> None:
        rt = RuntimeContext(custom={"personality": "concise"})
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "concise" in text

    def test_badge_omits_personality_when_helpful(self) -> None:
        # 'helpful' is the implicit default — don't clutter the badge with it.
        rt = RuntimeContext(custom={"personality": "helpful"})
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "helpful" not in text

    def test_badge_includes_persona_when_set(self) -> None:
        rt = RuntimeContext(custom={"active_persona_id": "coder"})
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "coder" in text
```

- [ ] **Step 2.2: Update `_render_mode_badge`**

Append optional persona / personality segments. Concise format:
`[D] mode: default · persona: coder · personality: concise   Shift+Tab to cycle`

- [ ] **Step 2.3: Run + commit**

```
pytest tests/test_mode_badge.py -v
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_mode_badge.py
git commit -m "feat(tui): mode badge surfaces persona + personality when set"
```

---

## Self-audit

**Audit angle 1 — placeholder scan:** All steps have real code. ✓
**Audit angle 2 — type consistency:** `personality: str = ""` consistent across PromptContext / build / build_with_memory / loop. ✓
**Audit angle 3 — branch interaction:** What if user sets `/personality concise` AND there's an active persona overlay? Both should fire — they're independent axes. The Jinja branch and the persona_overlay branch don't overlap. ✓
**Audit angle 4 — backward compat:** Existing tests don't pass `personality=` so default `""` keeps current behavior. ✓
**Audit angle 5 — `loop._active_persona_id` vs `runtime.custom["active_persona_id"]`:** The persona id lives on the loop, not in the runtime. Badge needs to read from loop OR we need to expose it via `runtime.custom`. Solution: when the badge is wired, the loop already exposes the runtime — add `loop` reference too, OR let the loop mirror `_active_persona_id` into `runtime.custom["active_persona_id"]` at session start. **Option chosen:** mirror to `runtime.custom["active_persona_id"]` so the badge stays runtime-only, no new wiring needed.

---

## Verification

1. `pytest OpenComputer/tests/ -q` — all green.
2. `ruff check OpenComputer/` — clean.
3. Manual TUI smoke: `/personality concise`, observe model becomes terser. Badge updates to show `personality: concise`.
4. Manual TUI smoke: switch personas via auto-classifier (or fake one with `runtime.custom["active_persona_id"] = "coder"`), badge shows `persona: coder`.
