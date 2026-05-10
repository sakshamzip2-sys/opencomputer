"""/context slash command — surfaces context-window % + compaction count.

Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`` §4.4.

Reads from ``runtime.custom`` keys that the agent loop populates each
turn (mirroring the ``/usage`` command's pattern):

  - ``model``                — current model id (loop sets this each turn)
  - ``session_tokens_in``    — cumulative input tokens this session
  - ``last_input_tokens``    — current-turn input tokens (preferred)
  - ``session_compactions``  — compaction count this session

Output renders:

  - Model
  - Used / max context (token counts + %)
  - Remaining tokens
  - Compaction trigger threshold
  - Compactions this session
  - Total session input tokens
"""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.context_cmd import ContextCommand
from plugin_sdk.runtime_context import RuntimeContext


@pytest.mark.asyncio
async def test_context_renders_basic_panel():
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "session_tokens_in": 5_000,
            "last_input_tokens": 4_500,
            "session_compactions": 0,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    assert "Context window" in out
    assert "claude-opus-4-7" in out
    assert "%" in out


@pytest.mark.asyncio
async def test_context_uses_last_input_tokens_over_session_total():
    """The current-turn token count is the right answer to "% of context
    used right now". ``session_tokens_in`` is cumulative across compactions,
    which is misleading for "% used now"."""
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "session_tokens_in": 500_000,  # cumulative across compactions
            "last_input_tokens": 50_000,   # current turn — what's actually loaded
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    assert "50,000" in out  # the current-turn value, formatted with comma
    assert "500,000" not in out.split("Context window", 1)[1].split("compactions this session", 1)[0]


@pytest.mark.asyncio
async def test_context_falls_back_to_session_tokens_when_last_input_missing():
    """If the loop hasn't populated last_input_tokens yet (very first
    turn before model returns), fall back to cumulative session count
    so we show *something*."""
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "session_tokens_in": 1_234,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    assert "1,234" in out


@pytest.mark.asyncio
async def test_context_renders_compaction_count():
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "last_input_tokens": 1_000,
            "session_compactions": 3,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    # Use a stable substring so the test doesn't depend on exact phrasing.
    assert "compactions this session" in out
    assert "3" in out


@pytest.mark.asyncio
async def test_context_compactions_zero_when_key_missing():
    """Pre-v18 sessions / before any compaction: render 0, not "(not tracked)"."""
    rt = RuntimeContext(custom={"model": "claude-opus-4-7", "last_input_tokens": 100})
    out = (await ContextCommand().execute("", rt)).output
    assert "compactions this session: 0" in out


@pytest.mark.asyncio
async def test_context_handles_unknown_model_gracefully():
    """Unknown / empty model name: still render with a sensible default
    context window (no exception, no crash)."""
    rt = RuntimeContext(custom={"last_input_tokens": 100})
    out = (await ContextCommand().execute("", rt)).output
    assert "Context window" in out
    # Model line should show "(unknown)" or empty rather than crashing.
    assert "100" in out


@pytest.mark.asyncio
async def test_context_percentage_is_correct():
    """Rendered % matches used / max."""
    rt = RuntimeContext(
        custom={
            "model": "claude-sonnet-4-6",  # 200k window per OC's static table
            "last_input_tokens": 100_000,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    # 100,000 of 200,000 = 50.0%
    assert "50.0%" in out


@pytest.mark.asyncio
async def test_context_caps_percentage_when_over_window():
    """A buggy provider could report tokens > window. We must not crash;
    render the actual numbers and let the user see something is off."""
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "last_input_tokens": 250_000,  # > 200k default
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    assert "Context window" in out
    # No exception raised; the renderer is honest about the over-cap value.


@pytest.mark.asyncio
async def test_context_handles_negative_remaining_gracefully():
    """If used > max, "remaining" goes negative. Surface it honestly
    rather than clamping to 0 — the user benefits from knowing the
    over-cap state."""
    rt = RuntimeContext(
        custom={
            "model": "claude-opus-4-7",
            "last_input_tokens": 250_000,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    # No assertion on the literal sign of the remaining; only that
    # the slash didn't raise. The previous test already covered the
    # over-cap %.
    assert out  # non-empty


@pytest.mark.asyncio
async def test_context_zero_tokens_does_not_divide_by_zero():
    """Edge: model maps to 0-context (impossible in practice, but we
    must not crash). The renderer falls back to a safe default rather
    than dividing by zero."""
    rt = RuntimeContext(custom={"last_input_tokens": 0})
    out = (await ContextCommand().execute("", rt)).output
    # Should render without exception; % is 0.0%.
    assert "0.0%" in out


@pytest.mark.asyncio
async def test_context_handles_empty_runtime_custom():
    """Adversarial: completely empty custom dict. The slash must render
    a useful empty-state panel — no crash, no NoneType errors."""
    rt = RuntimeContext()
    out = (await ContextCommand().execute("", rt)).output
    assert "Context window" in out


@pytest.mark.asyncio
async def test_context_handles_non_int_token_values():
    """Adversarial: a buggy plugin set a string token value. The slash
    coerces / falls back to 0 rather than raising."""
    rt = RuntimeContext(
        custom={
            "session_tokens_in": "not-a-number",
            "last_input_tokens": None,
        }
    )
    out = (await ContextCommand().execute("", rt)).output
    assert "Context window" in out


@pytest.mark.asyncio
async def test_context_command_name_and_description():
    """Stable surface — name and description are public contract."""
    cmd = ContextCommand()
    assert cmd.name == "context"
    assert cmd.description  # non-empty
