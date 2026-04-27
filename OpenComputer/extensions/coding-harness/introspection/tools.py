"""Native cross-platform introspection tools.

Replaces ``extensions/coding-harness/oi_bridge/tools/tier_1_introspection.py``
which shelled out to an Open Interpreter subprocess. The new tools call native
Python libraries directly:

  * list_app_usage       → psutil
  * read_clipboard_once  → pyperclip
  * screenshot           → mss
  * extract_screen_text  → mss + rapidocr-onnxruntime (see ``ocr.py``)
  * list_recent_files    → os.walk + pathlib + stat

Removing the OI subprocess eliminates the AGPL dependency chain and gives us
true cross-platform support (Windows included where the underlying libs allow).
Capability claims live under the ``introspection.*`` namespace from the start —
the legacy ``oi_bridge.*`` namespace will be retired alongside the old module
once T7+ ships.

Class shape lands in T1 (this file): consent_tier, parallel_safe,
capability_claims, schema with original tool names, ``NotImplementedError``
``execute`` bodies. T2-T6 fill those in one tool at a time.
"""

from __future__ import annotations

from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ListAppUsageTool(BaseTool):
    """List recently-active apps in the last N hours (psutil-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.list_app_usage",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List recently-active applications (last N hours).",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_app_usage",
            description="TODO: filled in by T2-T6",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 8)",
                        "default": 8,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError("Lands in T2-T6")


class ReadClipboardOnceTool(BaseTool):
    """Read clipboard contents once, never streamed (pyperclip-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = False  # clipboard is a singleton
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.read_clipboard_once",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read clipboard contents once (never streamed).",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_clipboard_once",
            description="TODO: filled in by T2-T6",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError("Lands in T2-T6")


class ScreenshotTool(BaseTool):
    """Capture a screenshot, returned as base64-encoded PNG (mss-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.screenshot",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Capture a screenshot of the current screen.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="screenshot",
            description="TODO: filled in by T2-T6",
            parameters={
                "type": "object",
                "properties": {
                    "quadrant": {
                        "type": "string",
                        "description": (
                            "Optional screen quadrant to capture: "
                            "'top-left', 'top-right', 'bottom-left', 'bottom-right'"
                        ),
                        "enum": ["top-left", "top-right", "bottom-left", "bottom-right"],
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError("Lands in T2-T6")


class ExtractScreenTextTool(BaseTool):
    """Extract text from the screen via OCR (mss + rapidocr-onnxruntime)."""

    consent_tier: int = 1
    # rapidocr-onnxruntime loads ~200MB of model weights per instance; running
    # multiple in parallel causes memory pressure on typical machines.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.extract_screen_text",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Extract all visible text from the screen using OCR.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="extract_screen_text",
            description="TODO: filled in by T2-T6",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError("Lands in T2-T6")


class ListRecentFilesTool(BaseTool):
    """List files modified in the last N hours (os.walk + stat)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.list_recent_files",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List files modified in the last N hours.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_recent_files",
            description="TODO: filled in by T2-T6",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 8)",
                        "default": 8,
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search (default: home dir)",
                        "default": "~",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default: 50)",
                        "default": 50,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError("Lands in T2-T6")
