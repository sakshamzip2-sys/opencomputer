"""v1.1 plan-2 M8.2 (2026-05-09) — settings-declared subagent hooks.

Covers:

* :class:`HookAgentConfig` dataclass exists + has expected fields.
* ``_parse_agent_hooks_block`` extracts ``type: agent`` entries from
  the same YAML block that ``_parse_hooks_block`` reads.
* ``_parse_hooks_block`` silently skips ``type: agent`` (no warning).
* ``make_agent_hook_handler`` returns an async handler that:
  - Spawns via DelegateTool (mocked) and parses the response.
  - Times out fail-open after ``timeout_seconds``.
  - Refuses to spawn when estimated input > ``token_budget_total``.
  - Parses ``returns: allow_block`` and ``returns: structured``.

The DelegateTool is mocked via ``monkeypatch.setattr`` on the lazily-
imported module so tests don't need a real provider.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.config import HookAgentConfig
from opencomputer.agent.config_store import (
    _parse_agent_hooks_block,
    _parse_hooks_block,
)
from opencomputer.hooks.agent_handlers import (
    _parse_response_allow_block,
    _parse_response_structured,
    _render_context,
    make_agent_hook_handler,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.hooks import HookContext, HookEvent


def _ctx(
    *,
    event=HookEvent.PRE_TOOL_USE,
    tool_name: str = "Bash",
    arguments: dict | None = None,
    session_id: str = "sess1",
) -> HookContext:
    tc = ToolCall(id="tc1", name=tool_name, arguments=arguments or {})
    return HookContext(event=event, session_id=session_id, tool_call=tc)


# ─── dataclass + parser ────────────────────────────────────────────────


def test_hook_agent_config_defaults():
    cfg = HookAgentConfig(event="PreToolUse", prompt="Inspect this.")
    assert cfg.event == "PreToolUse"
    assert cfg.prompt == "Inspect this."
    assert cfg.agent == ""
    assert cfg.isolation == "copy"
    assert cfg.returns == "allow_block"
    assert cfg.matcher is None
    assert cfg.max_turns == 5
    assert cfg.timeout_seconds == 60.0
    assert cfg.token_budget_total == 5000


def test_parse_agent_hooks_block_nested():
    block = {
        "PreToolUse": [
            {
                "type": "agent",
                "prompt": "Rate this.",
                "agent": "code-reviewer",
                "matcher": "Bash",
                "isolation": "copy",
                "max_turns": 3,
                "timeout_seconds": 30,
            },
            {"type": "command", "command": "/bin/true"},
            {"type": "prompt", "system": "rate"},
        ],
    }
    parsed = _parse_agent_hooks_block(block)
    assert len(parsed) == 1
    a = parsed[0]
    assert a.event == "PreToolUse"
    assert a.prompt == "Rate this."
    assert a.agent == "code-reviewer"
    assert a.isolation == "copy"
    assert a.matcher == "Bash"
    assert a.max_turns == 3
    assert a.timeout_seconds == 30.0


def test_parse_agent_hooks_block_flat_list():
    block = [
        {"event": "PostToolUse", "type": "agent", "prompt": "Audit."},
        {"event": "Stop", "type": "command", "command": "/bin/true"},
    ]
    parsed = _parse_agent_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].event == "PostToolUse"


def test_parse_agent_hooks_block_skips_invalid():
    """Unknown event / missing prompt / invalid isolation → skipped."""
    block = {
        "NotARealEvent": [{"type": "agent", "prompt": "x"}],
        "PreToolUse": [
            {"type": "agent", "prompt": ""},                              # empty
            {"type": "agent", "prompt": "ok", "isolation": "bogus"},      # bad iso
            {"type": "agent", "prompt": "ok", "returns": "fish"},         # bad returns
            {"type": "agent", "prompt": "ok", "max_turns": 0},            # zero turns
            {"type": "agent", "prompt": "ok"},                            # valid
        ],
    }
    parsed = _parse_agent_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].prompt == "ok"


def test_parse_hooks_block_silently_skips_agent_type():
    """type: agent entries are NOT a 'unsupported type' warning anymore."""
    block = {
        "PreToolUse": [
            {"type": "agent", "prompt": "x"},
            {"type": "command", "command": "/bin/true"},
        ],
    }
    parsed = _parse_hooks_block(block)
    # Only the command hook is returned — agent silently skipped.
    assert len(parsed) == 1
    assert parsed[0].command == "/bin/true"


# ─── _render_context ──────────────────────────────────────────────────


def test_render_context_user_prompt_first():
    cfg = HookAgentConfig(event="PreToolUse", prompt="Inspect.", matcher="Bash")
    ctx = _ctx(tool_name="Bash", arguments={"command": "rm -rf /"})
    out = _render_context(cfg, ctx)
    # User prompt body appears at the top.
    assert out.task_text.startswith("Inspect.")
    assert "PreToolUse" in out.task_text
    assert "Bash" in out.task_text
    assert "rm -rf" in out.task_text
    assert out.estimated_input_tokens > 0


def test_render_context_truncates_huge_args():
    cfg = HookAgentConfig(event="PreToolUse", prompt="Audit.")
    big_arg = "X" * 10_000
    ctx = _ctx(tool_name="Bash", arguments={"command": big_arg})
    out = _render_context(cfg, ctx)
    assert "[truncated]" in out.task_text


# ─── response parsing ──────────────────────────────────────────────────


def test_parse_response_block_with_reason():
    d = _parse_response_allow_block("block: command would exfiltrate creds\n")
    assert d.decision == "block"
    assert "exfiltrate" in (d.reason or "")


def test_parse_response_allow_variants():
    for text in ("allow", "Allow", "approve", "PASS", "ok"):
        d = _parse_response_allow_block(text)
        assert d.decision == "pass", f"expected pass for {text!r}"


def test_parse_response_structured_returns_full_text():
    d = _parse_response_structured("Risk: 7/10\nReason: rm -rf is dangerous")
    assert d.decision == "pass"
    assert "Risk" in (d.reason or "")
    assert "dangerous" in (d.reason or "")


def test_parse_response_structured_empty_returns_pass():
    d = _parse_response_structured("")
    assert d.decision == "pass"
    assert d.reason is None or d.reason == ""


# ─── handler integration (mocked DelegateTool) ────────────────────────


@pytest.mark.asyncio
async def test_handler_spawns_delegate_and_parses_block(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.", isolation="none",
    )
    captured: dict = {}

    async def _fake_execute(self, call):  # noqa: ARG001 — bound-method shape
        captured["task"] = call.arguments.get("task")
        captured["isolation"] = call.arguments.get("isolation")
        return ToolResult(
            tool_call_id=call.id,
            content="block: this Bash command would touch /etc",
        )

    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute", _fake_execute
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx(
        tool_name="Bash", arguments={"command": "echo > /etc/hosts"},
    ))
    assert decision.decision == "block"
    assert "touch /etc" in (decision.reason or "")
    assert "Audit." in captured["task"]
    assert captured["isolation"] == "none"


@pytest.mark.asyncio
async def test_handler_passes_agent_template(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse",
        prompt="Audit.",
        agent="code-reviewer",
        isolation="copy",
    )
    captured: dict = {}

    async def _fake(self, call):  # noqa: ARG001 — bound-method shape
        captured["agent"] = call.arguments.get("agent")
        return ToolResult(tool_call_id=call.id, content="allow")

    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute", _fake
    )
    handler = make_agent_hook_handler(cfg)
    await handler(_ctx())
    assert captured["agent"] == "code-reviewer"


@pytest.mark.asyncio
async def test_handler_structured_mode(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.", returns="structured",
        isolation="none",
    )
    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute",
        AsyncMock(return_value=ToolResult(
            tool_call_id="x",
            content="Risk score: 8\nReason: writes to /etc",
        )),
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx())
    # Structured always passes; reason carries the full body.
    assert decision.decision == "pass"
    assert "Risk score" in (decision.reason or "")


@pytest.mark.asyncio
async def test_handler_timeout_fails_open(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.", timeout_seconds=0.05,
        isolation="none",
    )

    async def _slow(self, call):  # noqa: ARG001 — bound-method shape
        await asyncio.sleep(0.5)
        return ToolResult(tool_call_id=call.id, content="block: nope")

    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute", _slow
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx())
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_handler_token_cap_refuses_spawn(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.",
        token_budget_total=5,        # absurdly low so the render exceeds it
        isolation="none",
    )
    mock = AsyncMock()
    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute", mock
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx(arguments={"command": "X" * 1000}))
    assert decision.decision == "pass"
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_handler_delegate_error_fails_open(monkeypatch):
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.", isolation="none",
    )
    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute",
        AsyncMock(side_effect=RuntimeError("provider down")),
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx())
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_handler_delegate_is_error_response_fails_open(monkeypatch):
    """is_error=True from delegate is treated as fail-open + warn."""
    cfg = HookAgentConfig(
        event="PreToolUse", prompt="Audit.", isolation="none",
    )
    monkeypatch.setattr(
        "opencomputer.tools.delegate.DelegateTool.execute",
        AsyncMock(return_value=ToolResult(
            tool_call_id="x", content="Error: spawn failed", is_error=True,
        )),
    )
    handler = make_agent_hook_handler(cfg)
    decision = await handler(_ctx())
    assert decision.decision == "pass"


# ─── load_config integration ───────────────────────────────────────────


def test_config_load_extracts_agent_hooks(tmp_path):
    """Full config-load round-trip — YAML → Config.agent_hooks tuple."""
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "hooks:\n"
        "  PreToolUse:\n"
        "    - type: agent\n"
        "      matcher: Bash\n"
        "      prompt: |\n"
        "        Inspect this.\n"
        "      agent: code-reviewer\n"
        "      isolation: copy\n"
        "      timeout_seconds: 30\n"
        "    - type: prompt\n"
        "      system: rate\n"
        "    - type: command\n"
        "      command: /bin/true\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert len(cfg.agent_hooks) == 1
    assert cfg.agent_hooks[0].event == "PreToolUse"
    assert cfg.agent_hooks[0].agent == "code-reviewer"
    assert cfg.agent_hooks[0].isolation == "copy"
    # The other types land in their own buckets.
    assert len(cfg.prompt_hooks) == 1
    assert len(cfg.hooks) == 1
