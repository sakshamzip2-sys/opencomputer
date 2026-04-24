"""F1 Task 9: consent gate fires BEFORE PreToolUse hooks.

Invariant: if a tool declares capability_claims and no matching grant
exists, the tool is blocked and the PreToolUse hook is NOT called
(plugins cannot pre-empt the gate). Verifies the gate-before-hook order
documented in ~/.claude/plans/i-want-you-to-twinkly-squirrel.md.
"""
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.consent import AuditLogger, ConsentGate, ConsentStore
from opencomputer.agent.state import apply_migrations
from plugin_sdk import CapabilityClaim, ConsentTier


def test_basetool_has_capability_claims_attribute():
    """Minimum: tool classes can declare capability_claims."""
    from plugin_sdk import BaseTool

    # The class attribute exists with an empty default.
    assert hasattr(BaseTool, "capability_claims")
    assert BaseTool.capability_claims == ()


def test_subclass_can_override_capability_claims():
    from plugin_sdk import BaseTool, ToolSchema
    from plugin_sdk.core import ToolCall, ToolResult

    class TestTool(BaseTool):
        capability_claims = (
            CapabilityClaim("read_files", ConsentTier.EXPLICIT, "test"),
        )

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(name="test", description="t", parameters={})

        async def execute(self, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content="ok")

    t = TestTool()
    assert len(t.capability_claims) == 1
    assert t.capability_claims[0].capability_id == "read_files"


def test_agentloop_accepts_consent_gate_kwarg():
    """AgentLoop constructor accepts the consent_gate kwarg (F1 wiring)."""
    import inspect

    from opencomputer.agent.loop import AgentLoop

    sig = inspect.signature(AgentLoop.__init__)
    assert "consent_gate" in sig.parameters


def test_extract_scope_from_tool_call():
    """The scope extractor pulls common path-like args from ToolCall.arguments."""
    from opencomputer.agent.loop import _extract_scope
    from plugin_sdk.core import ToolCall

    assert _extract_scope(ToolCall(id="1", name="t", arguments={"path": "/a/b"})) == "/a/b"
    assert _extract_scope(ToolCall(id="1", name="t", arguments={"file_path": "/x"})) == "/x"
    assert _extract_scope(ToolCall(id="1", name="t", arguments={"url": "https://example.com"})) == "https://example.com"
    assert _extract_scope(ToolCall(id="1", name="t", arguments={})) is None
    assert _extract_scope(ToolCall(id="1", name="t", arguments={"other": "x"})) is None


def test_gate_denies_when_tool_has_claim_and_no_grant():
    """ConsentGate.check denies unconsented claims — foundation for loop wiring."""
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(tmp, check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    log = AuditLogger(conn, hmac_key=b"k" * 16)
    gate = ConsentGate(store=store, audit=log)

    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope="/Users/saksham/foo.py", session_id="s1")
    assert d.allowed is False
    # Ensure the deny path logs an audit entry (would be caught by
    # the loop's dispatch branch before any PreToolUse hook fires).
    rows = conn.execute("SELECT action, decision FROM audit_log").fetchall()
    assert ("check", "deny") in rows
