"""PowerShellRun tool — Windows-only execution of pwsh/powershell scripts."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin_sdk.core import ToolCall


def _make_tool():
    from opencomputer.tools.powershell_run import PowerShellRunTool
    return PowerShellRunTool()


def test_schema_advertises_windows_only() -> None:
    tool = _make_tool()
    assert tool.schema.name == "PowerShellRun"
    assert "Windows" in tool.schema.description


def test_capability_claim_per_action_consent() -> None:
    """PowerShell can do anything; consent must be PER_ACTION."""
    from plugin_sdk.consent import ConsentTier
    tool = _make_tool()
    claim = tool.capability_claims[0]
    assert claim.tier_required == ConsentTier.PER_ACTION
    assert claim.capability_id == "gui.powershell_run"


def test_parallel_safe_false() -> None:
    """PowerShell mutates global state (registry, COM); not parallel-safe."""
    tool = _make_tool()
    assert tool.parallel_safe is False


def test_returns_error_on_non_windows() -> None:
    tool = _make_tool()
    if sys.platform == "win32":
        pytest.skip("only tests the non-windows guard")
    call = ToolCall(id="t1", name="PowerShellRun", arguments={"script": "Write-Host hi"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "windows" in result.content.lower() or "powershell" in result.content.lower()


def test_returns_error_on_empty_script() -> None:
    tool = _make_tool()
    call = ToolCall(id="t1", name="PowerShellRun", arguments={"script": "  "})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "non-empty" in result.content


def test_invokes_pwsh_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, prefer ``pwsh`` over ``powershell.exe``."""
    monkeypatch.setattr("sys.platform", "win32")
    fake_run = MagicMock()
    fake_run.return_value = MagicMock(returncode=0, stdout="hi", stderr="")

    with patch("opencomputer.tools.powershell_run.shutil.which") as which:
        which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("pwsh",) else None
        with patch("opencomputer.tools.powershell_run.subprocess.run", fake_run):
            tool = _make_tool()
            call = ToolCall(id="t1", name="PowerShellRun", arguments={"script": "Write-Host hi"})
            result = asyncio.run(tool.execute(call))

    args, _kwargs = fake_run.call_args
    assert args[0][0] == "/usr/bin/pwsh", f"expected pwsh first, got {args[0]!r}"
    assert "hi" in result.content
    assert result.is_error is False
