"""Hermes parity: BashTool refuses on Tirith block; allows on Tirith allow.

Tirith pre-exec scan happens INSIDE BashTool.execute (not via consent
gate threading) — verdict 'block' returns an error result with formatted
findings; verdict 'warn' surfaces findings as a prefix to normal output;
'allow' is silent.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.security.tirith import TirithResult
from opencomputer.tools.bash import BashTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_bash_refuses_on_tirith_block_with_findings():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    blocked = TirithResult(
        action="block",
        findings=[{"severity": "high", "title": "fake test pattern",
                   "description": "fake test desc"}],
        summary="blocked: test sentinel",
    )
    with patch(
        "opencomputer.tools.bash.tirith_check_command",
        return_value=blocked,
    ):
        result = await tool.execute(call)
    assert result.is_error is True
    # Refusal message includes the formatted findings.
    assert "Refused" in result.content
    assert "Tirith" in result.content
    assert ("fake test pattern" in result.content
            or "test sentinel" in result.content)


@pytest.mark.asyncio
async def test_bash_allows_on_tirith_allow():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    safe = TirithResult(action="allow")
    with patch(
        "opencomputer.tools.bash.tirith_check_command",
        return_value=safe,
    ):
        result = await tool.execute(call)
    # echo hi succeeds on the host.
    assert result.is_error is False
    assert "hi" in result.content
    # No Tirith decoration.
    assert "Tirith" not in result.content


@pytest.mark.asyncio
async def test_bash_warn_appends_findings_but_runs():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    warn = TirithResult(
        action="warn",
        findings=[{"severity": "medium", "title": "advisory",
                   "description": "noted"}],
        summary="advisory: noted",
    )
    with patch(
        "opencomputer.tools.bash.tirith_check_command",
        return_value=warn,
    ):
        result = await tool.execute(call)
    # Warning surfaces in the result content but doesn't block exec.
    assert result.is_error is False
    assert "advisory" in result.content


@pytest.mark.asyncio
async def test_bash_continues_when_tirith_unavailable():
    """If Tirith binary missing AND fail_open=True (default), bash runs.

    Real call to tirith.check_command — binary likely absent on test
    hosts; fail_open semantics yield action='allow' so bash proceeds.
    """
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})
    result = await tool.execute(call)
    assert result.is_error is False
    assert "hi" in result.content
