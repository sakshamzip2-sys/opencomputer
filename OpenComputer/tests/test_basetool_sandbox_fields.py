"""Tests for the M2 ``BaseTool`` sandbox fields — Milestone 2 (T2.5).

``plugin_sdk.tool_contract.BaseTool`` gained two additive class fields:

* ``sandbox_preference: Literal["required", "skip", "default"] = "default"``
* ``sandbox_backend_hint: str | None = None``

This is a PUBLIC contract change — it must be strictly additive. These
tests pin two things:

1. The defaults mean "current behavior" — every existing tool, which
   never declares either field, reads ``"default"`` / ``None``.
2. A tool subclass can set them, and the override is visible on both
   the class and an instance.
"""

from __future__ import annotations

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── a minimal concrete tool that overrides nothing ────────────────────


class _PlainTool(BaseTool):
    """A tool that declares neither sandbox field — the common case."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="Plain", description="plain", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
        return ToolResult(tool_call_id=call.id, content="ok")


# ─── defaults == current behavior ──────────────────────────────────────


def test_default_sandbox_preference_is_default() -> None:
    assert BaseTool.sandbox_preference == "default"
    assert _PlainTool.sandbox_preference == "default"
    assert _PlainTool().sandbox_preference == "default"


def test_default_sandbox_backend_hint_is_none() -> None:
    assert BaseTool.sandbox_backend_hint is None
    assert _PlainTool.sandbox_backend_hint is None
    assert _PlainTool().sandbox_backend_hint is None


def test_existing_basetool_fields_unchanged() -> None:
    """The pre-M2 class fields keep their values — the change is additive."""
    assert BaseTool.parallel_safe is False
    assert BaseTool.loop_safe is False
    assert BaseTool.strict_mode is False
    assert BaseTool.capability_claims == ()
    assert BaseTool.max_result_size == 100_000


# ─── a tool can opt in ─────────────────────────────────────────────────


def test_tool_can_set_sandbox_preference_required() -> None:
    class _RequiredTool(_PlainTool):
        sandbox_preference = "required"

    assert _RequiredTool.sandbox_preference == "required"
    assert _RequiredTool().sandbox_preference == "required"
    # The base class is untouched — no leakage onto sibling tools.
    assert BaseTool.sandbox_preference == "default"
    assert _PlainTool.sandbox_preference == "default"


def test_tool_can_set_sandbox_preference_skip() -> None:
    class _SkipTool(_PlainTool):
        sandbox_preference = "skip"

    assert _SkipTool.sandbox_preference == "skip"
    assert _SkipTool().sandbox_preference == "skip"


def test_tool_can_set_sandbox_backend_hint() -> None:
    class _HintTool(_PlainTool):
        sandbox_backend_hint = "e2b"

    assert _HintTool.sandbox_backend_hint == "e2b"
    assert _HintTool().sandbox_backend_hint == "e2b"
    # No leakage onto the base or sibling tools.
    assert BaseTool.sandbox_backend_hint is None
    assert _PlainTool.sandbox_backend_hint is None


def test_tool_can_set_both_fields_together() -> None:
    class _BothTool(_PlainTool):
        sandbox_preference = "required"
        sandbox_backend_hint = "docker"

    inst = _BothTool()
    assert inst.sandbox_preference == "required"
    assert inst.sandbox_backend_hint == "docker"


# ─── the fields are exported on the public contract ────────────────────


def test_sandbox_fields_present_on_basetool_via_getattr() -> None:
    """Defensive getattr (how the resolver reads them) sees the defaults.

    The resolver uses ``getattr(tool, "sandbox_preference", "default")``
    so even a pre-M2 tool object lacking the attribute is safe — but a
    post-M2 ``BaseTool`` subclass always has it.
    """
    tool = _PlainTool()
    assert getattr(tool, "sandbox_preference", "MISSING") == "default"
    assert getattr(tool, "sandbox_backend_hint", "MISSING") is None
