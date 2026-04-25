"""Tests for use_cases.proactive_security_monitoring.

Covers:
- scan_processes flags processes that match SUSPICIOUS_PROCESSES
- scan_processes returns correct shape
- check_recent_browser_history flags suspicious domains
- sweep returns combined report with summary
- SUSPICIOUS_DOMAINS frozenset is non-empty
- sweep sets overall_risk to 'high' when suspicious processes found
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.proactive_security_monitoring import (
    SUSPICIOUS_DOMAINS,
    SUSPICIOUS_PROCESSES,
    check_recent_browser_history,
    scan_processes,
    sweep,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper():
    w = MagicMock()
    w.call = AsyncMock(return_value={})
    return w


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


# Fake ps aux output with a suspicious process
_PS_AUX_WITH_SUSPICIOUS = """\
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
user       100  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/python3 normal.py
user       101  0.5  0.1  23456   234 ?        S    10:01   0:01 /tmp/keylogger --daemon
user       102  0.0  0.0  34567   345 ?        S    10:02   0:00 /usr/bin/bash
"""

_PS_AUX_CLEAN = """\
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
user       100  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/python3 app.py
user       102  0.0  0.0  34567   345 ?        S    10:02   0:00 /usr/bin/bash
"""

# Browser history with a suspicious URL
_HISTORY_WITH_SUSPICIOUS = (
    "https://malware.test/payload|Malware Download|1000000\n"
    "https://github.com|GitHub|999999\n"
    "https://google.com|Google|999998\n"
)

_HISTORY_CLEAN = (
    "https://github.com|GitHub|999999\n"
    "https://google.com|Google|999998\n"
)


# ---------------------------------------------------------------------------
# SUSPICIOUS_DOMAINS and SUSPICIOUS_PROCESSES
# ---------------------------------------------------------------------------

class TestConstants:
    def test_suspicious_processes_nonempty(self):
        assert len(SUSPICIOUS_PROCESSES) > 0

    def test_suspicious_domains_nonempty(self):
        assert len(SUSPICIOUS_DOMAINS) > 0

    def test_suspicious_domains_is_frozenset(self):
        assert isinstance(SUSPICIOUS_DOMAINS, frozenset)


# ---------------------------------------------------------------------------
# scan_processes
# ---------------------------------------------------------------------------

class TestScanProcesses:
    async def test_flags_suspicious_process(self):
        with patch(
            "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_WITH_SUSPICIOUS)),
        ):
            result = await scan_processes(_make_wrapper())

        assert "suspicious" in result
        assert len(result["suspicious"]) >= 1
        names = [e["name"] for e in result["suspicious"]]
        assert any("keylogger" in n for n in names)

    async def test_no_suspicious_when_clean(self):
        with patch(
            "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_CLEAN)),
        ):
            result = await scan_processes(_make_wrapper())

        assert result["suspicious"] == []

    async def test_returns_correct_shape(self):
        with patch(
            "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_CLEAN)),
        ):
            result = await scan_processes(_make_wrapper())

        assert "total" in result
        assert "suspicious" in result
        assert "all" in result
        assert isinstance(result["all"], list)

    async def test_returns_empty_on_error(self):
        with patch(
            "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await scan_processes(_make_wrapper())

        assert result["total"] == 0
        assert result["suspicious"] == []


# ---------------------------------------------------------------------------
# check_recent_browser_history
# ---------------------------------------------------------------------------

class TestCheckRecentBrowserHistory:
    async def test_flags_suspicious_domain(self):
        with patch(
            "extensions.oi_capability.tools.tier_3_browser.ReadBrowserHistoryTool.execute",
            new=AsyncMock(return_value=_tool_result(_HISTORY_WITH_SUSPICIOUS)),
        ):
            result = await check_recent_browser_history(_make_wrapper())

        suspicious = [e for e in result if e["is_suspicious"]]
        assert len(suspicious) >= 1
        assert any("malware.test" in e["url"] for e in suspicious)

    async def test_no_suspicious_when_clean_history(self):
        with patch(
            "extensions.oi_capability.tools.tier_3_browser.ReadBrowserHistoryTool.execute",
            new=AsyncMock(return_value=_tool_result(_HISTORY_CLEAN)),
        ):
            result = await check_recent_browser_history(_make_wrapper())

        suspicious = [e for e in result if e["is_suspicious"]]
        assert len(suspicious) == 0


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

class TestSweep:
    async def test_returns_combined_report(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
                new=AsyncMock(return_value=_tool_result(_PS_AUX_CLEAN)),
            ),
            patch(
                "extensions.oi_capability.tools.tier_3_browser.ReadBrowserHistoryTool.execute",
                new=AsyncMock(return_value=_tool_result(_HISTORY_CLEAN)),
            ),
        ):
            result = await sweep(_make_wrapper())

        assert "processes" in result
        assert "browser_history" in result
        assert "summary" in result
        assert "overall_risk" in result["summary"]

    async def test_sweep_risk_high_on_suspicious_process(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
                new=AsyncMock(return_value=_tool_result(_PS_AUX_WITH_SUSPICIOUS)),
            ),
            patch(
                "extensions.oi_capability.tools.tier_3_browser.ReadBrowserHistoryTool.execute",
                new=AsyncMock(return_value=_tool_result(_HISTORY_CLEAN)),
            ),
        ):
            result = await sweep(_make_wrapper())

        assert result["summary"]["overall_risk"] == "high"

    async def test_sweep_risk_low_when_clean(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_5_advanced.ListRunningProcessesTool.execute",
                new=AsyncMock(return_value=_tool_result(_PS_AUX_CLEAN)),
            ),
            patch(
                "extensions.oi_capability.tools.tier_3_browser.ReadBrowserHistoryTool.execute",
                new=AsyncMock(return_value=_tool_result(_HISTORY_CLEAN)),
            ),
        ):
            result = await sweep(_make_wrapper())

        assert result["summary"]["overall_risk"] == "low"
