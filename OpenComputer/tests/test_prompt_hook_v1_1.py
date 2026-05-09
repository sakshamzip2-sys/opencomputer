"""v1.1 plan-2 M8.1 (2026-05-09) — settings-declared LLM-prompt hooks.

Covers:

* :class:`HookPromptConfig` dataclass exists + has expected fields.
* ``_parse_prompt_hooks_block`` extracts ``type: prompt`` entries from
  the same YAML block that ``_parse_hooks_block`` reads.
* ``make_prompt_hook_handler`` returns an async handler that:
  - Calls the aux LLM (mocked) and parses the response.
  - Times out fail-open after ``timeout_seconds``.
  - Refuses to call when estimated input > ``token_budget_input``.
  - Parses ``returns: allow_block`` and ``returns: score``.

The aux LLM is mocked via :func:`monkeypatch.setattr` on the lazily-
imported ``opencomputer.agent.aux_llm.complete_text`` so we don't need
a real provider.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.config import HookPromptConfig
from opencomputer.agent.config_store import _parse_prompt_hooks_block
from opencomputer.hooks.prompt_handlers import (
    _parse_response_allow_block,
    _parse_response_score,
    _render_context,
    make_prompt_hook_handler,
)
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _ctx(
    *,
    event=HookEvent.PRE_TOOL_USE,
    tool_name: str = "Bash",
    arguments: dict | None = None,
    session_id: str = "sess1",
) -> HookContext:
    """Build a HookContext with a synthetic ToolCall for tests."""
    tc = ToolCall(
        id="tc1", name=tool_name, arguments=arguments or {},
    )
    return HookContext(event=event, session_id=session_id, tool_call=tc)


# ─── dataclass + parser ────────────────────────────────────────────────


def test_hook_prompt_config_defaults():
    cfg = HookPromptConfig(event="PreToolUse", system="Be careful.")
    assert cfg.event == "PreToolUse"
    assert cfg.system == "Be careful."
    assert cfg.model == "auto"
    assert cfg.returns == "allow_block"
    assert cfg.timeout_seconds == 5.0
    assert cfg.token_budget_input == 500
    assert cfg.token_budget_output == 100
    assert cfg.score_threshold == 7.0


def test_parse_prompt_hooks_block_nested():
    """Nested event-keyed shape extracts ``type: prompt`` entries only."""
    block = {
        "PreToolUse": [
            {
                "type": "prompt",
                "system": "Rate this.",
                "model": "haiku",
                "matcher": "Bash",
                "timeout_seconds": 3,
            },
            {
                # command entry — must be ignored by the prompt parser
                "type": "command",
                "command": "/bin/true",
            },
        ],
    }
    parsed = _parse_prompt_hooks_block(block)
    assert len(parsed) == 1
    p = parsed[0]
    assert p.event == "PreToolUse"
    assert p.system == "Rate this."
    assert p.model == "haiku"
    assert p.matcher == "Bash"
    assert p.timeout_seconds == 3.0


def test_parse_prompt_hooks_block_flat_list():
    block = [
        {"event": "PostToolUse", "type": "prompt", "system": "Audit."},
        {"event": "Stop", "type": "command", "command": "/bin/true"},
    ]
    parsed = _parse_prompt_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].event == "PostToolUse"
    assert parsed[0].system == "Audit."


def test_parse_prompt_hooks_block_skips_invalid():
    """Unknown event / missing system / bad returns → skipped, not raised."""
    block = {
        "NotARealEvent": [{"type": "prompt", "system": "x"}],
        "PreToolUse": [
            {"type": "prompt", "system": ""},  # empty system
            {"type": "prompt", "system": "ok", "returns": "fish"},  # bad returns
            {"type": "prompt", "system": "ok"},  # valid
        ],
    }
    parsed = _parse_prompt_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].system == "ok"


def test_parse_prompt_hooks_block_none_returns_empty():
    assert _parse_prompt_hooks_block(None) == ()


# ─── _render_context ──────────────────────────────────────────────────


def test_render_context_includes_event_and_tool():
    cfg = HookPromptConfig(event="PreToolUse", system="x", matcher="Bash")
    ctx = _ctx(
        tool_name="Bash",
        arguments={"command": "rm -rf /"},
    )
    out = _render_context(cfg, ctx)
    assert "PreToolUse" in out.user_message
    assert "Bash" in out.user_message
    assert "rm -rf" in out.user_message
    assert "sess1" in out.user_message
    assert out.estimated_input_tokens > 0


def test_render_context_truncates_huge_args():
    cfg = HookPromptConfig(event="PreToolUse", system="x")
    big_arg = "X" * 10_000
    ctx = _ctx(tool_name="Bash", arguments={"command": big_arg})
    out = _render_context(cfg, ctx)
    assert "[truncated]" in out.user_message


# ─── _parse_response_allow_block ───────────────────────────────────────


def test_parse_response_block_with_reason():
    d = _parse_response_allow_block("block: rm -rf is destructive\n")
    assert d.decision == "block"
    assert "destructive" in (d.reason or "")


def test_parse_response_block_bare():
    d = _parse_response_allow_block("BLOCK")
    assert d.decision == "block"


def test_parse_response_allow_variants():
    for text in ("allow", "Allow", "approve", "PASS", "ok"):
        d = _parse_response_allow_block(text)
        assert d.decision == "pass", f"expected pass for {text!r}"


def test_parse_response_ambiguous_passes():
    d = _parse_response_allow_block("looks fine to me")
    assert d.decision == "pass"


def test_parse_response_empty_passes():
    d = _parse_response_allow_block("")
    assert d.decision == "pass"


# ─── _parse_response_score ─────────────────────────────────────────────


def test_parse_response_score_above_threshold_blocks():
    d = _parse_response_score("Risk: 9 / 10", threshold=7.0)
    assert d.decision == "block"
    assert "9" in (d.reason or "")


def test_parse_response_score_below_threshold_passes():
    d = _parse_response_score("Risk: 3 / 10", threshold=7.0)
    assert d.decision == "pass"


def test_parse_response_score_no_number_passes():
    d = _parse_response_score("looks fine", threshold=5.0)
    assert d.decision == "pass"


# ─── handler integration (mocked aux-LLM) ─────────────────────────────


@pytest.mark.asyncio
async def test_handler_calls_aux_llm_and_parses_block(monkeypatch):
    cfg = HookPromptConfig(event="PreToolUse", system="Rate.")
    mock = AsyncMock(return_value="block: dangerous")
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", mock
    )
    handler = make_prompt_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Bash", arguments={"command": "rm -rf /"},
    ))
    assert decision.decision == "block"
    assert "dangerous" in (decision.reason or "")
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_handler_calls_aux_llm_and_parses_allow(monkeypatch):
    cfg = HookPromptConfig(event="PreToolUse", system="Rate.")
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text",
        AsyncMock(return_value="allow"),
    )
    handler = make_prompt_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Read", arguments={"file_path": "/etc/hosts"},
    ))
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_handler_score_mode(monkeypatch):
    cfg = HookPromptConfig(
        event="PreToolUse", system="Rate.", returns="score",
        score_threshold=5.0,
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text",
        AsyncMock(return_value="Risk score: 8 / 10"),
    )
    handler = make_prompt_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Bash", arguments={"command": "x"},
    ))
    assert decision.decision == "block"


@pytest.mark.asyncio
async def test_handler_timeout_fails_open(monkeypatch):
    """Aux LLM exceeding timeout → handler returns pass with a warning."""
    cfg = HookPromptConfig(
        event="PreToolUse", system="Rate.", timeout_seconds=0.05,
    )

    async def _slow(**__):
        await asyncio.sleep(0.5)
        return "block: nope"

    monkeypatch.setattr("opencomputer.agent.aux_llm.complete_text", _slow)
    handler = make_prompt_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Bash", arguments={"command": "x"},
    ))
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_handler_token_cap_refuses_to_call(monkeypatch):
    """Estimated input > budget → handler skips LLM call entirely."""
    cfg = HookPromptConfig(
        event="PreToolUse", system="Rate.", token_budget_input=10,
    )
    mock = AsyncMock()
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", mock
    )
    handler = make_prompt_hook_handler(cfg)
    big_args = {"command": "X" * 1000}
    decision = await handler(_ctx(tool_name="Bash", arguments=big_args))
    assert decision.decision == "pass"
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_handler_llm_error_fails_open(monkeypatch):
    """Aux LLM raising → handler returns pass + warning."""
    cfg = HookPromptConfig(event="PreToolUse", system="Rate.")
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text",
        AsyncMock(side_effect=RuntimeError("provider down")),
    )
    handler = make_prompt_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Bash", arguments={"command": "x"},
    ))
    assert decision.decision == "pass"


# ─── load_config integration ────────────────────────────────────────────


def test_config_load_extracts_prompt_hooks(tmp_path):
    """Full config-load round-trip — YAML → Config.prompt_hooks tuple."""
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "hooks:\n"
        "  PreToolUse:\n"
        "    - type: prompt\n"
        "      matcher: Bash\n"
        "      system: |\n"
        "        Rate this.\n"
        "      model: haiku\n"
        "      timeout_seconds: 3\n"
        "    - type: command\n"
        "      command: /bin/true\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert len(cfg.prompt_hooks) == 1
    assert cfg.prompt_hooks[0].event == "PreToolUse"
    assert cfg.prompt_hooks[0].matcher == "Bash"
    assert cfg.prompt_hooks[0].model == "haiku"
    # The command hook also lands in the existing tuple, untouched.
    assert len(cfg.hooks) == 1
    assert cfg.hooks[0].command == "/bin/true"
