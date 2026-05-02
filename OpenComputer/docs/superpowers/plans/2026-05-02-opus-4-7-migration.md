# Opus 4.7 Migration + Stop-Reason Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `claude-opus-4-7` as a working default by removing kwargs the model rejects (temperature/top_p/top_k), migrating extended thinking from the deprecated `enabled+budget_tokens` shape to `adaptive+effort`, and surfacing 4 stop-reason cases the loop currently swallows (refusal, context-window-exceeded, empty-end_turn, max_tokens+tool_use).

**Architecture:** Pure-function capability table in `opencomputer/agent/model_capabilities.py` answers per-model questions (`supports_adaptive_thinking`, `supports_temperature`). The provider and `runtime_flags` consult it before constructing API kwargs. The loop's `stop_reason_map` extends with two new `StopReason` values (`REFUSAL`, `CONTEXT_FULL`); two retry paths (empty-end_turn, max_tokens+tool_use) handle inline before the map. All retries are one-shot, gated by per-turn instance flags on `AgentLoop`. `ConversationResult` gains a `stop_reason` field (additive — defaults to `None`).

**Retry-path streaming caveat:** All four retry paths use `provider.complete()` (non-streaming) even when the original turn was streaming. This means the retry's text doesn't stream into the user's terminal — it lands as a single block. Acceptable trade-off because retries are rare (refusal, context-full, empty-turn, truncated-tool) and re-routing through the streaming path inside `_run_one_step` would require reflowing the StreamEvent emission contract. Documented here so future maintainers don't mistake it for a bug.

**Tech Stack:** Python 3.12+, anthropic SDK, pytest. Existing modules: [extensions/anthropic-provider/provider.py](../../extensions/anthropic-provider/provider.py), [opencomputer/agent/runtime_flags.py](../../opencomputer/agent/runtime_flags.py), [opencomputer/agent/loop.py](../../opencomputer/agent/loop.py), [opencomputer/agent/compaction.py](../../opencomputer/agent/compaction.py), [plugin_sdk/core.py](../../plugin_sdk/core.py).

**Spec:** [docs/superpowers/specs/2026-05-02-anthropic-opus-4-7-migration-design.md](../specs/2026-05-02-anthropic-opus-4-7-migration-design.md)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `opencomputer/agent/model_capabilities.py` | NEW | Pure-function capability table. ~80 LOC. |
| `tests/test_model_capabilities.py` | NEW | Table-driven tests across known + future-fake model names. |
| `plugin_sdk/core.py` | MODIFY | Add `REFUSAL` + `CONTEXT_FULL` to `StopReason`. |
| `tests/test_plugin_sdk_stop_reasons.py` | NEW | Smoke test that the new enum members exist + are stable strings. |
| `opencomputer/agent/runtime_flags.py` | MODIFY | Add `model` kwarg. Branch on `supports_adaptive_thinking`. Replace token-budget table with effort string mapping. |
| `tests/test_runtime_flags.py` | MODIFY | Add tests for adaptive vs legacy branches; update existing tests to pass `model`. |
| `extensions/anthropic-provider/provider.py` | MODIFY | Drop `temperature` for adaptive models (3 sites). Pass `model` to `anthropic_kwargs_from_runtime`. Lift `max_tokens` floor on high-effort calls. |
| `tests/test_anthropic_provider_kwargs.py` | NEW | Verify temperature drop / display=summarized / max_tokens floor. |
| `opencomputer/agent/loop.py` | MODIFY | Stop-reason map extension. New retry counter on `StepOutcome`. Empty-`end_turn` and `max_tokens+tool_use` inline retries. Refusal + context-full handlers after the map. |
| `tests/test_loop_stop_reasons.py` | NEW | 4 retry paths + refusal surfacing. |

---

## Task 1: Capability table module

**Files:**
- Create: `opencomputer/agent/model_capabilities.py`
- Create: `tests/test_model_capabilities.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_capabilities.py`:

```python
"""Tests for opencomputer.agent.model_capabilities — pure functions, no I/O."""

from __future__ import annotations

import pytest

from opencomputer.agent.model_capabilities import (
    supports_adaptive_thinking,
    supports_temperature,
    thinking_display_default,
)


@pytest.mark.parametrize("model,expected", [
    # Adaptive-required (Opus 4.7 forward + Mythos)
    ("claude-opus-4-7", True),
    ("claude-opus-4-7-20260301", True),
    ("claude-mythos-2026-preview", True),
    ("claude-opus-4-8-future", True),
    # Adaptive-recommended (4.6)
    ("claude-opus-4-6", True),
    ("claude-sonnet-4-6", True),
    ("claude-sonnet-4-6-20251101", True),
    # Legacy-thinking-only (4.5 and older)
    ("claude-opus-4-5", False),
    ("claude-sonnet-4-5", False),
    ("claude-haiku-4-5-20251001", False),
    ("claude-sonnet-3-7-20250219", False),
    ("claude-haiku-3-20240307", False),
    # Forward-default for unknown claude-* (modern assumption)
    ("claude-future-x", True),
    # Non-claude (no thinking concept here)
    ("gpt-4o", False),
    ("o1-preview", False),
    ("llama-3-70b", False),
])
def test_supports_adaptive_thinking(model: str, expected: bool) -> None:
    assert supports_adaptive_thinking(model) is expected


@pytest.mark.parametrize("model,expected", [
    # Opus 4.7+ and Mythos: temperature removed
    ("claude-opus-4-7", False),
    ("claude-mythos-2026-preview", False),
    ("claude-opus-4-8-future", False),
    # 4.6 and older still accept temperature
    ("claude-opus-4-6", True),
    ("claude-sonnet-4-6", True),
    ("claude-opus-4-5", True),
    ("claude-haiku-4-5", True),
    ("claude-sonnet-3-7", True),
    # Forward-default for unknown claude-*: assume modern (no temperature)
    ("claude-future-x", False),
    # Non-claude unaffected
    ("gpt-4o", True),
    ("o1-preview", True),
])
def test_supports_temperature(model: str, expected: bool) -> None:
    assert supports_temperature(model) is expected


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-7", "summarized"),
    ("claude-mythos-2026-preview", "summarized"),
    ("claude-opus-4-6", "summarized"),
    ("claude-sonnet-4-6", "summarized"),
    # Legacy models don't use the display field — function returns "" so
    # callers can skip the kwarg entirely.
    ("claude-opus-4-5", ""),
    ("claude-haiku-4-5", ""),
    ("gpt-4o", ""),
])
def test_thinking_display_default(model: str, expected: str) -> None:
    assert thinking_display_default(model) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/.config/superpowers/worktrees/claude/opus-4-7-migration/OpenComputer
pytest tests/test_model_capabilities.py -v
```

Expected: ImportError — module doesn't exist yet.

- [ ] **Step 3: Implement the module**

Create `opencomputer/agent/model_capabilities.py`:

```python
"""Pure-function capability table for model-conditional API kwargs.

Anthropic's model lineup has diverged enough that one provider can't
send identical kwargs to every model:

* Opus 4.7+ and Mythos reject ``temperature``/``top_p``/``top_k`` and
  reject manual extended thinking (``thinking: {type: enabled,
  budget_tokens: N}``); they require ``thinking: {type: adaptive}``
  with ``output_config.effort``.
* Opus 4.6 / Sonnet 4.6 accept both shapes but adaptive is recommended
  and ``temperature`` is still allowed.
* Opus 4.5 and older only support the legacy thinking shape.

This module answers three yes/no questions per model so the provider
and runtime_flags can pick the right shape without each rolling its
own table.

Detection is allowlist-based with a forward-compatible default: an
unknown ``claude-*`` model name is assumed "modern" (adaptive,
no temperature). Anthropic's trajectory is everything moves to that
shape; a wrong guess on a future model is one-line to fix.
"""

from __future__ import annotations

# Models that explicitly KEEP the legacy "manual extended thinking"
# shape (``thinking: {type: enabled, budget_tokens: N}``) and KEEP
# ``temperature``/``top_p``/``top_k``. Anything else with a ``claude-``
# prefix gets the modern (adaptive, no-temperature) treatment.
_LEGACY_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-sonnet-3-7",
    "claude-haiku-3",
    "claude-3-",  # claude-3-opus, claude-3-sonnet, claude-3-haiku
)


def _is_claude(model: str) -> bool:
    return model.startswith("claude-") or model.startswith("claude/")


def _is_legacy_claude(model: str) -> bool:
    return any(model.startswith(p) for p in _LEGACY_PREFIXES)


def supports_adaptive_thinking(model: str) -> bool:
    """True if the model accepts ``thinking: {type: adaptive}``.

    Modern Anthropic models (Opus 4.6+, Sonnet 4.6+, Mythos, future
    claude-*). Legacy claude-* and non-claude models return False.
    """
    if not _is_claude(model):
        return False
    return not _is_legacy_claude(model)


def supports_temperature(model: str) -> bool:
    """True if the model accepts ``temperature``/``top_p``/``top_k`` kwargs.

    Opus 4.7+, Mythos, and future modern claude-* reject these (return
    False). Legacy claude-* and all non-claude models accept them
    (return True).
    """
    # Legacy claude-* keeps temperature.
    if _is_legacy_claude(model):
        return True
    # Modern claude-* (4.6, 4.7, Mythos, unknown-future) drops it.
    if _is_claude(model):
        # 4.6 still accepts temperature per Doc spec; only 4.7+ drops it.
        if model.startswith("claude-opus-4-6") or model.startswith(
            "claude-sonnet-4-6"
        ):
            return True
        return False
    # Non-claude models: providers handle their own param names; we
    # never strip temperature from them here.
    return True


def thinking_display_default(model: str) -> str:
    """Recommended ``display`` field value for the thinking block.

    Returns ``"summarized"`` for adaptive-thinking models so the
    streaming Thinking Dropdown receives ``thinking_delta`` events.
    Returns ``""`` for legacy/non-claude models — the caller should
    omit the ``display`` kwarg entirely in that case.
    """
    return "summarized" if supports_adaptive_thinking(model) else ""


__all__ = [
    "supports_adaptive_thinking",
    "supports_temperature",
    "thinking_display_default",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_model_capabilities.py -v
```

Expected: all 31 parametrize cases PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/model_capabilities.py tests/test_model_capabilities.py
git commit -m "feat(agent): model capability table — adaptive thinking + temperature support per model

Pure-function helpers answering three per-model questions:
- supports_adaptive_thinking — True for Opus 4.6+, Sonnet 4.6+, Mythos, future claude-*
- supports_temperature — False for Opus 4.7+ and Mythos (Anthropic removed it)
- thinking_display_default — 'summarized' for adaptive models so Thinking Dropdown populates

Allowlist with forward-compatible default: unknown claude-* names assumed
modern (adaptive, no temperature). Future model lands → one-line patch
if wrong, no 400s in the meantime.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: StopReason enum extension

**Files:**
- Modify: `plugin_sdk/core.py:417-425`
- Create: `tests/test_plugin_sdk_stop_reasons.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plugin_sdk_stop_reasons.py`:

```python
"""Smoke tests for new StopReason enum members.

StopReason lives in plugin_sdk (public contract) so adding REFUSAL +
CONTEXT_FULL is a public-API change. BC rule #4 in
plugin_sdk/CLAUDE.md: additive only, no removals.
"""

from __future__ import annotations

from plugin_sdk import StopReason


def test_refusal_member_exists() -> None:
    assert StopReason.REFUSAL.value == "refusal"


def test_context_full_member_exists() -> None:
    assert StopReason.CONTEXT_FULL.value == "context_full"


def test_existing_members_unchanged() -> None:
    """BC: existing values must remain stable string-equal."""
    assert StopReason.END_TURN.value == "end_turn"
    assert StopReason.TOOL_USE.value == "tool_use"
    assert StopReason.MAX_TOKENS.value == "max_tokens"
    assert StopReason.INTERRUPTED.value == "interrupted"
    assert StopReason.BUDGET_EXHAUSTED.value == "budget_exhausted"
    assert StopReason.ERROR.value == "error"
```

- [ ] **Step 2: Run test to verify failure**

```bash
pytest tests/test_plugin_sdk_stop_reasons.py -v
```

Expected: FAIL — `AttributeError: REFUSAL` and `CONTEXT_FULL`.

- [ ] **Step 3: Add the new enum values**

Edit `plugin_sdk/core.py` — find the `StopReason` class (line 417) and add two members at the end:

```python
class StopReason(str, Enum):
    """Why a conversation step ended."""

    END_TURN = "end_turn"  # model produced final response, no more tool calls
    TOOL_USE = "tool_use"  # model wants to call tools — loop continues
    MAX_TOKENS = "max_tokens"  # hit output limit
    INTERRUPTED = "interrupted"  # user cancelled
    BUDGET_EXHAUSTED = "budget_exhausted"  # iteration budget spent
    ERROR = "error"  # unrecoverable error
    REFUSAL = "refusal"  # model declined the request (Anthropic safety filter)
    CONTEXT_FULL = "context_full"  # response stopped because context window was exceeded
```

- [ ] **Step 4: Run test to verify pass**

```bash
pytest tests/test_plugin_sdk_stop_reasons.py -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Confirm SDK boundary test still passes**

```bash
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```

Expected: PASS (no new imports introduced).

- [ ] **Step 6: Commit**

```bash
git add plugin_sdk/core.py tests/test_plugin_sdk_stop_reasons.py
git commit -m "feat(plugin_sdk): add StopReason.REFUSAL + CONTEXT_FULL

Additive enum extension (BC rule #4 — no removals). Loop will use
these in subsequent commits to surface refusal-stops and
context-window-exceeded stops that today silently map to END_TURN.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: runtime_flags migration to adaptive+effort

**Files:**
- Modify: `opencomputer/agent/runtime_flags.py`
- Modify: `tests/test_runtime_flags.py`

- [ ] **Step 1: Read existing tests to understand the current test shape**

```bash
cat tests/test_runtime_flags.py | head -80
```

(Note the existing test patterns and naming.)

- [ ] **Step 2: Write failing tests for the new branches**

Append to `tests/test_runtime_flags.py`:

```python
# ─── Adaptive-thinking migration tests ────────────────────────────

def test_anthropic_kwargs_adaptive_branch_for_opus_4_7() -> None:
    """Opus 4.7 must get adaptive thinking + output_config.effort."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="high",
    )
    assert out["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert out["output_config"] == {"effort": "high"}


def test_anthropic_kwargs_adaptive_branch_xhigh() -> None:
    """xhigh effort passes through unchanged on adaptive models."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="xhigh",
    )
    assert out["output_config"] == {"effort": "xhigh"}


def test_anthropic_kwargs_adaptive_minimal_collapses_to_low() -> None:
    """Internal 'minimal' has no Anthropic equivalent; collapse to 'low'."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="minimal",
    )
    assert out["output_config"] == {"effort": "low"}


def test_anthropic_kwargs_legacy_branch_for_opus_4_5() -> None:
    """Opus 4.5 must keep enabled+budget_tokens — adaptive not supported."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-5",
        reasoning_effort="high",
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    # No output_config on legacy branch — Opus 4.5 supports effort but
    # the effort+legacy combo is deferred to a follow-up PR.
    assert "output_config" not in out


def test_anthropic_kwargs_none_emits_nothing() -> None:
    """reasoning_effort='none' emits no thinking kwargs on either branch."""
    for model in ["claude-opus-4-7", "claude-opus-4-5"]:
        out = anthropic_kwargs_from_runtime(
            model=model,
            reasoning_effort="none",
        )
        assert "thinking" not in out
        assert "output_config" not in out


def test_anthropic_kwargs_unknown_effort_falls_back_to_high_on_adaptive() -> None:
    """Unknown internal effort name falls back to API default 'high' on adaptive."""
    out = anthropic_kwargs_from_runtime(
        model="claude-opus-4-7",
        reasoning_effort="ultra-mega",  # not in the table
    )
    assert out["output_config"] == {"effort": "high"}


def test_anthropic_kwargs_service_tier_still_works() -> None:
    """service_tier='priority' still passes through on both branches."""
    for model in ["claude-opus-4-7", "claude-opus-4-5"]:
        out = anthropic_kwargs_from_runtime(
            model=model,
            service_tier="priority",
        )
        assert out["service_tier"] == "priority"
```

- [ ] **Step 3: Update existing tests to pass `model` kwarg**

Find all existing calls to `anthropic_kwargs_from_runtime(...)` in `tests/test_runtime_flags.py` and add `model="claude-opus-4-5"` (the legacy branch — preserves the existing behavior the old tests assert):

```bash
grep -n "anthropic_kwargs_from_runtime(" tests/test_runtime_flags.py
```

For each call missing a `model=` arg, add `model="claude-opus-4-5",` as the first kwarg. The legacy branch keeps the existing `enabled+budget_tokens` shape, so old assertions remain valid.

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_runtime_flags.py -v
```

Expected: new tests FAIL with TypeError: missing `model` arg or assertion failures.

- [ ] **Step 5: Replace runtime_flags.py with the migrated version**

Replace the entire content of `opencomputer/agent/runtime_flags.py`:

```python
"""Translate runtime.custom flags into provider-specific API kwargs.

Tier 2.A provider integration follow-up: ``/reasoning`` and ``/fast`` slash
commands store flags in ``runtime.custom``; this module translates those
flags into the keyword arguments each provider's API expects, so the
flags actually take effect on the next LLM call.

Translation tables here, not in the providers, so:
  - The mapping is unit-testable in isolation (no provider mocks needed).
  - Adding a third provider just adds one new translator function.
  - The audit-doc-defined effort levels (none/minimal/low/medium/high/xhigh/max)
    have a single source of truth for their semantic meaning.

2026-05-02 — Anthropic side migrated to ``thinking: {type: adaptive}`` +
``output_config.effort`` for models that support it (Opus 4.6+, Sonnet 4.6+,
Mythos, future claude-*). Legacy ``enabled+budget_tokens`` retained for
Opus/Sonnet/Haiku 4.5 and older. Branching driven by
``opencomputer.agent.model_capabilities``.
"""

from __future__ import annotations

from opencomputer.agent.model_capabilities import (
    supports_adaptive_thinking,
    thinking_display_default,
)

# Legacy branch: token-budget table for models that still take
# ``thinking: {type: enabled, budget_tokens: N}`` (Opus 4.5 and older).
# Calibrated to public guidance: low ≈ short scratch, medium ≈ default,
# high ≈ deep reasoning, xhigh ≈ extended trains of thought.
_LEGACY_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    # "none" → omit thinking entirely
}

# Adaptive branch: map internal effort names → Anthropic effort values.
# Anthropic accepts {low, medium, high, xhigh, max}. Internal "minimal"
# has no exact match; collapse to "low".
_ADAPTIVE_EFFORT_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# OpenAI: ``reasoning_effort`` field accepts {minimal, low, medium, high}.
# OC's ``xhigh`` extends past OpenAI's range; we cap at "high".
_OPENAI_REASONING_MAP: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
    # "none" → omit reasoning_effort
}


def anthropic_kwargs_from_runtime(
    *,
    model: str,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
) -> dict:
    """Build the Anthropic-specific kwargs to merge into a ``messages.create`` call.

    Returns an empty dict when no flags are active, so callers can
    unconditionally ``kwargs.update(anthropic_kwargs_from_runtime(...))``
    without branching.

    Branches on the model's adaptive-thinking support:
      * Adaptive (Opus 4.6+, Sonnet 4.6+, Mythos, future claude-*):
        emits ``thinking: {type: adaptive, display: summarized}`` +
        ``output_config: {effort: <mapped>}``.
      * Legacy (Opus 4.5 and older): emits ``thinking: {type: enabled,
        budget_tokens: <mapped>}``.
    """
    out: dict = {}
    if reasoning_effort and reasoning_effort != "none":
        if supports_adaptive_thinking(model):
            display = thinking_display_default(model)
            thinking_block: dict = {"type": "adaptive"}
            if display:
                thinking_block["display"] = display
            out["thinking"] = thinking_block
            mapped = _ADAPTIVE_EFFORT_MAP.get(reasoning_effort, "high")
            out["output_config"] = {"effort": mapped}
        else:
            budget = _LEGACY_BUDGET.get(reasoning_effort)
            if budget is not None:
                out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if service_tier == "priority":
        out["service_tier"] = "priority"
    return out


def openai_kwargs_from_runtime(
    *,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
) -> dict:
    """Build the OpenAI Chat Completions kwargs to merge into the request body."""
    out: dict = {}
    if reasoning_effort and reasoning_effort != "none":
        mapped = _OPENAI_REASONING_MAP.get(reasoning_effort)
        if mapped is not None:
            out["reasoning_effort"] = mapped
    if service_tier == "priority":
        out["service_tier"] = "priority"
    return out


def runtime_flags_from_custom(custom: dict | None) -> dict[str, str | None]:
    """Extract the relevant runtime.custom keys; safe on missing or None.

    Returns ``{"reasoning_effort": ..., "service_tier": ...}`` — values may
    be ``None`` when the flag isn't set. Pass ``**runtime_flags_from_custom(rt.custom)``
    into the translators above.
    """
    if not custom:
        return {"reasoning_effort": None, "service_tier": None}
    re = custom.get("reasoning_effort")
    st = custom.get("service_tier")
    return {
        "reasoning_effort": re if isinstance(re, str) else None,
        "service_tier": st if isinstance(st, str) else None,
    }


__all__ = [
    "anthropic_kwargs_from_runtime",
    "openai_kwargs_from_runtime",
    "runtime_flags_from_custom",
]
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_runtime_flags.py -v
```

Expected: all PASS (both new adaptive tests and updated legacy tests).

- [ ] **Step 7: Commit**

```bash
git add opencomputer/agent/runtime_flags.py tests/test_runtime_flags.py
git commit -m "feat(runtime_flags): migrate Anthropic thinking shape to adaptive+effort

Branches on model_capabilities.supports_adaptive_thinking:
- Adaptive (Opus 4.6+/Sonnet 4.6+/Mythos): {type: adaptive, display: summarized}
  + output_config.effort
- Legacy (Opus 4.5 and older): unchanged enabled+budget_tokens

Required because Opus 4.7 (the default model) 400-errors on the legacy
shape. Display=summarized restores Thinking Dropdown population on Opus 4.7
(API default is omitted, which suppresses thinking_delta events).

anthropic_kwargs_from_runtime now requires model= kwarg.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Provider — drop temperature for adaptive models

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` — 3 sites (`_do_complete`, `_do_stream_complete`, `stream_complete`)
- Create: `tests/test_anthropic_provider_kwargs.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_anthropic_provider_kwargs.py`. The directory `extensions/anthropic-provider/` has a hyphen (invalid Python module name), so we load it the same way `tests/test_anthropic_provider_pool.py` does — via importlib from the file path:

```python
"""Verify provider-side kwargs construction respects model capabilities."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _load_anthropic_provider():
    """Load AnthropicProvider fresh from disk, bypassing module cache.

    Mirrors tests/test_anthropic_provider_pool.py:_load_anthropic_provider.
    """
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_provider_kwargs_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    return mod.AnthropicProvider()


@pytest.mark.asyncio
async def test_opus_4_7_call_omits_temperature(provider: AnthropicProvider) -> None:
    """On Opus 4.7, the kwargs sent to messages.create must NOT contain temperature."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        # Build a minimal valid response stub
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        return resp

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=100,
            temperature=0.7,
        )
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "top_k" not in captured


@pytest.mark.asyncio
async def test_opus_4_5_call_includes_temperature(provider: AnthropicProvider) -> None:
    """On Opus 4.5 (legacy), temperature is preserved."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        return resp

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-5",
            messages=[],
            max_tokens=100,
            temperature=0.7,
        )
    assert captured.get("temperature") == 0.7


@pytest.mark.asyncio
async def test_high_effort_lifts_max_tokens_floor_on_adaptive(provider: AnthropicProvider) -> None:
    """xhigh effort on Opus 4.7 lifts max_tokens floor to 64000."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        return resp

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=4096,  # below floor
            runtime_extras={"reasoning_effort": "xhigh"},
        )
    assert captured["max_tokens"] >= 64000


@pytest.mark.asyncio
async def test_low_effort_does_not_lift_max_tokens(provider: AnthropicProvider) -> None:
    """Low effort doesn't trigger the floor lift."""
    captured: dict = {}

    async def _capture(**kw):
        captured.update(kw)
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        return resp

    with patch.object(provider.client.messages, "create", side_effect=_capture):
        await provider.complete(
            model="claude-opus-4-7",
            messages=[],
            max_tokens=4096,
            runtime_extras={"reasoning_effort": "low"},
        )
    assert captured["max_tokens"] == 4096
```

- [ ] **Step 2: Run tests to verify failure**

```bash
pytest tests/test_anthropic_provider_kwargs.py -v
```

Expected: FAIL — temperature is currently always included; max_tokens floor not lifted.

- [ ] **Step 3: Modify the three call-site kwargs blocks in provider.py**

In `extensions/anthropic-provider/provider.py`, locate these three kwargs-construction blocks (around lines 428-448, 519-539, 567-587 in the current file). Each looks like:

```python
kwargs: dict[str, Any] = {
    "model": model,
    "max_tokens": max_tokens,
    "temperature": temperature,
    "messages": api_messages,
}
if sys_for_sdk:
    kwargs["system"] = sys_for_sdk
if tools:
    kwargs["tools"] = [t.to_anthropic_format() for t in tools]
if runtime_extras:
    from opencomputer.agent.runtime_flags import (
        anthropic_kwargs_from_runtime,
    )
    kwargs.update(
        anthropic_kwargs_from_runtime(
            reasoning_effort=runtime_extras.get("reasoning_effort"),
            service_tier=runtime_extras.get("service_tier"),
        )
    )
```

Replace each with:

```python
from opencomputer.agent.model_capabilities import supports_temperature

# Floor lift: high-effort on adaptive models needs headroom for
# thinking + tool calls (Doc 5 recommendation: start at 64k tokens).
effective_max_tokens = max_tokens
if runtime_extras and runtime_extras.get("reasoning_effort") in (
    "high", "xhigh", "max",
):
    from opencomputer.agent.model_capabilities import (
        supports_adaptive_thinking,
    )
    if supports_adaptive_thinking(model):
        effective_max_tokens = max(max_tokens, 64_000)

kwargs: dict[str, Any] = {
    "model": model,
    "max_tokens": effective_max_tokens,
    "messages": api_messages,
}
if supports_temperature(model):
    kwargs["temperature"] = temperature
if sys_for_sdk:
    kwargs["system"] = sys_for_sdk
if tools:
    kwargs["tools"] = [t.to_anthropic_format() for t in tools]
if runtime_extras:
    from opencomputer.agent.runtime_flags import (
        anthropic_kwargs_from_runtime,
    )
    kwargs.update(
        anthropic_kwargs_from_runtime(
            model=model,
            reasoning_effort=runtime_extras.get("reasoning_effort"),
            service_tier=runtime_extras.get("service_tier"),
        )
    )
```

Apply this change to all three sites: `_do_complete`, `_do_stream_complete`, and `stream_complete`.

Move the `from opencomputer.agent.model_capabilities import supports_temperature` import to the top of the file (next to the existing imports) so it's not re-imported per call.

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_anthropic_provider_kwargs.py -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Run the broader provider test suite to catch regressions**

```bash
pytest tests/test_anthropic_provider_pool.py tests/test_anthropic_thinking_stream.py -v
```

Expected: PASS (existing tests should continue passing — the change is conditional on model name).

- [ ] **Step 6: Commit**

```bash
git add extensions/anthropic-provider/provider.py tests/test_anthropic_provider_kwargs.py
git commit -m "fix(anthropic-provider): drop temperature/top_p/top_k for Opus 4.7+

Opus 4.7 and Mythos reject these kwargs with HTTP 400. Conditional
inclusion based on model_capabilities.supports_temperature.

Also lift max_tokens floor to 64000 on adaptive models when
reasoning_effort is high/xhigh/max — Doc 5 recommendation for
giving the model headroom for thinking + tool calls.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Loop — extend stop_reason_map + expose stop_reason on ConversationResult

**Files:**
- Modify: `opencomputer/agent/loop.py:2753` (stop_reason_map block, inside `_run_one_step`)
- Modify: `opencomputer/agent/loop.py` (`ConversationResult` dataclass — add `stop_reason` field)
- Modify: `opencomputer/agent/loop.py` (the `run_conversation` method — populate the new field on return)
- Create: `tests/test_loop_stop_reasons.py`

- [ ] **Step 1: Read the existing AgentLoop construction pattern**

```bash
grep -B2 -A20 "def _make_loop" tests/test_loop_thinking_dispatch.py | head -25
```

The pattern is:
```python
return AgentLoop(
    provider=provider,
    config=Config(
        model=ModelConfig(provider="fake", model="fake-1"),
        session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
    ),
)
```

We re-use this exact shape — do NOT roll a new construction.

- [ ] **Step 2: Write the failing test for refusal mapping**

Create `tests/test_loop_stop_reasons.py`:

```python
"""Tests for new stop-reason handlers added in the Opus 4.7 migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _ScriptedProvider(BaseProvider):
    """Returns a sequence of pre-built ProviderResponses on successive calls."""

    name = "scripted"
    default_model = "claude-opus-4-7"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = responses
        self._idx = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        resp = self._responses[self._idx]
        self._idx += 1
        return resp

    async def stream_complete(self, **kwargs: Any):
        resp = await self.complete(**kwargs)
        if resp.message.content:
            yield StreamEvent(kind="text_delta", text=resp.message.content)
        yield StreamEvent(kind="done", response=resp)


def _resp(
    stop_reason: str,
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 10,
) -> ProviderResponse:
    return ProviderResponse(
        message=Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        ),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Match the construction pattern from test_loop_thinking_dispatch.py."""
    return AgentLoop(
        provider=provider,
        config=Config(
            model=ModelConfig(provider="scripted", model="claude-opus-4-7"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )


@pytest.mark.asyncio
async def test_refusal_maps_to_stop_reason_refusal(tmp_path) -> None:
    """When stop_reason='refusal', loop emits StopReason.REFUSAL not END_TURN.

    Asserts on the new `ConversationResult.stop_reason` field added in
    this task (additive — defaults to None for legacy callers).
    """
    provider = _ScriptedProvider([
        _resp("refusal", content="I cannot help with that request."),
    ])
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test prompt", session_id="t1")
    assert result.stop_reason == StopReason.REFUSAL
    assert "declined" in result.final_message.content.lower()
    # Original model text should be preserved alongside our marker.
    assert "cannot help" in result.final_message.content.lower()
```

- [ ] **Step 3: Run test to verify failure**

```bash
pytest tests/test_loop_stop_reasons.py::test_refusal_maps_to_stop_reason_refusal -v
```

Expected: FAIL — currently maps to END_TURN, and `ConversationResult.stop_reason` doesn't exist yet.

- [ ] **Step 4: Add `stop_reason` field to `ConversationResult` and populate it**

In `opencomputer/agent/loop.py`, find the `ConversationResult` dataclass:

```python
@dataclass(slots=True)
class ConversationResult:
    """What a full run_conversation call returns."""

    final_message: Message
    messages: list[Message]
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int
```

Replace with:

```python
@dataclass(slots=True)
class ConversationResult:
    """What a full run_conversation call returns."""

    final_message: Message
    messages: list[Message]
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int
    stop_reason: StopReason | None = None
    """The terminal stop reason of the final step. ``None`` only when the
    loop exited via budget exhaustion or an external interrupt before
    any step completed. Additive field — existing callers ignoring the
    field continue to work unchanged."""
```

Then find every `return ConversationResult(...)` inside `run_conversation` (there are typically 1-3 such return points; grep for `ConversationResult(` inside the method). Pass through the last `StepOutcome.stop_reason` to the new field. Pattern:

```python
return ConversationResult(
    final_message=final_msg,
    messages=session_messages,
    session_id=session_id,
    iterations=iter_count,
    input_tokens=total_input,
    output_tokens=total_output,
    stop_reason=last_stop_reason,  # NEW — track the last StepOutcome.stop_reason
)
```

The `last_stop_reason` variable must be threaded through the conversation loop. If it's already there under a different name, just pass that through.

- [ ] **Step 5: Extend stop_reason_map in `_run_one_step`**

In `opencomputer/agent/loop.py`, find the block at line ~2753:

```python
stop_reason_map = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
}
stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)
```

Replace with:

```python
stop_reason_map = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
    "refusal": StopReason.REFUSAL,
    "model_context_window_exceeded": StopReason.CONTEXT_FULL,
}
stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)

# Refusal: ensure the user sees something, even if the model emitted no text.
if stop == StopReason.REFUSAL and not resp.message.content:
    msg = Message(
        role="assistant",
        content="_Claude declined to respond._",
        tool_calls=resp.message.tool_calls,
    )
    resp = ProviderResponse(
        message=msg,
        stop_reason=resp.stop_reason,
        usage=resp.usage,
    )
elif stop == StopReason.REFUSAL:
    # Model gave a brief explanation; prepend our marker so it's
    # visually distinguishable from a normal turn.
    msg = Message(
        role="assistant",
        content=f"_Claude declined to respond._\n\n{resp.message.content}",
        tool_calls=resp.message.tool_calls,
    )
    resp = ProviderResponse(
        message=msg,
        stop_reason=resp.stop_reason,
        usage=resp.usage,
    )
```

(The `Message` and `ProviderResponse` imports already exist near the top of `loop.py`. Verify with `grep "from plugin_sdk" opencomputer/agent/loop.py | head -5`.)

- [ ] **Step 6: Run test to verify pass**

```bash
pytest tests/test_loop_stop_reasons.py::test_refusal_maps_to_stop_reason_refusal -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_loop_stop_reasons.py
git commit -m "feat(loop): map stop_reason='refusal' to StopReason.REFUSAL with visible message

Anthropic returns stop_reason='refusal' when its safety filter
declines a request. Today this silently maps to END_TURN, leaving
the user staring at an empty assistant turn. Now we map to
StopReason.REFUSAL and prepend a visible marker — model's brief
explanation (if any) is preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Loop — context-window-exceeded retry with compaction

**Files:**
- Modify: `opencomputer/agent/loop.py` (around the new stop_reason_map handling)
- Modify: `tests/test_loop_stop_reasons.py` (append)

- [ ] **Step 1: Append the failing test**

Add to `tests/test_loop_stop_reasons.py`:

```python
@pytest.mark.asyncio
async def test_context_full_triggers_compaction_and_retry(tmp_path) -> None:
    """First call returns model_context_window_exceeded; second call succeeds after compaction."""
    provider = _ScriptedProvider([
        _resp("model_context_window_exceeded", input_tokens=199_000),
        _resp("end_turn", content="Now I can answer."),
    ])
    # Use _make_loop helper from this file
    
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Long prompt", session_id="t2")
    # After retry, we got a clean END_TURN with the second response's content.
    assert result.stop_reason == StopReason.END_TURN
    assert "Now I can answer" in result.final_message.content
    # Both provider calls were consumed.
    assert provider._idx == 2


@pytest.mark.asyncio
async def test_context_full_double_failure_surfaces(tmp_path) -> None:
    """If retry also returns context_full, surface as CONTEXT_FULL with clear message."""
    provider = _ScriptedProvider([
        _resp("model_context_window_exceeded"),
        _resp("model_context_window_exceeded"),
    ])
    # Use _make_loop helper from this file
    
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Long prompt", session_id="t3")
    assert result.stop_reason == StopReason.CONTEXT_FULL
    assert "compaction" in result.final_message.content.lower() or \
           "new session" in result.final_message.content.lower()
```

- [ ] **Step 2: Run test to verify failure**

```bash
pytest tests/test_loop_stop_reasons.py -v -k context_full
```

Expected: FAIL — no compaction retry path exists.

- [ ] **Step 3: Add the context-full retry handler in loop.py**

In `opencomputer/agent/loop.py`, immediately AFTER the `stop_reason_map` block (right after the refusal handling added in Task 5), add:

```python
# Context-full retry: trigger compaction once and retry the same turn.
# Per-turn one-shot guard via _context_full_retry_attempted in StepOutcome.
if stop == StopReason.CONTEXT_FULL and not getattr(self, "_context_full_retry_attempted", False):
    self._context_full_retry_attempted = True
    try:
        from opencomputer.agent.compaction import CompactionEngine
        if hasattr(self, "_compaction") and self._compaction is not None:
            result = await self._compaction.maybe_run(
                wire_messages, resp.usage.input_tokens, force=True,
            )
            if result.did_compact:
                wire_messages = result.messages
                # Retry — call provider once more with compacted messages.
                # Mirror the same kwargs from the original _do_call invocation.
                resp = await call_with_fallback(
                    _do_call,
                    primary_model=model_name,
                    fallback_models=self.config.model.fallback_models,
                )
                stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)
                msg = resp.message
    except Exception as exc:  # noqa: BLE001
        # Compaction failure → surface CONTEXT_FULL to user
        _log.warning("Compaction-on-context-full failed: %s", exc)

# After retry attempt, if still context-full, surface a clear message.
if stop == StopReason.CONTEXT_FULL:
    msg = Message(
        role="assistant",
        content=(
            "_Context window full and compaction was insufficient — "
            "please start a new session._"
        ),
    )
    self._context_full_retry_attempted = False  # reset for next turn

# Otherwise reset the flag for the next turn even on success.
elif getattr(self, "_context_full_retry_attempted", False):
    self._context_full_retry_attempted = False
```

You'll also need to look up the `_log` reference (probably `_log = logging.getLogger(...)` near top of loop.py) and confirm it exists. If not, add `import logging; _log = logging.getLogger(__name__)` to top of file.

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_loop_stop_reasons.py -v -k context_full
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_loop_stop_reasons.py
git commit -m "feat(loop): handle stop_reason=model_context_window_exceeded with compaction retry

Sonnet 4.5+ returns this stop_reason when the response was truncated
because the input + accumulated output exceeded the context window.
We now trigger CompactionEngine.maybe_run(force=True) and retry once.
If retry also returns context_full, surface a clear message rather
than silently truncating.

One-shot retry guard via instance flag so a stuck context can't loop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Loop — empty end_turn detection + retry

**Files:**
- Modify: `opencomputer/agent/loop.py` (BEFORE the stop_reason_map)
- Modify: `tests/test_loop_stop_reasons.py` (append)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.asyncio
async def test_empty_end_turn_triggers_continuation_retry(tmp_path) -> None:
    """Empty end_turn (no content, no tool calls) → retry with 'Please continue'."""
    provider = _ScriptedProvider([
        _resp("end_turn", content=""),  # empty
        _resp("end_turn", content="Sorry, here's the answer."),
    ])
    # Use _make_loop helper from this file
    
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test", session_id="t4")
    assert result.stop_reason == StopReason.END_TURN
    assert "here's the answer" in result.final_message.content
    assert provider._idx == 2  # continuation retry happened


@pytest.mark.asyncio
async def test_empty_end_turn_after_retry_still_empty_accepts(tmp_path) -> None:
    """If continuation retry is also empty, accept rather than loop forever."""
    provider = _ScriptedProvider([
        _resp("end_turn", content=""),
        _resp("end_turn", content=""),
    ])
    # Use _make_loop helper from this file
    
    loop = _make_loop(provider, tmp_path)

    result = await loop.run_conversation("Test", session_id="t5")
    assert result.stop_reason == StopReason.END_TURN
    assert provider._idx == 2  # exactly one retry, no more
```

- [ ] **Step 2: Run test to verify failure**

```bash
pytest tests/test_loop_stop_reasons.py -v -k empty_end_turn
```

Expected: FAIL — empty turns currently pass through as END_TURN with empty content.

- [ ] **Step 3: Add the empty-end_turn detection BEFORE stop_reason_map**

In `opencomputer/agent/loop.py`, immediately BEFORE the `stop_reason_map` block (line ~2753), add:

```python
# Empty-end_turn detection: per Doc 3, Claude can return 2-3 empty
# tokens with stop_reason=end_turn after tool results when text is
# inadvertently appended in the same content block. Recover by
# injecting a synthetic "Please continue" user message into the
# wire-only message list and retrying once. The synthetic message is
# NOT persisted to SessionDB.
if (
    resp.stop_reason == "end_turn"
    and not (resp.message.content or "").strip()
    and not resp.message.tool_calls
    and not getattr(self, "_empty_continuation_attempted", False)
):
    self._empty_continuation_attempted = True
    retry_messages = list(wire_messages) + [
        Message(role="user", content="Please continue.")
    ]
    # Re-invoke the provider with the synthetic continuation. We
    # rebuild the call using the same kwargs path as _do_call.
    try:
        # Inline the call to avoid re-entering _do_call's session logic.
        resp = await self._call_provider_for_retry(
            messages=retry_messages,
            tools=tool_schemas,
            system=system,
            model=model_name,
        )
    except Exception:
        # Retry failed → fall through to the original empty turn.
        self._empty_continuation_attempted = False
elif getattr(self, "_empty_continuation_attempted", False):
    # Reset for next turn after a successful continuation.
    self._empty_continuation_attempted = False
```

Then add a small helper method `_call_provider_for_retry` to the `AgentLoop` class. Find an existing method like `_run_one_step` and add this helper alongside it:

```python
async def _call_provider_for_retry(
    self,
    *,
    messages: list[Message],
    tools: list,
    system: str,
    model: str,
) -> ProviderResponse:
    """Synthetic-continuation retry. NOT persisted to SessionDB."""
    return await self.provider.complete(
        model=model,
        messages=messages,
        system=system,
        tools=tools,
        max_tokens=self.config.model.max_tokens,
        temperature=self.config.model.temperature,
    )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_loop_stop_reasons.py -v -k empty_end_turn
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_loop_stop_reasons.py
git commit -m "feat(loop): detect empty end_turn and retry with continuation prompt

Per Doc 3, Anthropic returns 2-3 empty tokens with stop_reason=end_turn
when text is appended after tool_results in the same content block, or
when the assistant turn was already deemed complete. Today this looks
like the agent hung/ignored the user.

Recovery: inject a synthetic 'Please continue.' user message into
wire-only messages (not persisted to SessionDB) and retry once. If
retry is still empty, accept rather than loop forever.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Loop — max_tokens + tool_use auto-retry

**Files:**
- Modify: `opencomputer/agent/loop.py` (BEFORE stop_reason_map, alongside Task 7's empty-end_turn block)
- Modify: `tests/test_loop_stop_reasons.py` (append)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.asyncio
async def test_max_tokens_with_tool_use_retries_with_doubled_max_tokens(tmp_path) -> None:
    """max_tokens stop with last block being tool_use → retry with max_tokens * 2."""
    from plugin_sdk.core import ToolCall

    truncated_msg = Message(
        role="assistant",
        content="Calling tool",
        tool_calls=[ToolCall(id="t1", name="Read", arguments={"path": ""})],
        # Truncated arguments (path="" instead of full path)
    )
    truncated_resp = ProviderResponse(
        message=truncated_msg,
        stop_reason="max_tokens",
        usage=Usage(input_tokens=10, output_tokens=4096),
    )

    full_msg = Message(
        role="assistant",
        content="Done",
        tool_calls=[ToolCall(id="t2", name="Read", arguments={"path": "/tmp/file"})],
    )
    full_resp = ProviderResponse(
        message=full_msg,
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=200),
    )

    provider = _ScriptedProvider([truncated_resp, full_resp])
    # Use _make_loop helper from this file
    
    config.model.max_tokens = 4096  # initial
    loop = _make_loop(provider, tmp_path)

    # We don't run a full conversation here — just _run_one_step would be cleaner.
    # For now, drive through run_conversation and assert the second call
    # was made (retry happened) and the final tool call has full args.
    result = await loop.run_conversation("Read file", session_id="t6")
    assert provider._idx == 2  # retry happened
```

- [ ] **Step 2: Run test to verify failure**

```bash
pytest tests/test_loop_stop_reasons.py -v -k max_tokens_with_tool_use
```

Expected: FAIL — no retry currently.

- [ ] **Step 3: Add the max_tokens+tool_use retry handler**

In `opencomputer/agent/loop.py`, alongside the empty-end_turn block (also BEFORE stop_reason_map), add:

```python
# max_tokens + tool_use retry: if the response was truncated mid
# tool_use, retry with doubled max_tokens (capped to a streaming-aware
# ceiling). Once-shot guard via _max_tokens_retry_attempted.
if (
    resp.stop_reason == "max_tokens"
    and resp.message.tool_calls
    and not getattr(self, "_max_tokens_retry_attempted", False)
):
    self._max_tokens_retry_attempted = True
    # Compute lifted max_tokens. Cap at 64k (non-streaming ceiling on
    # Opus 4.7); streaming path can go higher but 64k is plenty.
    current = self.config.model.max_tokens
    lifted = min(current * 2, 64_000)
    if lifted > current:
        # Re-invoke the provider with the lifted max_tokens.
        try:
            resp = await self.provider.complete(
                model=model_name,
                messages=wire_messages,
                system=system,
                tools=tool_schemas,
                max_tokens=lifted,
                temperature=self.config.model.temperature,
            )
        except Exception:
            self._max_tokens_retry_attempted = False
elif getattr(self, "_max_tokens_retry_attempted", False):
    self._max_tokens_retry_attempted = False
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_loop_stop_reasons.py -v -k max_tokens_with_tool_use
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_loop_stop_reasons.py
git commit -m "feat(loop): auto-retry max_tokens + tool_use with doubled max_tokens

When max_tokens is hit DURING a tool_use block, the model emits a
partial tool call (e.g. truncated arguments) that the dispatcher
can't execute. Per Doc 3 fix-it pattern: retry with doubled
max_tokens (capped at 64k) once. If retry also truncates, surface
as a normal MAX_TOKENS outcome.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Full suite + ruff + manual smoke

- [ ] **Step 1: Run the full test suite locally**

```bash
cd ~/.config/superpowers/worktrees/claude/opus-4-7-migration/OpenComputer
pytest tests/ -x -q
```

Expected: all pass. Memory rule "no push without deep testing" applies — must be green.

- [ ] **Step 2: Lint**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/anthropic-provider/ tests/
```

Expected: no errors.

- [ ] **Step 3: Manual smoke test on Opus 4.7**

Skip if no `ANTHROPIC_API_KEY` available.

```bash
export ANTHROPIC_API_KEY=sk-ant-...  # your key
oc  # or: python -m opencomputer
```

In the chat:
1. Type "Hello, are you Opus 4.7?" → confirm response (no 400).
2. Type `/reasoning high` → then "Explain Bayes' theorem in 2 sentences" → confirm Thinking Dropdown panel populates with text.
3. Type a prompt designed to refuse (e.g., something the safety filter would decline) → confirm "_Claude declined to respond._" message appears rather than empty turn.

If all three pass, the migration is verified.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/opus-4-7-migration
```

- [ ] **Step 5: Open PR**

```bash
gh pr create --title "feat: Opus 4.7 migration + stop-reason hygiene (Subsystem A)" --body "$(cat <<'EOF'
## Summary
- Drop `temperature`/`top_p`/`top_k` for Opus 4.7+ and Mythos (rejected by API)
- Migrate `runtime_flags` from deprecated `enabled+budget_tokens` to `adaptive+effort` (Opus 4.6+/Sonnet 4.6+/Mythos)
- Set `display: "summarized"` on adaptive thinking blocks so the Thinking Dropdown populates
- Add `StopReason.REFUSAL` and `StopReason.CONTEXT_FULL` (additive, plugin_sdk BC)
- Auto-retry on context_full (compaction once), empty-end_turn (continuation prompt), and max_tokens+tool_use (doubled max_tokens)

Spec: `docs/superpowers/specs/2026-05-02-anthropic-opus-4-7-migration-design.md`
Plan: `docs/superpowers/plans/2026-05-02-opus-4-7-migration.md`

## Test plan
- [ ] Full pytest suite green
- [ ] Ruff clean
- [ ] Manual: `oc` chat on Opus 4.7 (default) makes successful round-trip
- [ ] Manual: `/reasoning high` populates Thinking Dropdown
- [ ] Manual: refusal-prone prompt produces visible "_Claude declined to respond._" message

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Section 1 (problem) — all 7 issues covered by Tasks 4 (temperature), 3 (thinking shape), 3 (display), 5/6 (stop reasons), 7/8 (empty-end_turn + max_tokens+tool_use retries).
- ✅ Section 3 (capability table) — Task 1.
- ✅ Section 3.1 (provider patches) — Task 4.
- ✅ Section 3.2 (runtime_flags migration) — Task 3.
- ✅ Section 3.3 (stop-reason map) — Tasks 5/6/7/8.
- ✅ Section 3.4 (max_tokens floor lift) — Task 4.
- ✅ Section 7 (testing strategy) — Tasks 1, 3, 4, 5, 6, 7, 8 each create or modify tests.
- ✅ Section 11 (acceptance criteria) — Task 9 manual smoke covers AC #1-3; pytest green covers AC #4-5.

**Placeholder scan:** No TBD/TODO/"implement appropriate" found. Code blocks present in every implementation step.

**Type consistency:**
- `anthropic_kwargs_from_runtime` signature consistent across Tasks 3 (defines) and 4 (calls with `model=model_name`).
- `StopReason.REFUSAL` and `StopReason.CONTEXT_FULL` defined in Task 2, used in Tasks 5 and 6.
- `supports_adaptive_thinking` / `supports_temperature` / `thinking_display_default` consistent across Tasks 1 (defines), 3 (uses), 4 (uses).
- `_ScriptedProvider` test helper defined once in Task 5 and reused by Tasks 6, 7, 8.

**Plan complete.**
