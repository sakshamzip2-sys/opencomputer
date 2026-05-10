"""End-to-end: Bash tool refuses commands matched by a ``command_rules`` deny.

Pattern-based deny short-circuits BEFORE Tirith — denials are
deterministic and operator-controlled. The hardline list is still
non-bypassable and runs first; this test ensures the new rule
layer slots between hardline and Tirith correctly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.security.approvals import ApprovalsConfig, CommandRule
from opencomputer.tools.bash import BashTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_bash_refuses_on_command_rule_deny():
    tool = BashTool()
    call = ToolCall(
        id="c1", name="Bash",
        arguments={"command": "git push --force origin main"},
    )

    fake_cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="git push --force", verdict="deny"),
        ),
    )
    with patch(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        return_value=fake_cfg,
    ):
        result = await tool.execute(call)
    assert result.is_error is True
    assert "deny rule" in result.content
    assert "command_rules" in result.content


@pytest.mark.asyncio
async def test_bash_allows_when_no_rule_matches():
    """No deny rule → command proceeds through the rest of the pipeline."""
    tool = BashTool()
    call = ToolCall(
        id="c1", name="Bash", arguments={"command": "echo hello"},
    )

    fake_cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="rm -rf", verdict="deny"),
        ),
    )
    with patch(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        return_value=fake_cfg,
    ):
        result = await tool.execute(call)
    assert result.is_error is False
    assert "hello" in result.content


@pytest.mark.asyncio
async def test_bash_allow_rule_does_not_block_or_short_circuit():
    """``allow`` is informational here — Tirith still runs as a backstop."""
    tool = BashTool()
    call = ToolCall(
        id="c1", name="Bash",
        arguments={"command": "git commit -m message"},
    )

    fake_cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="git commit", verdict="allow"),
        ),
    )
    with patch(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        return_value=fake_cfg,
    ):
        result = await tool.execute(call)
    # Real shell will run `git commit` and (in test cwd with no git
    # state) print an error message — but BashTool itself shouldn't
    # *refuse* the command; it lets git speak for itself.
    assert "deny rule" not in result.content


@pytest.mark.asyncio
async def test_bash_approval_load_failure_does_not_break_exec():
    """If the YAML load raises, exec must continue (fail-open)."""
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo ok"})

    def boom() -> ApprovalsConfig:
        raise RuntimeError("simulated config crash")

    with patch(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        side_effect=boom,
    ):
        result = await tool.execute(call)
    # Command ran normally.
    assert "ok" in result.content
    assert result.is_error is False
