"""``/usage`` surfaces a ``compactions`` row when ``session_compactions`` > 0.

Mirrors the existing cache-line test pattern: the row only appears when
non-zero, so quiet sessions stay clean.

Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`` §4.5.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.usage_cmd import UsageCommand
from plugin_sdk.runtime_context import RuntimeContext


@pytest.mark.asyncio
async def test_usage_renders_compactions_row_when_present():
    rt = RuntimeContext(
        custom={
            "session_tokens_in": 100_000,
            "session_tokens_out": 8_000,
            "session_compactions": 3,
        }
    )
    out = (await UsageCommand().execute("", rt)).output
    assert "compactions" in out.lower()
    assert "3" in out


@pytest.mark.asyncio
async def test_usage_omits_compactions_row_when_zero():
    rt = RuntimeContext(
        custom={
            "session_tokens_in": 100_000,
            "session_tokens_out": 8_000,
            "session_compactions": 0,
        }
    )
    out = (await UsageCommand().execute("", rt)).output
    assert "compactions" not in out.lower()


@pytest.mark.asyncio
async def test_usage_omits_compactions_row_when_key_missing():
    """Pre-v18 sessions / clean state — key never set."""
    rt = RuntimeContext(custom={"session_tokens_in": 100, "session_tokens_out": 50})
    out = (await UsageCommand().execute("", rt)).output
    assert "compactions" not in out.lower()


@pytest.mark.asyncio
async def test_usage_compactions_row_handles_non_int_safely():
    """Adversarial: a buggy plugin sets ``session_compactions`` to a string.
    The /usage render must not crash."""
    rt = RuntimeContext(
        custom={
            "session_tokens_in": 100,
            "session_tokens_out": 50,
            "session_compactions": "broken",  # not int
        }
    )
    out = (await UsageCommand().execute("", rt)).output
    # Don't crash; non-int is dropped (matches cache row's _fmt_count pattern).
    assert "Session usage" in out
