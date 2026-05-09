"""M8.1 + M8.2 — prompt + agent hook types.

Pins the contract added 2026-05-09:

* YAML parser recognises `type: prompt` / `type: agent` and produces
  the corresponding typed dataclass.
* `_parse_returns` shapes (allow/block/score for prompt; allow/block
  for agent) follow the documented spec.
* Handler factories produce async callables that fail-open on
  timeout / exception (CLAUDE.md §7 contract).
* `cfg.hooks_prompt` / `cfg.hooks_agent` populate from settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from opencomputer.agent.config import (
    HookAgentConfig,
    HookCommandConfig,
    HookPromptConfig,
)
from opencomputer.agent.config_store import _parse_hooks_block, load_config
from opencomputer.hooks.agent_handlers import (
    _parse_returns as _agent_parse_returns,
)
from opencomputer.hooks.agent_handlers import make_agent_hook_handler
from opencomputer.hooks.prompt_handlers import (
    _parse_returns as _prompt_parse_returns,
)
from opencomputer.hooks.prompt_handlers import make_prompt_hook_handler


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ─── parser: type: prompt ───────────────────────────────────────────────


class TestParserPrompt:
    def test_parses_minimal_prompt_hook(self) -> None:
        block = [
            {
                "type": "prompt",
                "event": "PreToolUse",
                "system": "Decide allow/block",
            }
        ]
        parsed = _parse_hooks_block(block)
        assert len(parsed) == 1
        h = parsed[0]
        assert isinstance(h, HookPromptConfig)
        assert h.event == "PreToolUse"
        assert h.system == "Decide allow/block"
        assert h.returns == "allow"  # default
        assert h.timeout_seconds == 5.0  # default
        assert h.token_budget == 600  # default

    def test_prompt_hook_honors_overrides(self) -> None:
        block = [
            {
                "type": "prompt",
                "event": "PreToolUse",
                "system": "Score risk",
                "returns": "score",
                "model": "claude-haiku-4-5",
                "timeout_seconds": 10.0,
                "token_budget": 1500,
                "matcher": "Bash",
            }
        ]
        parsed = _parse_hooks_block(block)
        h = parsed[0]
        assert isinstance(h, HookPromptConfig)
        assert h.returns == "score"
        assert h.model == "claude-haiku-4-5"
        assert h.timeout_seconds == 10.0
        assert h.token_budget == 1500
        assert h.matcher == "Bash"

    def test_prompt_hook_missing_system_skipped(self) -> None:
        block = [{"type": "prompt", "event": "PreToolUse"}]
        assert _parse_hooks_block(block) == ()

    def test_prompt_hook_invalid_returns_skipped(self) -> None:
        block = [
            {
                "type": "prompt",
                "event": "PreToolUse",
                "system": "x",
                "returns": "invalid",
            }
        ]
        assert _parse_hooks_block(block) == ()


# ─── parser: type: agent ────────────────────────────────────────────────


class TestParserAgent:
    def test_parses_minimal_agent_hook(self) -> None:
        block = [
            {
                "type": "agent",
                "event": "PreToolUse",
                "agent": "code-reviewer",
                "prompt": "Review this diff for secrets",
            }
        ]
        parsed = _parse_hooks_block(block)
        assert len(parsed) == 1
        h = parsed[0]
        assert isinstance(h, HookAgentConfig)
        assert h.event == "PreToolUse"
        assert h.agent == "code-reviewer"
        assert h.prompt == "Review this diff for secrets"
        assert h.returns == "allow"
        assert h.max_turns == 5
        assert h.timeout_seconds == 60.0
        assert h.token_budget == 5000

    def test_agent_hook_honors_overrides(self) -> None:
        block = [
            {
                "type": "agent",
                "event": "Stop",
                "agent": "summariser",
                "prompt": "Summarise this turn",
                "max_turns": 3,
                "timeout_seconds": 30.0,
                "token_budget": 2000,
                "returns": "block",
            }
        ]
        h = _parse_hooks_block(block)[0]
        assert isinstance(h, HookAgentConfig)
        assert h.max_turns == 3
        assert h.timeout_seconds == 30.0
        assert h.token_budget == 2000
        assert h.returns == "block"

    def test_agent_hook_missing_agent_skipped(self) -> None:
        block = [
            {
                "type": "agent",
                "event": "PreToolUse",
                "prompt": "x",
            }
        ]
        assert _parse_hooks_block(block) == ()

    def test_agent_hook_missing_prompt_skipped(self) -> None:
        block = [
            {
                "type": "agent",
                "event": "PreToolUse",
                "agent": "x",
            }
        ]
        assert _parse_hooks_block(block) == ()

    def test_agent_hook_score_returns_skipped(self) -> None:
        # 'score' is for prompt-hook only; agent-hook accepts allow/block
        block = [
            {
                "type": "agent",
                "event": "PreToolUse",
                "agent": "x",
                "prompt": "y",
                "returns": "score",
            }
        ]
        assert _parse_hooks_block(block) == ()


# ─── parser: heterogeneous mix ──────────────────────────────────────────


class TestParserHeterogeneous:
    def test_mixed_command_prompt_agent_all_parsed(self) -> None:
        block = [
            {
                "type": "command",
                "event": "PreToolUse",
                "command": "echo hi",
            },
            {
                "type": "prompt",
                "event": "PreToolUse",
                "system": "Decide",
            },
            {
                "type": "agent",
                "event": "PreToolUse",
                "agent": "review",
                "prompt": "Check this",
            },
        ]
        parsed = _parse_hooks_block(block)
        assert len(parsed) == 3
        types = {type(p).__name__ for p in parsed}
        assert types == {
            "HookCommandConfig",
            "HookPromptConfig",
            "HookAgentConfig",
        }

    def test_unknown_type_skipped(self) -> None:
        block = [
            {"type": "magic", "event": "PreToolUse", "command": "x"},
            {"type": "command", "event": "PreToolUse", "command": "ok"},
        ]
        parsed = _parse_hooks_block(block)
        assert len(parsed) == 1
        assert isinstance(parsed[0], HookCommandConfig)


# ─── load_config buckets parsed hooks ────────────────────────────────────


class TestLoadConfigBuckets:
    def test_load_config_separates_buckets(self, tmp_path: Path) -> None:
        cfg_yaml = tmp_path / "config.yaml"
        cfg_yaml.write_text(
            "hooks:\n"
            "  PreToolUse:\n"
            "    - type: command\n"
            "      command: 'echo cmd'\n"
            "    - type: prompt\n"
            "      system: 'Decide'\n"
            "    - type: agent\n"
            "      agent: rev\n"
            "      prompt: Check\n"
        )
        cfg = load_config(cfg_yaml)
        assert len(cfg.hooks) == 1
        assert len(cfg.hooks_prompt) == 1
        assert len(cfg.hooks_agent) == 1


# ─── _parse_returns helpers ──────────────────────────────────────────────


class TestPromptParseReturns:
    @pytest.mark.parametrize(
        "raw,mode,expected_decision",
        [
            ("allow", "allow", "pass"),
            ("ok", "allow", "pass"),
            ("block", "allow", "block"),
            ("anything else", "allow", "block"),
            ("block reason", "block", "block"),
            ("allow", "block", "pass"),
            ("3.5 risk score", "score", "pass"),
        ],
    )
    def test_decision_for_canonical_replies(
        self, raw: str, mode: str, expected_decision: str
    ) -> None:
        decision, _ = _prompt_parse_returns(raw, mode)
        assert decision == expected_decision

    def test_score_extracts_first_numeric(self) -> None:
        _, modified = _prompt_parse_returns("risk = 7.2 / 10", "score")
        assert "7.2" in modified


class TestAgentParseReturns:
    @pytest.mark.parametrize(
        "raw,mode,expected",
        [
            ("allow", "allow", "pass"),
            ("block this", "allow", "block"),
            ("allow", "block", "pass"),
            ("block", "block", "block"),
        ],
    )
    def test_agent_decision(self, raw: str, mode: str, expected: str) -> None:
        decision, _ = _agent_parse_returns(raw, mode)
        assert decision == expected


# ─── prompt-hook handler fail-open posture ──────────────────────────────


class TestPromptHandlerFailOpen:
    def test_aux_llm_timeout_returns_pass(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookPromptConfig(
            event="PreToolUse",
            system="x",
            timeout_seconds=0.05,  # tiny so the wait_for trips fast
        )
        handler = make_prompt_hook_handler(cfg)

        async def _slow(*a, **kw):
            await asyncio.sleep(1.0)
            return "block"

        with patch(
            "opencomputer.agent.aux_llm.complete_text", side_effect=_slow
        ):
            ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
            decision = _run(handler(ctx))

        assert decision.decision == "pass"

    def test_aux_llm_exception_returns_pass(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookPromptConfig(event="PreToolUse", system="x")
        handler = make_prompt_hook_handler(cfg)

        async def _boom(*a, **kw):
            raise RuntimeError("provider down")

        with patch(
            "opencomputer.agent.aux_llm.complete_text", side_effect=_boom
        ):
            ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
            decision = _run(handler(ctx))

        assert decision.decision == "pass"

    def test_token_budget_overrun_returns_pass_no_call(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookPromptConfig(
            event="PreToolUse",
            system="x" * 10000,  # huge system prompt
            token_budget=10,  # but tiny budget
        )
        handler = make_prompt_hook_handler(cfg)

        # Don't even need to patch — we should fail-open before calling.
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
        decision = _run(handler(ctx))
        assert decision.decision == "pass"


# ─── agent-hook handler fail-open posture ───────────────────────────────


class TestAgentHandlerFailOpen:
    def test_delegate_timeout_returns_pass(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookAgentConfig(
            event="PreToolUse",
            agent="x",
            prompt="y",
            timeout_seconds=0.05,
        )
        handler = make_agent_hook_handler(cfg)

        slow_execute = AsyncMock(side_effect=asyncio.sleep(1.0))

        class _StubDelegate:
            async def execute(self, _call):
                await asyncio.sleep(1.0)
                from plugin_sdk.core import ToolResult

                return ToolResult(tool_call_id="x", content="block")

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_StubDelegate(),
        ):
            ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
            decision = _run(handler(ctx))

        assert decision.decision == "pass"

    def test_delegate_exception_returns_pass(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookAgentConfig(event="PreToolUse", agent="x", prompt="y")
        handler = make_agent_hook_handler(cfg)

        class _BoomDelegate:
            async def execute(self, _call):
                raise RuntimeError("agent gone")

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_BoomDelegate(),
        ):
            ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
            decision = _run(handler(ctx))

        assert decision.decision == "pass"

    def test_token_budget_overrun_returns_pass_no_spawn(self) -> None:
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookAgentConfig(
            event="PreToolUse",
            agent="x",
            prompt="y" * 10000,
            token_budget=10,
        )
        handler = make_agent_hook_handler(cfg)
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
        decision = _run(handler(ctx))
        assert decision.decision == "pass"

    def test_delegate_not_initialized_returns_pass(self) -> None:
        """Gap-3 amend (PR #533): when DelegateTool.set_factory hasn't been
        called yet (e.g. settings-hook fires at gateway startup before any
        AgentLoop wired the factory), DelegateTool().execute() returns a
        ToolResult with is_error=True and content 'Error: delegate is not
        initialized...'. The handler must fail-open per CLAUDE.md §7,
        not propagate the error to the agent loop."""
        from plugin_sdk.core import ToolResult
        from plugin_sdk.hooks import HookContext, HookEvent

        cfg = HookAgentConfig(event="PreToolUse", agent="x", prompt="y")
        handler = make_agent_hook_handler(cfg)

        class _UninitializedDelegate:
            async def execute(self, _call):
                # Mirror DelegateTool's actual behavior when
                # _factory is None (see tools/delegate.py L268-276)
                return ToolResult(
                    tool_call_id=_call.id,
                    content=(
                        "Error: delegate is not initialized. "
                        "CLI bootstrapping must call DelegateTool.set_factory(...)."
                    ),
                    is_error=True,
                )

        with patch(
            "opencomputer.tools.delegate.DelegateTool",
            return_value=_UninitializedDelegate(),
        ):
            ctx = HookContext(event=HookEvent.PRE_TOOL_USE, session_id="test")
            decision = _run(handler(ctx))

        assert decision.decision == "pass"
