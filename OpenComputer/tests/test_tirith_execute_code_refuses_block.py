"""Hermes parity: ExecuteCode refuses on Tirith block."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin_sdk.core import ToolCall
from opencomputer.tools.execute_code import ExecuteCode
from opencomputer.security.tirith import TirithResult


@pytest.mark.asyncio
async def test_execute_code_refuses_on_tirith_block():
    tool = ExecuteCode()
    call = ToolCall(
        id="c1", name="ExecuteCode",
        arguments={"code": "print('hi')"},
    )
    blocked = TirithResult(
        action="block",
        findings=[{"severity": "high", "title": "fake", "description": "x"}],
        summary="blocked: fake",
    )
    with patch(
        "opencomputer.tools.execute_code.tirith_check_command",
        return_value=blocked,
    ):
        result = await tool.execute(call)
    assert result.is_error is True
    assert "Refused" in result.content
    assert "Tirith" in result.content


@pytest.mark.asyncio
async def test_execute_code_does_not_refuse_on_tirith_allow():
    """When Tirith allows, ExecuteCode does NOT add a Refused: Tirith error.

    (May still error for other reasons — sandbox / subprocess setup —
    so we don't assert on full success, only on the absence of Tirith
    refusal.)
    """
    tool = ExecuteCode()
    call = ToolCall(
        id="c1", name="ExecuteCode",
        arguments={"code": "print('hi')"},
    )
    safe = TirithResult(action="allow")
    with patch(
        "opencomputer.tools.execute_code.tirith_check_command",
        return_value=safe,
    ):
        result = await tool.execute(call)
    assert "Refused: Tirith" not in result.content
