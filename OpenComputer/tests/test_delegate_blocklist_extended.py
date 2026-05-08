"""Hermes spec: subagents must NEVER receive Memory, SendMessage, or ExecuteCode.

These tools either write to shared persistent memory, push messages
cross-platform, or execute arbitrary code — none of which a subagent
should be able to do without explicit user supervision.
"""
from __future__ import annotations

from opencomputer.tools.delegate import DELEGATE_BLOCKED_TOOLS


def test_memory_blocked():
    assert "Memory" in DELEGATE_BLOCKED_TOOLS


def test_send_message_blocked():
    assert "SendMessage" in DELEGATE_BLOCKED_TOOLS


def test_execute_code_blocked():
    assert "ExecuteCode" in DELEGATE_BLOCKED_TOOLS


def test_existing_blocks_still_present():
    """Don't regress existing blocks while extending the set."""
    for name in ("delegate", "AskUserQuestion", "Clarify", "ExitPlanMode"):
        assert name in DELEGATE_BLOCKED_TOOLS, f"{name!r} missing from blocklist"
